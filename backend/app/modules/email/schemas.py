"""邮件服务配置请求模型。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

EmailProviderCode = Literal["aliyun_directmail", "tencent_ses"]


class EmailProfileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    provider: EmailProviderCode
    region: str = Field(default="", max_length=64)
    from_address: EmailStr
    from_name: str = Field(default="", max_length=100)
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    enabled: bool = True

    @field_validator("access_key_id", "secret_access_key")
    @classmethod
    def validate_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        cleaned = value.get_secret_value().strip()
        if not cleaned:
            return None
        if len(cleaned) > 4000 or any(ord(c) < 32 for c in cleaned):
            raise ValueError("密钥长度超限或包含控制字符")
        return SecretStr(cleaned)


class EmailProfileUpdate(EmailProfileInput):
    version: int = Field(ge=1)


class EmailTestInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    to: EmailStr
    subject: str = Field(default="ERP 邮件服务测试", max_length=200)


class EmailSendInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    to: list[EmailStr] = Field(min_length=1, max_length=50)
    subject: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=100_000)
    html: str = Field(default="", max_length=200_000)

    @field_validator("to")
    @classmethod
    def validate_to(cls, value: list[EmailStr]) -> list[EmailStr]:
        if len(set(value)) != len(value):
            raise ValueError("收件人不能重复")
        return value
