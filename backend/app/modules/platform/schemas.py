"""平台管理请求模型（Pydantic 2）。

* 请求字段白名单控制，未声明字段不会进入业务逻辑；
* 渠道应用密钥字段允许 ``None``（不修改）与空串（清空），由 channels 模块解释；
* 响应由 Router 组装为字典（大整数 ID 一律字符串）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.identity.service import normalize_email


class TenantCreateRequest(BaseModel):
    """创建租户请求。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    code: str = Field(min_length=2, max_length=40, description="租户编码（小写字母、数字、下划线、短横线）")
    name: str = Field(min_length=1, max_length=128, description="租户名称")
    timezone: str = Field(default="Asia/Shanghai", max_length=64, description="租户时区")
    plan_id: int | None = Field(default=None, gt=0, description="套餐ID（为空使用默认套餐）")
    owner_email: str = Field(min_length=3, max_length=255, description="负责人邮箱")

    @field_validator("code")
    @classmethod
    def _normalize_code(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("owner_email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        normalized = normalize_email(value)
        if "@" not in normalized:
            raise ValueError("邮箱格式不正确")
        return normalized


class SuspendTenantRequest(BaseModel):
    """暂停租户请求。"""

    reason: str = Field(min_length=1, max_length=255, description="暂停原因")


class PlanFeaturePayload(BaseModel):
    """套餐额度项。"""

    feature_code: str = Field(min_length=1, max_length=64, description="额度编码")
    limit_value: int | None = Field(default=None, ge=0, description="额度上限（空表示不限制）")


class PlanCreateRequest(BaseModel):
    """创建套餐请求。"""

    code: str = Field(min_length=1, max_length=64, description="套餐编码")
    name: str = Field(min_length=1, max_length=64, description="套餐名称")
    description: str = Field(default="", max_length=255, description="套餐说明")
    features: list[PlanFeaturePayload] = Field(default_factory=list, description="套餐额度")


class PlanUpdateRequest(BaseModel):
    """调整套餐请求（局部更新）。"""

    name: str | None = Field(default=None, max_length=64, description="套餐名称")
    description: str | None = Field(default=None, max_length=255, description="套餐说明")
    status: str | None = Field(default=None, description="套餐状态：active / disabled")
    features: list[PlanFeaturePayload] | None = Field(default=None, description="套餐额度（整体覆盖）")


class ChannelAppPutRequest(BaseModel):
    """保存平台渠道应用请求。

    ``partner_key`` / ``partner_secret``：``None`` 表示不修改，空串表示清空；
    密钥只会加密入库，不会在响应中回显。
    """

    partner_id: int | None = Field(default=None, gt=0, description="平台 Partner ID")
    partner_key: str | None = Field(default=None, max_length=255, description="Partner Key（不回显）")
    partner_secret: str | None = Field(default=None, max_length=255, description="Partner Secret（不回显）")
    redirect_url: str | None = Field(default=None, max_length=512, description="授权回调地址")
