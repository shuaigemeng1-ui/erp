"""身份模块请求模型（Pydantic 2）。

* 请求字段使用白名单，未声明的字段不会被接收；
* 邮箱统一小写；角色绑定与数据范围转换到 ``identity.service`` 的不可变输入结构；
* 响应由 Router 组装为字典（大整数 ID 一律字符串）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.identity.service import RoleBindingChoice, ScopeChoice, normalize_email


class LoginRequest(BaseModel):
    """登录请求。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=255, description="登录邮箱")
    password: str = Field(min_length=1, max_length=256, description="登录密码")

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        normalized = normalize_email(value)
        if "@" not in normalized:
            raise ValueError("邮箱格式不正确")
        return normalized


class PasswordChangeRequest(BaseModel):
    """修改密码请求。"""

    current_password: str = Field(min_length=1, max_length=256, description="当前密码")
    new_password: str = Field(min_length=10, max_length=256, description="新密码")


class InvitationAcceptRequest(BaseModel):
    """接受邀请请求。"""

    token: str = Field(min_length=10, max_length=256, description="邀请令牌原文")
    password: str = Field(min_length=10, max_length=256, description="设置的新密码")
    display_name: str = Field(default="", max_length=100, description="显示名称")


class ScopePayload(BaseModel):
    """单个数据范围选择。"""

    scope_type: Literal["all", "stores", "stock_places"] = Field(description="范围类型")
    scope_ids: list[int] = Field(default_factory=list, description="范围对象ID列表（all 时为空）")


class RoleBindingPayload(BaseModel):
    """一条角色绑定：角色 + 数据范围。"""

    role_id: int = Field(gt=0, description="角色ID")
    scopes: list[ScopePayload] = Field(default_factory=list, description="数据范围列表")

    def to_choice(self) -> RoleBindingChoice:
        return RoleBindingChoice(
            role_id=self.role_id,
            scopes=tuple(
                ScopeChoice(scope_type=scope.scope_type, scope_ids=tuple(scope.scope_ids))
                for scope in self.scopes
            ),
        )


def to_choices(bindings: list[RoleBindingPayload]) -> list[RoleBindingChoice]:
    """请求结构 -> 服务层输入。"""
    return [binding.to_choice() for binding in bindings]


class InviteMemberRequest(BaseModel):
    """邀请成员请求。"""

    email: str = Field(min_length=3, max_length=255, description="受邀邮箱")
    role_bindings: list[RoleBindingPayload] = Field(
        min_length=1, description="角色与数据范围（至少一个，范围不能为空）"
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        normalized = normalize_email(value)
        if "@" not in normalized:
            raise ValueError("邮箱格式不正确")
        return normalized


class MemberStatusRequest(BaseModel):
    """成员状态变更请求。"""

    status: Literal["active", "disabled"] = Field(description="成员状态")


class ReplaceMemberRolesRequest(BaseModel):
    """整体覆盖成员角色绑定请求。"""

    role_bindings: list[RoleBindingPayload] = Field(
        min_length=1, description="角色与数据范围（整体覆盖，范围不能为空）"
    )


class OwnerTransferRequest(BaseModel):
    """转移租户所有者请求。"""

    target_member_id: int = Field(gt=0, description="新的所有者：当前租户的 active 成员ID")


class RoleCreateRequest(BaseModel):
    """创建角色请求。"""

    code: str = Field(min_length=1, max_length=64, description="角色编码")
    name: str = Field(min_length=1, max_length=64, description="角色名称")
    description: str = Field(default="", max_length=255, description="角色说明")
    permissions: list[str] = Field(default_factory=list, description="权限编码列表")
    copy_from_role_id: int | None = Field(default=None, gt=0, description="复制来源角色ID")


class RoleUpdateRequest(BaseModel):
    """修改角色请求（局部更新）。"""

    name: str | None = Field(default=None, max_length=64, description="角色名称")
    description: str | None = Field(default=None, max_length=255, description="角色说明")
    permissions: list[str] | None = Field(default=None, description="权限编码列表（整体覆盖）")


class MemberListQuery(BaseModel):
    """成员列表查询（文档用途，路由使用 Query 参数）。"""

    page: int = 1
    page_size: int = 20
    q: str = ""
    status: str | None = None


def dump_request(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """审计 payload 用的安全转换（不包含密码等敏感字段）。"""
    if not payload:
        return None
    blocked = {"password", "new_password", "current_password", "token", "invitation_token"}
    return {key: value for key, value in payload.items() if key not in blocked}
