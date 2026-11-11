"""Outbox 投递任务。

后台任务通过 Outbox 模式投递：业务事务内写 ``outbox_events``，
独立 Worker 轮询后投递到 Celery 队列，避免事务与消息不一致。

实现负责人：backend-sales（tasks 模块）。
"""

from __future__ import annotations
