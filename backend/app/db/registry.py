"""模型元数据注册表。

Alembic 的 ``env.py`` 与校验脚本从这里导入中心库 / 租户库的完整元数据。
各模块新增模型文件后必须在此登记，否则不会进入迁移与校验。
"""

from __future__ import annotations

from sqlalchemy import MetaData

from app.db.base import CenterBase, TenantBase

# 导入各模块模型以填充 metadata（顺序无关，但必须全部登记）
from app.modules.identity import models_center as _identity_center  # noqa: F401,E402
from app.modules.identity import models_tenant as _identity_tenant  # noqa: F401,E402
from app.modules.platform import models_center as _platform_center  # noqa: F401,E402
from app.modules.access import models_tenant as _access_tenant  # noqa: F401,E402
from app.modules.catalog import models_tenant as _catalog_tenant  # noqa: F401,E402
from app.modules.inventory import models_tenant as _inventory_tenant  # noqa: F401,E402
from app.modules.channels import models_center as _channels_center  # noqa: F401,E402
from app.modules.channels import models_tenant as _channels_tenant  # noqa: F401,E402
from app.modules.sales import models_tenant as _sales_tenant  # noqa: F401,E402
from app.modules.tasks import models_tenant as _tasks_tenant  # noqa: F401,E402
from app.modules.audit import models_tenant as _audit_tenant  # noqa: F401,E402

center_metadata: MetaData = CenterBase.metadata
tenant_metadata: MetaData = TenantBase.metadata

__all__ = ["center_metadata", "tenant_metadata"]
