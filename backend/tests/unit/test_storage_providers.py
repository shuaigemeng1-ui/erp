"""对象存储适配器回归：模拟 SDK/HTTP，不访问真实云服务。"""
from types import SimpleNamespace

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError

from app.modules.storage import providers
from app.modules.storage.providers import StorageConfig, StorageProviderError, create_client


def config(provider="s3", **changes):
    values = dict(profile_id=4, provider=provider, endpoint="https://objects.example.test",
                  bucket="images", region="test-region", access_key_id="PRIVATE-ACCESS-ID",
                  secret_access_key="PRIVATE-SECRET-KEY", public_base_url="https://cdn.example.test/media",
                  key_prefix="erp", addressing_style="auto")
    values.update(changes)
    return StorageConfig(**values)


class FakeSDK:
    def __init__(self, error=None):
        self.error = error
        self.uploads = []
        self.heads = []
        self.closed = False

    def put_object(self, **kwargs):
        if self.error:
            raise self.error
        self.uploads.append(kwargs)
        return {"ETag": "test-etag"}

    def head_bucket(self, **kwargs):
        if self.error:
            raise self.error
        self.heads.append(kwargs)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("provider,signature,style", [
    ("oss", "s3", "virtual"), ("cos", "s3", "virtual"), ("obs", "s3v4", "auto"),
    ("s3", "s3v4", "auto"), ("s3_compatible", "s3v4", "auto"),
])
def test_vendor_config_and_explicit_credentials(monkeypatch, provider, signature, style):
    calls = []
    fake = FakeSDK()
    def factory(service, **kwargs):
        calls.append((service, kwargs))
        return fake
    monkeypatch.setattr(providers.boto3.session, "Session", lambda: SimpleNamespace(client=factory))
    client = create_client(config(provider))
    client.head_bucket()
    client.close()
    service, kwargs = calls[0]
    assert service == "s3"
    assert kwargs["aws_access_key_id"] == "PRIVATE-ACCESS-ID"
    assert kwargs["aws_secret_access_key"] == "PRIVATE-SECRET-KEY"
    assert kwargs["aws_session_token"] is None
    assert kwargs["endpoint_url"] == "https://objects.example.test"
    sdk_config = kwargs["config"]
    assert sdk_config.signature_version == signature
    assert sdk_config.s3 == {"addressing_style": style, "payload_signing_enabled": False}
    assert sdk_config.request_checksum_calculation == "when_required"
    assert sdk_config.response_checksum_validation == "when_required"
    assert sdk_config.connect_timeout == 5 and sdk_config.read_timeout == 30
    assert sdk_config.retries["total_max_attempts"] == 3
    assert fake.heads == [{"Bucket": "images"}] and fake.closed


def test_upload_keeps_full_key_and_permanent_encoded_url_without_acl():
    fake = FakeSDK()
    client = create_client(config(key_prefix="erp/uploads/"), sdk_client=fake)
    result = client.put_object("catalog/7/颜色 a+#.png", b"image-bytes", "image/png")
    assert result.profile_id == 4 and result.provider == "s3"
    assert result.object_key == "erp/uploads/catalog/7/颜色 a+#.png"
    assert result.url == "https://cdn.example.test/media/erp/uploads/catalog/7/%E9%A2%9C%E8%89%B2%20a%2B%23.png"
    assert fake.uploads == [{"Bucket": "images", "Key": result.object_key, "Body": b"image-bytes",
                             "ContentType": "image/png", "ContentLength": 11}]
    assert "?" not in result.url and "ACL" not in fake.uploads[0]
    changed = create_client(config("obs", profile_id=5, public_base_url="https://other.example.test"), sdk_client=FakeSDK())
    assert changed.put_object("catalog/7/next.png", b"x", "image/png").url.startswith("https://other.example.test/")
    assert result.url.startswith("https://cdn.example.test/")


@pytest.mark.parametrize("values", [
    {"access_key_id": ""}, {"secret_access_key": ""}, {"bucket": ""},
    {"endpoint": "http://objects.example.test"}, {"endpoint": "https://id:secret@objects.example.test"},
    {"endpoint": "https://objects.example.test/path"}, {"public_base_url": "https://cdn.example.test?signature=secret"},
    {"key_prefix": "../tenant"}, {"provider": "unknown"},
])
def test_invalid_configuration_fails_before_sdk_creation(monkeypatch, values):
    def forbidden():
        pytest.fail("Invalid configuration must not create an SDK session")
    monkeypatch.setattr(providers.boto3.session, "Session", forbidden)
    with pytest.raises(StorageProviderError):
        create_client(config(**values))


def test_credentials_do_not_appear_in_config_repr_or_sdk_failure():
    cfg = config()
    assert cfg.access_key_id not in repr(cfg) and cfg.secret_access_key not in repr(cfg)
    error = ClientError({"Error": {"Code": "AccessDenied", "Message": repr(cfg) + cfg.secret_access_key}}, "PutObject")
    client = create_client(cfg, sdk_client=FakeSDK(error))
    with pytest.raises(StorageProviderError) as caught:
        client.put_object("catalog/7/image.png", b"x", "image/png")
    assert caught.value.code == "AccessDenied"
    assert cfg.access_key_id not in str(caught.value) and cfg.secret_access_key not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    with pytest.raises(StorageProviderError):
        client.head_bucket()


def test_signed_upload_does_not_follow_provider_redirect(monkeypatch):
    client = create_client(config(key_prefix=""))
    sent = []
    class EmptyBody:
        def stream(self, *args, **kwargs):
            return iter(())
    def send(request):
        sent.append(request.url)
        return AWSResponse(request.url, 301,
            {"location": "https://untrusted.example.test/stolen", "x-amz-bucket-region": "other-region"}, EmptyBody())
    monkeypatch.setattr(client._client._endpoint.http_session, "send", send)
    try:
        with pytest.raises(StorageProviderError) as caught:
            client.put_object("catalog/7/image.png", b"x", "image/png")
        assert caught.value.code == "UnexpectedRedirect"
        assert len(sent) == 1 and "untrusted.example.test" not in sent[0]
    finally:
        client.close()


def test_endpoint_guard_blocks_other_host_before_sending():
    client = create_client(config(), sdk_client=FakeSDK())
    client._check_request_endpoint(SimpleNamespace(url="https://images.objects.example.test/key?x-id=PutObject"))
    with pytest.raises(StorageProviderError) as caught:
        client._check_request_endpoint(SimpleNamespace(url="https://objects.example.test.evil.test/key"))
    assert caught.value.code == "UnexpectedEndpoint"


@pytest.mark.parametrize("key", ["/catalog/a.png", "catalog/../a.png", "catalog\\a.png", "catalog//a.png"])
def test_invalid_object_key_never_uploads(key):
    fake = FakeSDK()
    client = create_client(config(), sdk_client=fake)
    with pytest.raises(StorageProviderError):
        client.put_object(key, b"x", "image/png")
    assert not fake.uploads
