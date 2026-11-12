"""商品中心的租户资料选项和持久化图片；不依赖额外文件服务。"""
from __future__ import annotations
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import re
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.core.config import settings
from app.core.errors import AppError, ConflictError, ErrorCode
from app.modules.catalog.models_tenant import Supplier, CatalogOption, Product
from app.modules.identity.models_center import TenantMembership, User
from app.modules.audit.models_tenant import FileRecord


class SupplierIn(BaseModel):
    model_config=ConfigDict(str_strip_whitespace=True,extra="forbid")
    code: str=Field(min_length=1,max_length=64)
    name: str=Field(min_length=1,max_length=200)
    contact_name: str | None=Field(default=None,max_length=100)
    contact_details: str | None=Field(default=None,max_length=255)
    remark: str | None=Field(default=None,max_length=2000)

    @field_validator("contact_name","contact_details","remark")
    @classmethod
    def empty_to_none(cls,value):
        return value or None


class SupplierUpdate(SupplierIn):
    code: str | None=Field(default=None,min_length=1,max_length=64)
    name: str | None=Field(default=None,min_length=1,max_length=200)
    status: Literal["active","disabled"] | None=None

    @field_validator("code","name","status")
    @classmethod
    def required_when_supplied(cls,value):
        if value is None:
            raise ValueError("编号、名称和状态不能设为空")
        return value


class OptionIn(BaseModel):
    model_config=ConfigDict(str_strip_whitespace=True,extra="forbid")
    kind: Literal["brand","category","tag","packaging"]
    name: str=Field(min_length=1,max_length=100)
    parent_id: int | None=Field(default=None,gt=0)


class OptionUpdate(BaseModel):
    model_config=ConfigDict(str_strip_whitespace=True,extra="forbid")
    name: str | None=Field(default=None,min_length=1,max_length=100)
    parent_id: int | None=Field(default=None,gt=0)
    status: Literal["active","disabled"] | None=None

    @field_validator("name","status")
    @classmethod
    def required_when_supplied(cls,value):
        if value is None:
            raise ValueError("名称和状态不能设为空")
        return value


def member_options(center,tenant_id):
    rows=center.execute(select(TenantMembership.id,TenantMembership.member_no,User.display_name,TenantMembership.status,User.status)
        .join(User,User.id==TenantMembership.user_id).where(TenantMembership.tenant_id==tenant_id).order_by(User.display_name,TenantMembership.id)).all()
    return [{"id":str(id_),"member_no":str(member_no),"name":name or str(member_no),"status":"active" if status=="active" and user_status=="active" else "disabled"}
        for id_,member_no,name,status,user_status in rows]


def validate_members(center,tenant_id,payload):
    members={m["id"]:m for m in member_options(center,tenant_id)}
    for key in ("purchaser_id","developer_id","inquiry_buyer_id"):
        if key in payload.model_fields_set and (value:=getattr(payload,key,None)):
            if str(value) not in members or members[str(value)]["status"]!="active":
                raise AppError(ErrorCode.VALIDATION_ERROR,"选择的人员不是本租户有效成员")
    return {id_:m["name"] for id_,m in members.items()}


def get_options(db,center,tenant_id):
    result={"suppliers":[resource_payload(s)
        for s in db.execute(select(Supplier).where(Supplier.tenant_id==tenant_id).order_by(Supplier.id)).scalars()],
        "members":member_options(center,tenant_id),"brand":[],"category":[],"tag":[],"packaging":[]}
    for item in db.execute(select(CatalogOption).where(CatalogOption.tenant_id==tenant_id).order_by(CatalogOption.id)).scalars():
        result[item.kind].append({"id":str(item.id),"name":item.name,"parent_id":str(item.parent_id) if item.parent_id else None,"status":item.status})
    return result


def resource_payload(row):
    result={"id":str(row.id),"name":row.name,"status":row.status}
    if isinstance(row,Supplier):
        result.update(code=row.code,contact_name=row.contact_name,contact_details=row.contact_details,remark=row.remark)
    else:
        result["parent_id"]=str(row.parent_id) if row.parent_id else None
    return result


def create_resource(db,tenant_id,body,actor_id):
    if isinstance(body,SupplierIn):
        row=Supplier(tenant_id=tenant_id,**body.model_dump(),status="active")
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
    return resource_payload(row)


def _option_is_referenced(db,tenant_id,row):
    if row.kind=="tag":
        # JSON 数组按完整标签值判断，避免 LIKE 导致子串误判，兼容 MySQL 与测试 SQLite。
        return any(row.name in (tags or []) for tags in db.scalars(select(Product.feature_tags).where(Product.tenant_id==tenant_id)))
    column={"brand":Product.brand,"category":Product.category,"packaging":Product.packaging_name}[row.kind]
    return db.scalar(select(Product.id).where(Product.tenant_id==tenant_id,column==row.name).limit(1)) is not None


