"""商品中心的租户资料选项和持久化图片；不依赖额外文件服务。"""
from __future__ import annotations
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import re
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.core.config import settings
from app.core.errors import AppError, ConflictError, ErrorCode
from app.modules.catalog.models_tenant import Supplier, CatalogOption
from app.modules.identity.models_center import TenantMembership, User
from app.modules.audit.models_tenant import FileRecord


class SupplierIn(BaseModel):
    model_config=ConfigDict(str_strip_whitespace=True,extra="forbid")
    code: str=Field(min_length=1,max_length=64)
    name: str=Field(min_length=1,max_length=200)


class OptionIn(BaseModel):
    model_config=ConfigDict(str_strip_whitespace=True,extra="forbid")
    kind: Literal["brand","category","tag","packaging"]
    name: str=Field(min_length=1,max_length=100)
    parent_id: int | None=Field(default=None,gt=0)


def member_options(center,tenant_id):
    rows=center.execute(select(TenantMembership.id,User.display_name,TenantMembership.status,User.status)
        .join(User,User.id==TenantMembership.user_id).where(TenantMembership.tenant_id==tenant_id).order_by(User.display_name,TenantMembership.id)).all()
    return [{"id":str(id_),"name":name or str(id_),"status":"active" if status=="active" and user_status=="active" else "disabled"}
        for id_,name,status,user_status in rows]


def validate_members(center,tenant_id,payload):
    members={m["id"]:m for m in member_options(center,tenant_id)}
    for key in ("purchaser_id","developer_id","inquiry_buyer_id"):
        if key in payload.model_fields_set and (value:=getattr(payload,key,None)):
            if str(value) not in members or members[str(value)]["status"]!="active":
                raise AppError(ErrorCode.VALIDATION_ERROR,"选择的人员不是本租户有效成员")
    return {id_:m["name"] for id_,m in members.items()}


def get_options(db,center,tenant_id):
    result={"suppliers":[{"id":str(s.id),"code":s.code,"name":s.name,"status":s.status}
        for s in db.execute(select(Supplier).where(Supplier.tenant_id==tenant_id).order_by(Supplier.id)).scalars()],
        "members":member_options(center,tenant_id),"brand":[],"category":[],"tag":[],"packaging":[]}
    for item in db.execute(select(CatalogOption).where(CatalogOption.tenant_id==tenant_id).order_by(CatalogOption.id)).scalars():
        result[item.kind].append({"id":str(item.id),"name":item.name,"parent_id":str(item.parent_id) if item.parent_id else None,"status":item.status})
    return result


def create_resource(db,tenant_id,body,actor_id):
    if isinstance(body,SupplierIn):
        row=Supplier(tenant_id=tenant_id,code=body.code,name=body.name,status="active")
        kind="supplier"
    else:
        if body.parent_id:
            parent=db.execute(select(CatalogOption).where(CatalogOption.tenant_id==tenant_id,CatalogOption.id==body.parent_id,CatalogOption.kind=="category",CatalogOption.status=="active")).scalar_one_or_none()
            if body.kind!="category" or parent is None:
                raise AppError(ErrorCode.VALIDATION_ERROR,"父分类不存在或不可用")
        row=CatalogOption(tenant_id=tenant_id,kind=body.kind,name=body.name,parent_id=body.parent_id,status="active")
        kind=body.kind
    try:
        db.add(row)
        db.flush()
        from app.modules.audit.service import record
        record(db,tenant_id=tenant_id,action="catalog.option.create",resource_type=kind,resource_id=row.id,actor_id=actor_id,summary=f"创建商品资料：{row.name}")
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("资料编号或名称已存在") from exc
    return {"id":str(row.id),"name":row.name,"status":row.status,**({"code":row.code} if kind=="supplier" else {"parent_id":str(row.parent_id) if row.parent_id else None})}


def image_type(data: bytes) -> tuple[str,str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data)>=24 and data[12:16]==b"IHDR":
        return "png","image/png"
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "jpg","image/jpeg"
    if len(data)>=16 and data[:4]==b"RIFF" and data[8:12]==b"WEBP":
        return "webp","image/webp"
    raise AppError(ErrorCode.VALIDATION_ERROR,"仅支持PNG、JPEG、WebP图片")


def save_image(db,tenant_id,actor_id,filename,data):
    if not data or len(data)>10*1024*1024:
        raise AppError(ErrorCode.VALIDATION_ERROR,"图片不能为空且不能超过10MB")
    extension,mime=image_type(data)
    object_key=f"catalog/{tenant_id}/{uuid4().hex}.{extension}"
    path=Path(settings.catalog_media_dir).resolve()/object_key
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        path.write_bytes(data)
        row=FileRecord(tenant_id=tenant_id,object_key=object_key,filename=(filename or "image")[:255],content_type=mime,size_bytes=len(data),uploaded_by=actor_id,status="active")
        db.add(row)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return {"id":str(row.id),"url":f"{settings.api_prefix}/tenants/{tenant_id}/products/media/{row.id}"}


def image_file(db,tenant_id,file_id):
    row=db.execute(select(FileRecord).where(FileRecord.tenant_id==tenant_id,FileRecord.id==file_id,FileRecord.status=="active")).scalar_one_or_none()
    if row is None or not row.object_key.startswith(f"catalog/{tenant_id}/"):
        raise AppError(ErrorCode.NOT_FOUND)
    root=Path(settings.catalog_media_dir).resolve()
    path=(root/row.object_key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise AppError(ErrorCode.NOT_FOUND)
    return path,row.content_type


def validate_media_refs(db,tenant_id,payload):
    urls=list(getattr(payload,"images",None) or [])
    urls.extend(s.image_url for s in getattr(payload,"skus",None) or [] if s.image_url)
    pattern=re.compile(re.escape(settings.api_prefix)+r"/tenants/(\d+)/products/media/(\d+)/?$")
    for url in urls:
        match=pattern.fullmatch(urlsplit(url).path)
        if match:
            if int(match[1])!=tenant_id:
                raise AppError(ErrorCode.VALIDATION_ERROR,"不能引用其他租户的商品图片")
            image_file(db,tenant_id,int(match[2]))
