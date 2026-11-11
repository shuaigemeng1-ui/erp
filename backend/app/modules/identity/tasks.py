"""租户开通的 Celery 任务（Worker 侧执行）。

* 平台路由只登记租户并投递本任务，HTTP 请求立即返回 ``202``；
* Worker 重新创建中心库 Session，执行 ``identity.provisioning`` 的流水线，
  每一步状态写入 ``provisioning_jobs``，失败不激活租户；
* 任务异常记录日志后重新抛出，Celery 结果可见（最终失败原因以 ``provisioning_jobs`` 为准）；
* 任务参数只有租户 ID 与操作人 ID，不携带数据库密码或凭据。

任务名（Lead 加入 ``app/workers/celery_app.py`` 的 include）：

* ``app.modules.identity.tasks.provision_tenant_job`` → 队列 ``default``
* ``app.modules.identity.tasks.retry_provision_job`` → 队列 ``default``
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.session import center_session
from app.modules.identity import provisioning
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

PROVISION_TENANT_TASK = "app.modules.identity.tasks.provision_tenant_job"
RETRY_PROVISION_TASK = "app.modules.identity.tasks.retry_provision_job"
IDENTITY_TASK_QUEUE = "default"
# 首版不自动重试：失败步骤由平台「重试开通」接口显式触发，避免长任务重复执行
IDENTITY_TASK_MAX_RETRIES = 0


@celery_app.task(
    name=PROVISION_TENANT_TASK,
    bind=True,
    queue=IDENTITY_TASK_QUEUE,
    max_retries=IDENTITY_TASK_MAX_RETRIES,
    acks_late=True,
)
def provision_tenant_job(self: Any, tenant_id: int, operator_user_id: int | None = None) -> dict[str, Any]:
    """执行新租户开通流水线（幂等，可重试）。"""
    logger.info("开始执行租户开通任务 tenant=%s operator=%s", tenant_id, operator_user_id)
    try:
        with center_session() as center:
            try:
                result = provisioning.provision_tenant(
                    center, int(tenant_id), operator_user_id=operator_user_id
                )
            except Exception:
                center.rollback()
                raise
    except Exception:
        logger.exception("租户开通任务异常 tenant=%s", tenant_id)
        raise
    payload = result.as_dict()
    if result.failed_step:
        logger.error(
            "租户开通失败 tenant=%s step=%s: %s", tenant_id, result.failed_step, result.error_message
        )
        # 失败原因已写入 provisioning_jobs；抛出让 Celery 任务状态可见 FAILURE
        raise RuntimeError(
            f"租户 {tenant_id} 开通失败于步骤 {result.failed_step}: {result.error_message}"
        )
    logger.info("租户开通任务完成 tenant=%s status=%s", tenant_id, payload.get("status"))
    return payload


@celery_app.task(
    name=RETRY_PROVISION_TASK,
    bind=True,
    queue=IDENTITY_TASK_QUEUE,
    max_retries=IDENTITY_TASK_MAX_RETRIES,
    acks_late=True,
)
def retry_provision_job(self: Any, tenant_id: int, operator_user_id: int | None = None) -> dict[str, Any]:
    """重试开通失败 / 中断的租户（已成功步骤跳过）。"""
    logger.info("开始重试租户开通 tenant=%s operator=%s", tenant_id, operator_user_id)
    try:
        with center_session() as center:
            try:
                result = provisioning.retry_provisioning(
                    center, int(tenant_id), operator_user_id=operator_user_id
                )
            except Exception:
                center.rollback()
                raise
    except Exception:
        logger.exception("租户开通重试任务异常 tenant=%s", tenant_id)
        raise
    payload = result.as_dict()
    if result.failed_step:
        logger.error(
            "租户开通重试失败 tenant=%s step=%s: %s",
            tenant_id,
            result.failed_step,
            result.error_message,
        )
        raise RuntimeError(
            f"租户 {tenant_id} 重试开通失败于步骤 {result.failed_step}: {result.error_message}"
        )
    logger.info("租户开通重试完成 tenant=%s status=%s", tenant_id, payload.get("status"))
    return payload


__all__ = [
    "IDENTITY_TASK_QUEUE",
    "PROVISION_TENANT_TASK",
    "RETRY_PROVISION_TASK",
    "provision_tenant_job",
    "retry_provision_job",
]
