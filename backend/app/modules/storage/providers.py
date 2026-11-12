"""云对象存储适配：显式凭据、厂商签名配置和长期公开 URL。

OSS/COS 使用其官方 AWS SDK 适配说明要求的 S3 V2 签名；
OBS、AWS S3 及其他兼容服务使用 S3 V4。不改变桶或对象的公开权限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any
from urllib.parse import quote, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, InvalidRegionError, ReadTimeoutError, SSLError

SUPPORTED_PROVIDERS = ("oss", "cos", "obs", "s3", "s3_compatible")
PROVIDER_NAMES = {
    "oss": "阿里云 OSS", "cos": "腾讯云 COS", "obs": "华为云 OBS",
    "s3": "AWS S3", "s3_compatible": "S3 兼容服务",
}
_SAFE_ERROR_CODES = frozenset({
    "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "NoSuchBucket",
    "AuthorizationHeaderMalformed", "PermanentRedirect", "TemporaryRedirect",
    "InvalidBucketName", "RequestTimeout", "SlowDown", "ServiceUnavailable",
})
_PUBLIC_ERRORS = {
    "StorageRequestFailed": "对象存储请求失败，请检查服务地址、密钥和存储桶权限",
    "InvalidConfiguration": "对象存储配置无效，请检查服务地址、密钥和存储桶",
    "InvalidRegion": "区域必须填写区域编码（如 cn-south-1），不能填写中文名称；也可留空自动识别",
    "ObsPathStyleUnsupported": "华为云 OBS 不支持路径寻址，请选择自动或虚拟主机",
    "ObsBucketNameUnsupported": "OBS 桶名须为3至63位小写字母、数字或连接符，当前 HTTPS 适配不支持带点桶名",
    "InvalidObjectKey": "对象存储目录或文件键无效，请检查目录层级和长度",
    "UnexpectedRedirect": "对象存储服务要求重定向，请核对存储桶区域和服务地址",
    "UnexpectedEndpoint": "对象存储请求地址与配置不一致，请核对服务地址和桶名",
    "AccessDenied": "对象存储拒绝访问，请检查访问密钥和存储桶操作权限；连接检查需要 HeadBucket 权限",
    "InvalidAccessKeyId": "对象存储访问密钥 ID 无效，请检查配置",
    "SignatureDoesNotMatch": "对象存储签名校验失败，请检查密钥、区域编码和服务器时间",
    "NoSuchBucket": "存储桶不存在，请检查桶名、区域和服务地址",
    "AuthorizationHeaderMalformed": "对象存储认证格式不匹配，请检查厂商、区域和服务地址",
    "PermanentRedirect": "存储桶要求重定向，请核对区域和服务地址",
    "TemporaryRedirect": "对象存储要求临时重定向，请稍后重试或核对服务地址",
    "InvalidBucketName": "存储桶名称无效，请检查桶名",
    "RequestTimeout": "对象存储请求超时，请检查服务器网络后重试",
    "SlowDown": "对象存储请求受限，请稍后重试",
    "ServiceUnavailable": "对象存储服务暂不可用，请稍后重试",
    "NetworkError": "无法连接对象存储服务，请检查服务地址、DNS和服务器网络",
    "TlsError": "对象存储 HTTPS 证书校验失败，请检查服务地址及证书",
}


@dataclass(frozen=True, slots=True)
class StorageConfig:
    profile_id: int
    provider: str
    endpoint: str
    bucket: str
    region: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    public_base_url: str
    key_prefix: str = ""
    addressing_style: str = "auto"


@dataclass(frozen=True, slots=True)
class UploadResult:
    profile_id: int
    provider: str
    object_key: str
    url: str


class StorageProviderError(Exception):
    """只暴露固定提示及白名单错误码，不携带 SDK 响应或异常文本。"""

    def __init__(self, message="对象存储请求失败，请检查服务地址、密钥和存储桶权限", *, code="StorageRequestFailed"):
        self.message = message
        self.code = code
        self.error_code = code
        super().__init__(message)


def public_error(exc: StorageProviderError) -> tuple[str, str]:
    """仅从本地固定字典构造API错误，忽略异常携带的任意文本。"""
    code = exc.code if isinstance(exc.code, str) and exc.code in _PUBLIC_ERRORS else "StorageRequestFailed"
    return _PUBLIC_ERRORS[code], code


def _checked_url(value: str, *, endpoint: bool):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment
                 and not any(ord(char) < 33 for char in value))
        valid = valid and (not endpoint or parsed.path in ("", "/"))
        # Accessing port also rejects malformed/non-numeric/out-of-range ports.
        parsed.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise StorageProviderError("对象存储地址必须为有效的 HTTPS 地址", code="InvalidConfiguration")
    return parsed


def _checked_key(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise StorageProviderError("对象存储目录或文件键无效", code="InvalidObjectKey")
    if not value and allow_empty:
        return ""
    if (not value or value.startswith("/") or "\\" in value
            or any(ord(char) < 32 for char in value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise StorageProviderError("对象存储目录或文件键无效", code="InvalidObjectKey")
    if len(value.encode("utf-8")) > 1024:
        raise StorageProviderError("对象存储文件键过长", code="InvalidObjectKey")
    return value


def _safe_sdk_error(exc: Exception) -> StorageProviderError:
    code = "StorageRequestFailed"
    if isinstance(exc, ClientError):
        candidate = exc.response.get("Error", {}).get("Code")
        if isinstance(candidate, str):
            candidate = {"403": "AccessDenied", "404": "NoSuchBucket", "408": "RequestTimeout", "503": "ServiceUnavailable"}.get(candidate, candidate)
        if isinstance(candidate, str) and candidate in _SAFE_ERROR_CODES:
            code = candidate
    elif isinstance(exc, InvalidRegionError):
        code = "InvalidRegion"
    elif isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        code = "RequestTimeout"
    elif isinstance(exc, SSLError):
        code = "TlsError"
    elif isinstance(exc, EndpointConnectionError):
        code = "NetworkError"
    return StorageProviderError(_PUBLIC_ERRORS[code], code=code)


class S3StorageClient:
    """单个配置快照对应一个客户端；调用方在 finally 中 close。"""

    def __init__(self, config: StorageConfig, *, sdk_client: Any | None = None):
        if config.provider not in SUPPORTED_PROVIDERS or config.addressing_style not in {"auto", "virtual", "path"}:
            raise StorageProviderError("不支持的对象存储厂商或寻址方式", code="InvalidConfiguration")
        if not config.access_key_id or not config.secret_access_key or not config.bucket:
            # Never allow boto3 to fall through to environment/instance credentials.
            raise StorageProviderError("对象存储账号、密钥和存储桶不能为空", code="InvalidConfiguration")
        if config.region and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", config.region):
            raise StorageProviderError(_PUBLIC_ERRORS["InvalidRegion"], code="InvalidRegion")
        if config.provider == "obs":
            if config.addressing_style == "path":
                raise StorageProviderError(_PUBLIC_ERRORS["ObsPathStyleUnsupported"], code="ObsPathStyleUnsupported")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", config.bucket):
                raise StorageProviderError(_PUBLIC_ERRORS["ObsBucketNameUnsupported"], code="ObsBucketNameUnsupported")
        endpoint = _checked_url(config.endpoint, endpoint=True)
        _checked_url(config.public_base_url, endpoint=False)
        prefix = config.key_prefix.strip("/")
        _checked_key(prefix, allow_empty=True)
        self.config = config
        self.key_prefix = prefix
        self._allowed_hosts = {endpoint.hostname, f"{config.bucket}.{endpoint.hostname}"}
        self._allowed_port = endpoint.port or 443
        try:
            self._client = sdk_client if sdk_client is not None else self._build_sdk_client()
            events = getattr(getattr(self._client, "meta", None), "events", None)
            if events is not None:
                # Botocore's HTTP transport does not follow redirects; this also blocks
                # its S3 region redirect handler from resending signed credentials.
                events.register_first("needs-retry.s3", self._reject_redirect)
                events.register_first("before-send.s3", self._check_request_endpoint)
        except StorageProviderError:
            raise
        except Exception as exc:
            raise _safe_sdk_error(exc) from None

    def _build_sdk_client(self):
        signature = "s3" if self.config.provider in {"oss", "cos"} else "s3v4"
        style = self.config.addressing_style
        if style == "auto" and self.config.provider in {"oss", "cos", "obs"}:
            style = "virtual"
        sdk_config = Config(
            signature_version=signature,
            connect_timeout=5,
            read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": style, "payload_signing_enabled": False},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            ignore_configured_endpoint_urls=True,
            proxies={},
        )
        # Passing both credentials explicitly prevents ambient AWS credentials and
        # metadata endpoints from being used for another configured cloud vendor.
        return boto3.session.Session().client(
            "s3", endpoint_url=self.config.endpoint.rstrip("/"),
            aws_access_key_id=self.config.access_key_id,
            aws_secret_access_key=self.config.secret_access_key,
            aws_session_token=None,
            region_name=self.config.region or "us-east-1", config=sdk_config,
        )

    def _reject_redirect(self, response=None, **kwargs):
        if response is not None:
            status = getattr(response[0], "status_code", None)
            if status in {301, 302, 303, 307, 308}:
                raise StorageProviderError("对象存储服务要求重定向，请核对存储桶区域和服务地址", code="UnexpectedRedirect")

    def _check_request_endpoint(self, request, **kwargs):
        try:
            parsed = urlsplit(request.url)
            valid = (parsed.scheme == "https" and parsed.username is None and parsed.password is None
                     and not parsed.fragment and parsed.hostname in self._allowed_hosts
                     and (parsed.port or 443) == self._allowed_port)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise StorageProviderError("对象存储请求地址与配置不一致", code="UnexpectedEndpoint")

    def put_object(self, key: str, data: bytes, content_type: str) -> UploadResult:
        """传入不含配置前缀的对象键，返回持久化后的完整键和长期 URL。"""
        key = _checked_key(key)
        full_key = _checked_key(f"{self.key_prefix}/{key}" if self.key_prefix else key)
        url = self.config.public_base_url.rstrip("/") + "/" + quote(full_key, safe="/-_.~")
        if len(url) > 2048:
            raise StorageProviderError("对象存储访问地址过长", code="InvalidConfiguration")
        try:
            self._client.put_object(Bucket=self.config.bucket, Key=full_key, Body=data,
                                    ContentType=content_type, ContentLength=len(data))
        except StorageProviderError:
            raise
        except Exception as exc:
            raise _safe_sdk_error(exc) from None
        return UploadResult(profile_id=self.config.profile_id, provider=self.config.provider,
                            object_key=full_key, url=url)

    def head_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.config.bucket)
        except StorageProviderError:
            raise
        except Exception as exc:
            raise _safe_sdk_error(exc) from None

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            # Pool cleanup must not turn a confirmed upload into an apparent failure.
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def create_client(config: StorageConfig, *, sdk_client: Any | None = None) -> S3StorageClient:
    return S3StorageClient(config, sdk_client=sdk_client)
