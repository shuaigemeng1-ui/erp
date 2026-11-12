"""商品字段差异：与业务事务同提交，成本信息仅进入受权限保护的明细。"""
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import load_only
from app.modules.audit.models_tenant import AuditLog
from app.modules.catalog.models_tenant import Product, ProductSku, ProductImage, Supplier
from app.modules.catalog.identifiers import list_aliases
from app.modules.catalog.aggregates import aggregate_sales_status

COST_FIELDS = {"cost_amount", "cost_currency", "guidance_cost", "average_cost"}
LABELS = {
    "code":"SPU编号", "name":"商品中文名称", "name_en":"商品英文名称", "brand":"品牌", "category":"分类",
    "preferred_supplier_id":"首选供应商", "purchaser_id":"采购员", "developer_id":"业务开发员", "inquiry_buyer_id":"采购询价员",
    "hs_code":"海关编码", "material_zh":"中文材质", "material_en":"英文材质", "usage":"用途", "feature_tags":"特性标签",
    "packaging_name":"包材名称", "length_cm":"商品长度(cm)", "width_cm":"商品宽度(cm)", "height_cm":"商品高度(cm)",
    "package_length_cm":"包装后长度(cm)", "package_width_cm":"包装后宽度(cm)", "package_height_cm":"包装后高度(cm)",
    "sales_type":"销售类型", "remark":"备注", "features":"产品特点", "description":"商品描述", "base_price":"基础售价",
    "sku_code":"SKU编号", "image_url":"图片", "images":"图片", "aliases":"别名", "specs":"规格", "price":"售价",
    "cost_amount":"当前成本", "guidance_cost":"指导成本", "average_cost":"平均成本", "cost_currency":"成本币种",
    "weight_g":"重量(g)", "sales_status":"销售状态", "status":"归档状态",
}
ENUM_NAMES = {"on_sale":"在售", "discontinued":"停售", "trial":"试卖", "clearance":"清仓",
    "partial_clearance":"部分清仓", "partial_discontinued":"部分停售", "active":"未归档", "archived":"已归档",
    "normal":"普通销售", "variation":"变参销售", "bundle":"捆绑销售", "assembly":"组装产品"}


def json_value(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dict):
        return {str(k): json_value(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [json_value(v) for v in value]
    return value


def snapshot(db, tenant_id: int, product: Product, *, include_cost: bool) -> dict:
    def fields(row):
        return {c.name: json_value(getattr(row,c.name)) for c in row.__table__.columns
                if c.name not in {"id","tenant_id","product_id","created_at","updated_at","version"}
                and (include_cost or c.name not in COST_FIELDS)}
    stmt=select(ProductSku).where(ProductSku.tenant_id==tenant_id,ProductSku.product_id==product.id).order_by(ProductSku.id)
    if not include_cost:
        stmt=stmt.options(load_only(*(getattr(ProductSku,c.name) for c in ProductSku.__table__.columns if c.name not in COST_FIELDS)))
    skus=list(db.execute(stmt).scalars())
    aliases=list_aliases(db,tenant_id,[s.id for s in skus])
    base=fields(product)
    supplier=db.get(Supplier,product.preferred_supplier_id) if product.preferred_supplier_id else None
    if supplier and supplier.tenant_id==tenant_id:
        base["preferred_supplier_id"]={"id":str(supplier.id),"name":supplier.name}
    base["images"]=[i.url for i in db.execute(select(ProductImage).where(ProductImage.tenant_id==tenant_id,ProductImage.product_id==product.id,ProductImage.sku_id.is_(None)).order_by(ProductImage.sort_order,ProductImage.id)).scalars()]
    base["sales_status"]=aggregate_sales_status(skus)
    result={"product":base,"skus":{}}
    for sku in skus:
        data=fields(sku)
        data["aliases"]=sorted(a["identifier"] for a in aliases.get(sku.id,[]) if a["is_active"])
        result["skus"][str(sku.id)]=data
    return result


def differences(before: dict, after: dict) -> list[dict]:
    changes=[]
    def compare(old, new, *, entity, sku_id=None):
        for field in sorted(set(old)|set(new)):
            if field=="specs":
                old_specs,new_specs=old.get(field) or {},new.get(field) or {}
                for key in sorted(set(old_specs)|set(new_specs)):
                    if old_specs.get(key)!=new_specs.get(key):
                        changes.append({"entity":entity,"sku_id":sku_id,"sku_code":new.get("sku_code") or old.get("sku_code"),
                            "field":"specs."+key,"label":"规格·"+key,"old":old_specs.get(key),"new":new_specs.get(key),"sensitive":False})
                continue
            if old.get(field)==new.get(field):
                continue
            old_value,new_value=old.get(field),new.get(field)
            if field in {"sales_status","status","sales_type"}:
                old_value=ENUM_NAMES.get(old_value,old_value)
                new_value=ENUM_NAMES.get(new_value,new_value)
            changes.append({"entity":entity,"sku_id":sku_id,"sku_code":new.get("sku_code") or old.get("sku_code"),
                "field":field,"label":LABELS.get(field,field),"old":old_value,"new":new_value,"sensitive":field in COST_FIELDS})
    compare(before.get("product",{}),after.get("product",{}),entity="spu")
    for sku_id in sorted(set(before.get("skus",{}))|set(after.get("skus",{}))):
        compare(before.get("skus",{}).get(sku_id,{}),after.get("skus",{}).get(sku_id,{}),entity="sku",sku_id=sku_id)
    return changes


def record_changes(db, tenant_id, product, before, after, audit, *, action="catalog.product.update"):
    changes=differences(before,after)
    if not changes:
        return False
    for change in changes:
        if change["field"] in {"purchaser_id","developer_id","inquiry_buyer_id"}:
            for side in ("old","new"):
                value=change[side]
                if value is not None:
                    change[side]={"id":str(value),"name":audit.member_names.get(str(value),str(value))}
    from app.modules.audit.service import record
    record(db,tenant_id=tenant_id,action=action,resource_type="product",resource_id=product.id,
        product_id=product.id,actor_id=audit.actor_id,actor_name_snapshot=audit.actor_name or ("系统" if not audit.actor_id else str(audit.actor_id)),
        summary=f"{product.code}：修改 {len(changes)} 项资料",payload={"changes":changes},request_id=audit.request_id,ip=audit.ip)
    return True
