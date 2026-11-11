"""平台管理路由（中心库）。

实现范围：

* 租户列表 / 创建 / 详情 / 重试开通 / 暂停 / 启用 / 归档
* 平台审计日志
* 套餐与额度
* 平台渠道应用（Shopee）凭据配置

实现负责人：backend-identity（平台侧复用 identity 模块模型）。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/platform", tags=["platform"])