def _validate_category_parent(db,tenant_id,row,parent_id):
    if parent_id is None:
        return
    if row.kind!="category":
        raise AppError(ErrorCode.VALIDATION_ERROR,"只有分类可以设置父分类")
    seen={row.id}
    cursor=parent_id
    first=True
    while cursor is not None:
        if cursor in seen:
            raise AppError(ErrorCode.VALIDATION_ERROR,"父分类不能形成循环")
        seen.add(cursor)
        parent=db.execute(select(CatalogOption).where(CatalogOption.tenant_id==tenant_id,
            CatalogOption.id==cursor,CatalogOption.kind=="category").with_for_update()).scalar_one_or_none()
        if parent is None or (first and parent.status!="active"):
            raise AppError(ErrorCode.VALIDATION_ERROR,"父分类不存在或不可用")
        cursor=parent.parent_id
        first=False


def update_resource(db,tenant_id,resource_id,body,actor_id):
    model=Supplier if isinstance(body,SupplierUpdate) else CatalogOption
    row=db.execute(select(model).where(model.tenant_id==tenant_id,model.id==resource_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND,"商品基础资料不存在")
    changes={key:value for key,value in body.model_dump(exclude_unset=True).items() if getattr(row,key)!=value}
    if not changes:
        return resource_payload(row)
    if isinstance(row,CatalogOption):
        if "name" in changes and _option_is_referenced(db,tenant_id,row):
            raise ConflictError("该资料已被商品使用，不能修改名称")
        if "parent_id" in changes:
            _validate_category_parent(db,tenant_id,row,changes["parent_id"])
    before={key:getattr(row,key) for key in changes}
    kind="supplier" if isinstance(row,Supplier) else row.kind
    try:
        for key,value in changes.items():
            setattr(row,key,value)
        db.flush()
        from app.modules.audit.service import record
        record(db,tenant_id=tenant_id,action="catalog.option.update",resource_type=kind,
            resource_id=row.id,actor_id=actor_id,summary=f"修改商品资料：{row.name}",
            payload={"changes":[{"field":key,"old":before[key],"new":value} for key,value in changes.items()]})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("资料编号或名称已存在") from exc
    return resource_payload(row)


def image_type(data: bytes) -> tuple[str,str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data)>=24 and data[12:16]==b"IHDR":
        return "png","image/png"
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "jpg","image/jpeg"
    if len(data)>=16 and data[:4]==b"RIFF" and data[8:12]==b"WEBP":
        return "webp","image/webp"
    raise AppError(ErrorCode.VALIDATION_ERROR,"仅支持PNG、JPEG、WebP图片")


def save_image(db,tenant_id,actor_id,filename,data,*,center):
    if not data or len(data)>10*1024*1024:
        raise AppError(ErrorCode.VALIDATION_ERROR,"图片不能为空且不能超过10MB")
    extension,mime=image_type(data)
    from app.modules.storage.service import upload_default
    uploaded=upload_default(center,tenant_id,object_key=f"catalog/{tenant_id}/{uuid4().hex}.{extension}",data=data,content_type=mime)
    try:
        row=FileRecord(tenant_id=tenant_id,object_key=uploaded.object_key,filename=(filename or "image")[:255],
            content_type=mime,size_bytes=len(data),uploaded_by=actor_id,status="active",storage_profile_id=uploaded.profile_id,
            storage_provider=uploaded.provider,url=uploaded.url)
        db.add(row)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"id":str(row.id),"url":row.url}


def get_media_record(db,tenant_id,file_id):
    row=db.execute(select(FileRecord).where(FileRecord.tenant_id==tenant_id,FileRecord.id==file_id,FileRecord.status=="active")).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND)
    return row


def image_file(db,tenant_id,file_id):
    row=get_media_record(db,tenant_id,file_id)
    if row.storage_provider!="local" or not row.object_key.startswith(f"catalog/{tenant_id}/"):
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
            row=get_media_record(db,tenant_id,int(match[2]))
            if row.storage_provider=="local":
                image_file(db,tenant_id,int(match[2]))
        else:
            # 新图片必须由本租户上传登记；兼容本租户已有的历史外部图片。
            from app.modules.catalog.models_tenant import ProductImage,ProductSku
            uploaded=db.execute(select(FileRecord.id).where(FileRecord.tenant_id==tenant_id,FileRecord.url==url,FileRecord.status=="active").limit(1)).first()
            historical=db.execute(select(ProductImage.id).where(ProductImage.tenant_id==tenant_id,ProductImage.url==url).limit(1)).first()
            historical_sku=db.execute(select(ProductSku.id).where(ProductSku.tenant_id==tenant_id,ProductSku.image_url==url).limit(1)).first()
            if not (uploaded or historical or historical_sku):
                raise AppError(ErrorCode.VALIDATION_ERROR,"图片未在本租户登记，请先上传图片")
