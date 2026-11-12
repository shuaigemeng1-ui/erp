"""平台系统配置；全部接口只接受平台管理员会话。"""
from fastapi import APIRouter, Path

from app.api.deps import CenterDb, PlatformAdmin
from app.core.http import success
from app.modules.storage import service
from app.modules.storage.schemas import DefaultStorageInput, StorageProfileInput, StorageProfileUpdate

router = APIRouter(prefix="/platform", tags=["system-settings"])


@router.get("/system-settings", summary="系统配置与对象存储账号")
def get_system_settings(center: CenterDb, _: PlatformAdmin):
    return success(service.get_settings(center))


@router.post("/storage-profiles", summary="新增对象存储账号")
def create_storage_profile(body: StorageProfileInput, center: CenterDb, admin: PlatformAdmin):
    return success(service.save_profile(center, body, actor_user_id=admin.user_id))


@router.put("/storage-profiles/{profile_id}", summary="更新对象存储账号")
def update_storage_profile(body: StorageProfileUpdate, center: CenterDb, admin: PlatformAdmin, profile_id: int = Path(gt=0)):
    return success(service.save_profile(center, body, actor_user_id=admin.user_id, profile_id=profile_id))


@router.put("/system-settings/default-storage", summary="切换默认对象存储账号")
def set_default_storage(body: DefaultStorageInput, center: CenterDb, admin: PlatformAdmin):
    return success(service.set_default(center, int(body.profile_id), version=body.version, actor_user_id=admin.user_id))


@router.post("/storage-profiles/{profile_id}/test", summary="检查存储桶连接")
def test_storage_profile(center: CenterDb, _: PlatformAdmin, profile_id: int = Path(gt=0)):
    return success(service.test_profile(center, profile_id))
