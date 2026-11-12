"""系统配置请求白名单；访问密钥只写入，永不返回。"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Provider = Literal["oss", "cos", "obs", "s3", "s3_compatible"]


def https_url(value: str, *, endpoint: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("请输入有效HTTPS地址") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "\\" in value or any(ord(c) < 33 for c in value)):
        raise ValueError("地址必须使用HTTPS，不能包含账号、密码、查询参数或片段")
    if endpoint and parsed.path not in {"", "/"}:
        raise ValueError("服务地址只填写Endpoint，不包含桶名或对象路径")
    if any(part in {".", ".."} for part in unquote(parsed.path).split("/")):
        raise ValueError("地址路径不能包含相对目录")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("端口不合法")
    return value.rstrip("/")


class StorageProfileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    provider: Provider
    endpoint: str = Field(min_length=1, max_length=500)
    bucket: str = Field(min_length=1, max_length=255)
    region: str = Field(default="", max_length=100)
    public_base_url: str = Field(min_length=1, max_length=1000)
    key_prefix: str = Field(default="", max_length=200)
    addressing_style: Literal["auto", "virtual", "path"] = "auto"
    enabled: bool = True
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None

    @field_validator("region")
    @classmethod
    def validate_region(cls, value):
        if value and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("区域必须填写区域编码（如 cn-south-1），不能填写中文名称；也可留空自动识别")
        return value

    @model_validator(mode="after")
    def validate_obs_addressing(self):
        if self.provider == "obs":
            if self.addressing_style == "path":
                raise ValueError("华为云 OBS 不支持路径寻址，请选择自动或虚拟主机")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket):
                raise ValueError("OBS 桶名须为3至63位小写字母、数字或连接符，当前 HTTPS 适配不支持带点桶名")
        return self

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value):
        return https_url(value, endpoint=True)

    @field_validator("public_base_url")
    @classmethod
    def validate_public_url(cls, value):
        return https_url(value)

    @field_validator("bucket")
    @classmethod
    def validate_bucket(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value) or ".." in value:
            raise ValueError("存储桶名称只能包含字母、数字、点、下划线和连接符")
        return value

    @field_validator("key_prefix")
    @classmethod
    def validate_prefix(cls, value):
        value = value.strip("/")
        if "\\" in value or any(part in {".", ".."} for part in unquote(value).split("/")) or any(ord(c) < 32 for c in value):
            raise ValueError("对象前缀不能包含相对目录、反斜杠或控制字符")
        return value

    @field_validator("access_key_id", "secret_access_key")
    @classmethod
    def validate_secret_length(cls, value):
        if value and len(value.get_secret_value()) > 4000:
            raise ValueError("访问密钥长度超过限制")
        if value is not None:
            cleaned = value.get_secret_value().strip()
            if any(ord(c) < 32 for c in cleaned):
                raise ValueError("访问密钥不能包含控制字符")
            return SecretStr(cleaned) if cleaned else None
        return None


class StorageProfileUpdate(StorageProfileInput):
    version: int = Field(ge=1)


class DefaultStorageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)
    profile_id: str = Field(pattern=r"^[1-9][0-9]*$")
    version: int = Field(ge=1)
