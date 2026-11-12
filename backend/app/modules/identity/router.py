"""身份与租户路由。

认证与本人（``/auth``、``/me``）：

* ``GET  /auth/csrf``：签发匿名 CSRF 令牌并写 ``vs_csrf`` Cookie（登录前）
* ``POST /auth/login``：同源 Cookie 会话登录，成功后轮换会话与 CSRF
* ``POST /auth/logout``：撤销当前会话
* ``POST /auth/password``：修改密码，成功后撤销该用户全部会话并重签当前会话
* ``GET  /auth/invitations/{token}``：邀请信息预览
* ``POST /auth/invitations/accept``：接受邀请并设置密码
* ``GET  /me`` / ``GET /me/tenants``

租户内（``/tenants/{tenant_id}``）：

* ``GET    /bootstrap``：当前租户、成员角色与绑定、有效权限、功能开关、权限版本
* ``GET    /members``：成员分页列表
* ``POST   /members/invitations``：邀请成员（防提权，一次有效令牌）
* ``PATCH  /members/{member_id}``：启用 / 停用成员
* ``PUT    /members/{member_id}/roles``：整体覆盖角色与数据范围
* ``GET/POST /roles``、``PATCH/DELETE /roles/{role_id}``：角色管理
* ``GET    /permissions``：权限编码目录

CSRF / 来源校验由 ``RequestContextMiddleware`` 统一完成，路由内不重复实现。
依赖声明顺序固定为「租户上下文 → 权限快照 → 权限校验」，确保 request.state 已就绪。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import (
    AccessSnapshotDep,
    CenterDb,
    CurrentUser,
    RequirePermission,
    TenantCtx,
    TenantDb,
    require_permission,
)
from app.core.auth import (
    clear_session_cookies,
    session_scope,
    set_csrf_cookie,
    set_session_cookies,
)
from app.core.errors import AppError, ErrorCode
from app.core.http import PageParams, paged, success
from app.core.permissions import Permission
from app.core.redis import (
    create_session,
    delete_session,
    issue_anonymous_csrf,
    revoke_user_sessions,
)
from app.modules.identity import repository as repo
from app.modules.identity import service
from app.modules.identity.schemas import (
    InvitationAcceptRequest,
    InviteMemberRequest,
    LoginRequest,
    MemberStatusRequest,
    OwnerTransferRequest,
    PasswordChangeRequest,
    ReplaceMemberRolesRequest,
    RoleCreateRequest,
    RoleUpdateRequest,
    to_choices,
)

router = APIRouter(tags=["identity"])
tenant_router = APIRouter(tags=["tenant"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# --------------------------------------------------------------------------- 认证
@router.get("/auth/csrf", summary="签发匿名 CSRF 令牌")
def get_csrf_token(response: Response) -> dict:
    """登录前获取 CSRF 令牌：同时写入可读 Cookie ``vs_csrf``。"""
    token = issue_anonymous_csrf()
    set_csrf_cookie(response, token)
    return success({"csrf_token": token})


@router.post("/auth/login", summary="登录")
def login(payload: LoginRequest, request: Request, center: CenterDb) -> JSONResponse:
    """邮箱 + 密码登录，成功后下发 HttpOnly 会话 Cookie 与 CSRF Cookie。

    ``scope`` 区分两个独立入口：租户登录（tenant）与平台管理登录（platform）。
    平台管理端只允许平台管理员登录；会话内记录 scope，后续平台/租户接口分别校验。

    连续失败 5 次 / 15 分钟（同一邮箱 + IP）返回 ``429 AUTH_RATE_LIMITED``。
    """
    user = service.authenticate(center, payload.email, payload.password, ip=_client_ip(request))
    is_platform_admin = service.is_platform_admin(center, int(user.id))
    if payload.scope == "platform" and not is_platform_admin:
        raise AppError(ErrorCode.PLATFORM_PERMISSION_REQUIRED, "该账号不是平台管理员")
    service.record_login(center, user)
    # 轮换会话：旧会话（若存在）立即失效
    old_session_id = getattr(request.state, "session_id", "")
    if old_session_id:
        delete_session(old_session_id)
    session_id, _session_payload, csrf_token = create_session(
        int(user.id), scope=payload.scope
    )
    data = {
        "user": service.user_payload(user, is_platform_admin=is_platform_admin),
        "tenants": service.list_user_tenants(center, int(user.id)),
        "session_scope": payload.scope,
    }
    response = JSONResponse(status_code=200, content=success(data))
    set_session_cookies(response, session_id, csrf_token)
    return response


@router.post("/auth/logout", summary="退出登录")
def logout(request: Request, user: CurrentUser) -> JSONResponse:
    """只撤销当前会话，并清理 Cookie。"""
    session_id = getattr(request.state, "session_id", "")
    if session_id:
        delete_session(session_id, user.user_id)
    response = JSONResponse(status_code=200, content=success({"logged_out": True}))
    clear_session_cookies(response)
    return response


@router.post("/auth/password", summary="修改密码")
def change_password(
    payload: PasswordChangeRequest, request: Request, center: CenterDb, user: CurrentUser
) -> JSONResponse:
    """修改密码后撤销该用户全部会话，并为当前请求重新签发会话。"""
    service.change_password(center, user.user_id, payload.current_password, payload.new_password)
    current_scope = session_scope(getattr(request.state, "session_payload", None)) or "tenant"
    old_session_id = getattr(request.state, "session_id", "")
    if old_session_id:
        delete_session(old_session_id, user.user_id)
    revoke_user_sessions(user.user_id)
    session_id, _session_payload, csrf_token = create_session(
        user.user_id, scope=current_scope
    )
    response = JSONResponse(status_code=200, content=success({"updated": True}))
    set_session_cookies(response, session_id, csrf_token)
    return response


@router.get("/auth/invitations/{token}", summary="邀请信息预览")
def preview_invitation(
    center: CenterDb,
    token: str = Path(min_length=10, max_length=256, description="邀请令牌原文"),
) -> dict:
    return success(service.preview_invitation(center, token))


@router.post("/auth/invitations/accept", summary="接受邀请并设置密码")
def accept_invitation(payload: InvitationAcceptRequest, center: CenterDb) -> JSONResponse:
    """激活邀请：校验一次性令牌后建立成员关系、写入角色绑定并签发会话。"""
    user, tenants = service.accept_invitation(
        center,
        token=payload.token,
        password=payload.password,
        display_name=payload.display_name,
    )
    session_id, _session_payload, csrf_token = create_session(
        int(user.id), scope="tenant"
    )
    data = {
        "user": service.user_payload(
            user, is_platform_admin=service.is_platform_admin(center, int(user.id))
        ),
        "tenants": tenants,
        "session_scope": "tenant",
    }
    response = JSONResponse(status_code=200, content=success(data))
    set_session_cookies(response, session_id, csrf_token)
    return response


# --------------------------------------------------------------------------- 本人
@router.get("/me", summary="当前账号")
def me(request: Request, user: CurrentUser) -> dict:
    scope = session_scope(getattr(request.state, "session_payload", None)) or "tenant"
    return success(
        {
            "id": str(user.user_id),
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status,
            "is_platform_admin": user.is_platform_admin,
            "locale": user.locale,
            "session_scope": scope,
        }
    )


@router.get("/me/tenants", summary="当前账号可访问租户")
def my_tenants(center: CenterDb, user: CurrentUser) -> dict:
    return success(service.list_user_tenants(center, user.user_id))


# --------------------------------------------------------------------------- 租户启动数据
@tenant_router.get("/tenants/{tenant_id}/bootstrap", summary="租户启动数据")
def bootstrap(ctx: TenantCtx, snapshot: AccessSnapshotDep, db: TenantDb) -> dict:
    return success(service.bootstrap_payload(db, ctx, snapshot))


# --------------------------------------------------------------------------- 成员
@tenant_router.get("/tenants/{tenant_id}/members", summary="成员列表")
def list_members(
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    center: CenterDb,
    pagination: PageParams = Depends(),
    q: str = Query(default="", max_length=100, description="按邮箱或名称搜索"),
    status: str | None = Query(default=None, description="成员状态过滤"),
    _: RequirePermission = Depends(require_permission(Permission.MEMBER_READ.value)),
) -> dict:
    data = service.list_members(
        center,
        db,
        tenant_id=ctx.tenant_id,
        page=pagination.page,
        page_size=pagination.page_size,
        keyword=q,
        status=status,
    )
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@tenant_router.post("/tenants/{tenant_id}/members/invitations", summary="邀请成员")
def invite_member(
    payload: InviteMemberRequest,
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    center: CenterDb,
    _: RequirePermission = Depends(require_permission(Permission.MEMBER_INVITE.value)),
) -> dict:
    data = service.create_member_invitation(
        center,
        db,
        tenant_id=ctx.tenant_id,
        email=payload.email,
        bindings=to_choices(payload.role_bindings),
        actor_id=ctx.user_id,
        actor_effective=snapshot.effective,
    )
    return success(data)


@tenant_router.patch("/tenants/{tenant_id}/members/{member_id}", summary="启用或停用成员")
def update_member(
    payload: MemberStatusRequest,
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    center: CenterDb,
    member_id: int = Path(ge=1, description="成员ID"),
    _: RequirePermission = Depends(require_permission(Permission.MEMBER_INVITE.value)),
) -> dict:
    return success(
        service.update_member_status(
            center,
            db,
            tenant_id=ctx.tenant_id,
            member_id=member_id,
            status=payload.status,
            actor_id=ctx.user_id,
        )
    )


@tenant_router.put("/tenants/{tenant_id}/members/{member_id}/roles", summary="整体覆盖成员角色与数据范围")
def replace_member_roles(
    payload: ReplaceMemberRolesRequest,
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    center: CenterDb,
    member_id: int = Path(ge=1, description="成员ID"),
    _: RequirePermission = Depends(require_permission(Permission.ROLE_ASSIGN.value)),
) -> dict:
    return success(
        service.replace_member_bindings(
            center,
            db,
            tenant_id=ctx.tenant_id,
            member_id=member_id,
            bindings=to_choices(payload.role_bindings),
            actor_id=ctx.user_id,
            actor_effective=snapshot.effective,
        )
    )


@tenant_router.post("/tenants/{tenant_id}/owner-transfer", summary="转移租户所有者")
def transfer_owner(
    payload: OwnerTransferRequest,
    ctx: TenantCtx,
    db: TenantDb,
    center: CenterDb,
    user: CurrentUser,
) -> dict:
    """仅当前有效所有者（或平台管理员）可调用；最后有效所有者保护见成员停用 / 角色覆盖。

    两个库无法跨库事务：先写租户库（角色绑定 + 权限版本），再写中心库（归属标记）。
    """
    return success(
        service.transfer_owner(
            center,
            db,
            tenant_id=ctx.tenant_id,
            target_member_id=payload.target_member_id,
            actor_user_id=ctx.user_id,
            actor_member_id=ctx.member_id,
            actor_is_platform_admin=user.is_platform_admin,
        )
    )


@tenant_router.post(
    "/tenants/{tenant_id}/members/invitations/{invitation_id}/resend",
    summary="重新签发成员邀请",
)
def resend_invitation(
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    center: CenterDb,
    invitation_id: int = Path(ge=1, description="邀请ID"),
    _: RequirePermission = Depends(require_permission(Permission.MEMBER_INVITE.value)),
) -> dict:
    """轮换邀请令牌（旧令牌立即失效），返回明文 token 与激活链接一次。"""
    return success(
        service.resend_invitation(
            center,
            db,
            tenant_id=ctx.tenant_id,
            invitation_id=invitation_id,
            actor_id=ctx.user_id,
        )
    )


# --------------------------------------------------------------------------- 角色与权限
@tenant_router.get("/tenants/{tenant_id}/roles", summary="角色列表")
def list_roles(
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    _: RequirePermission = Depends(require_permission(Permission.ROLE_MANAGE.value)),
) -> dict:
    return success(service.list_roles(db, ctx.tenant_id))


@tenant_router.post("/tenants/{tenant_id}/roles", summary="创建角色")
def create_role(
    payload: RoleCreateRequest,
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    _: RequirePermission = Depends(require_permission(Permission.ROLE_MANAGE.value)),
) -> dict:
    return success(
        service.create_role(
            db,
            tenant_id=ctx.tenant_id,
            code=payload.code,
            name=payload.name,
            description=payload.description,
            permissions=payload.permissions,
            copy_from_role_id=payload.copy_from_role_id,
            actor_id=ctx.user_id,
            actor_effective=snapshot.effective,
        )
    )


@tenant_router.patch("/tenants/{tenant_id}/roles/{role_id}", summary="修改角色")
def update_role(
    payload: RoleUpdateRequest,
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    role_id: int = Path(ge=1, description="角色ID"),
    _: RequirePermission = Depends(require_permission(Permission.ROLE_MANAGE.value)),
) -> dict:
    return success(
        service.update_role(
            db,
            tenant_id=ctx.tenant_id,
            role_id=role_id,
            name=payload.name,
            description=payload.description,
            permissions=payload.permissions,
            actor_id=ctx.user_id,
            actor_effective=snapshot.effective,
        )
    )


@tenant_router.delete("/tenants/{tenant_id}/roles/{role_id}", summary="删除角色")
def delete_role(
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    role_id: int = Path(ge=1, description="角色ID"),
    _: RequirePermission = Depends(require_permission(Permission.ROLE_MANAGE.value)),
) -> dict:
    service.delete_role(db, tenant_id=ctx.tenant_id, role_id=role_id, actor_id=ctx.user_id)
    return success({"deleted": True, "id": str(role_id)})


@tenant_router.get("/tenants/{tenant_id}/permissions", summary="权限编码目录")
def permission_catalogue(
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    _: RequirePermission = Depends(require_permission(Permission.ROLE_MANAGE.value)),
) -> dict:
    return success(service.permission_codes_catalog())


# --------------------------------------------------------------------------- 邀请列表（辅助）
@tenant_router.get("/tenants/{tenant_id}/invitations", summary="待处理邀请列表")
def list_invitations(
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    center: CenterDb,
    pagination: PageParams = Depends(),
    _: RequirePermission = Depends(require_permission(Permission.MEMBER_INVITE.value)),
) -> dict:
    rows = repo.list_invitations(
        center, ctx.tenant_id, offset=(pagination.page - 1) * pagination.page_size, limit=pagination.page_size
    )
    items = [
        {
            "id": str(row.id),
            "email": row.email,
            "status": row.status,
            "expires_at": service.iso_utc(row.expires_at),
            "invited_by": str(row.invited_by) if row.invited_by else None,
            "created_at": service.iso_utc(row.created_at),
        }
        for row in rows
    ]
    return paged(items, len(items), pagination.page, pagination.page_size)


__all__ = ["router", "tenant_router"]
