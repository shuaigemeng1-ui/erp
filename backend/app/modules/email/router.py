"""平台邮件服务配置接口。"""

from __future__ import annotations

from fastapi import APIRouter, Path

from app.api.deps import CenterDb, PlatformAdmin
from app.core.http import success
from app.modules.email import service
from app.modules.email.schemas import EmailProfileInput, EmailProfileUpdate, EmailTestInput

router = APIRouter(prefix="/platform", tags=["email"])


@router.get("/email-profiles", summary="邮件服务账号列表")
def list_email_profiles(center: CenterDb, _: PlatformAdmin):
    return success(service.list_profiles(center))


@router.post("/email-profiles", summary="新增邮件服务账号")
def create_email_profile(body: EmailProfileInput, center: CenterDb, admin: PlatformAdmin):
    return success(service.save_profile(center, body, actor_user_id=admin.user_id))


@router.put("/email-profiles/{profile_id}", summary="更新邮件服务账号")
def update_email_profile(
    body: EmailProfileUpdate,
    center: CenterDb,
    admin: PlatformAdmin,
    profile_id: int = Path(gt=0),
):
    return success(
        service.save_profile(
            center, body, actor_user_id=admin.user_id, profile_id=profile_id
        )
    )


@router.post("/email-profiles/{profile_id}/default", summary="设为默认邮件账号")
def set_default_email_profile(
    center: CenterDb, admin: PlatformAdmin, profile_id: int = Path(gt=0)
):
    return success(service.set_default(center, profile_id, actor_user_id=admin.user_id))


@router.post("/email-profiles/{profile_id}/test", summary="发送测试邮件")
def test_email_profile(
    body: EmailTestInput,
    center: CenterDb,
    _: PlatformAdmin,
    profile_id: int = Path(gt=0),
):
    return success(
        service.test_profile(
            center, profile_id, to=str(body.to), subject=body.subject
        )
    )
