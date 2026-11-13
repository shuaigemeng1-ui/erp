"""管理中心 HTTP 客户端（仅标准库，可注入 transport 便于自测）。

* 节点认证：``Authorization: Bearer <node_token>``，不使用浏览器会话；
* 协议协商：附带 ``X-Agent-Protocol`` / ``X-Agent-Version`` 头，后端可按版本决定是否下发变更任务；
* 错误分类：
  - ``EndpointUnsupported``：新接口尚未在后端上线（404/405/501），客户端降级继续运行；
  - ``NetworkError``：超时、连接失败等可重试错误；
  - ``CenterError``：其它 HTTP 业务错误（4xx 不重试，5xx/429 可重试）。
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .constants import AGENT_VERSION, EXECUTOR_VERSION, PROTOCOL_VERSION


@dataclass
class CenterError(Exception):
    """管理中心返回的业务错误。"""

    status: int
    message: str
    body: Any = None
    path: str = ""

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"中心接口错误 {self.status} {self.path}: {self.message}"


class NetworkError(Exception):
    """网络不可达 / 超时 / 响应无法解析，可重试。"""


class EndpointUnsupported(Exception):
    """新协议接口尚未实现（404/405/501），客户端按降级逻辑继续。"""


class Transport(Protocol):
    """最小 transport 协议：测试注入假实现，生产使用 urllib。"""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]: ...


class UrllibTransport:
    """基于 urllib 的默认 transport（HTTPS 使用系统 CA）。"""

    def __init__(self) -> None:
        self._ssl_context = ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self._ssl_context) as response:
                return int(getattr(response, "status", 200)), response.read()
        except urllib.error.HTTPError as exc:  # 4xx/5xx 需要拿到响应体
            try:
                payload = exc.read()
            except OSError:  # pragma: no cover - 读取失败时保留空体
                payload = b""
            return int(exc.code), payload
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise NetworkError(str(exc)) from exc


def _unsupported_status(status: int) -> bool:
    return status in (404, 405, 501)


class CenterClient:
    """管理中心 API 客户端（无状态、线程安全：每次请求独立构造 Request）。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: Transport | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self.transport: Transport = transport or UrllibTransport()
        self._log = logger or (lambda message: None)

    # ------------------------------------------------------------------ 基础
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"erp-agent/{AGENT_VERSION}",
            "X-Agent-Version": AGENT_VERSION,
            "X-Agent-Protocol": str(PROTOCOL_VERSION),
            "X-Executor-Version": EXECUTOR_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        status, raw = self.transport.request(
            method,
            url,
            headers=self._headers(),
            body=body,
            timeout=float(timeout or self.timeout),
        )
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if 200 <= status < 300:
            if not text.strip():
                return {}
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise NetworkError(f"中心响应不是合法 JSON: {exc}") from exc
            return data if isinstance(data, dict) else {"data": data}
        message = text[:300]
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict) and error.get("message"):
                    message = str(error["message"])[:300]
        except json.JSONDecodeError:
            pass
        raise CenterError(status=status, message=message, body=text[:1000], path=path)

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_unsupported: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST JSON；``allow_unsupported=True`` 时把 404/405/501 转为 EndpointUnsupported。"""
        try:
            return self._request("POST", path, payload or {}, timeout=timeout)
        except CenterError as exc:
            if allow_unsupported and _unsupported_status(exc.status):
                raise EndpointUnsupported(f"{path} 未实现（HTTP {exc.status}）") from exc
            raise

    def get(
        self,
        path: str,
        *,
        allow_unsupported: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            return self._request("GET", path, None, timeout=timeout)
        except CenterError as exc:
            if allow_unsupported and _unsupported_status(exc.status):
                raise EndpointUnsupported(f"{path} 未实现（HTTP {exc.status}）") from exc
            raise

    # ------------------------------------------------------------------ 业务接口
    def enroll(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/v1/agent/enroll", payload)

    def telemetry(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/v1/agent/telemetry", payload)

    def telemetry_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """批量指标（新协议，单条旧格式仍兼容；未上线时抛 EndpointUnsupported）。"""
        return self.post("/api/v1/agent/telemetry", payload, allow_unsupported=True)

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Agent 独立心跳（新协议，后端未上线时降级）。"""
        return self.post("/api/v1/agent/heartbeat", payload, allow_unsupported=True)

    def ping(self) -> dict[str, Any]:
        return self.get("/api/v1/agent/ping", allow_unsupported=True)

    def poll_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/v1/agent/jobs/poll", payload)

    def job_event(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/api/v1/agent/jobs/{job_id}/events", payload)

    def job_heartbeat(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """任务租约续租（新协议，后端未上线时返回 EndpointUnsupported）。"""
        return self.post(
            f"/api/v1/agent/jobs/{job_id}/heartbeat", payload, allow_unsupported=True
        )

    def job_reconcile(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """执行器重启 / unknown 后与服务端核对（新协议，未上线时返回 EndpointUnsupported）。"""
        return self.post(
            f"/api/v1/agent/jobs/{job_id}/reconcile", payload, allow_unsupported=True
        )


def data_of(response: dict[str, Any]) -> dict[str, Any]:
    """兼容 ``{success, data}`` 与裸对象两种响应形态。"""
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    if isinstance(data, dict):
        return data
    return response
