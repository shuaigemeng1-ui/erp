"""租户实例侧网关客户端（受限新链：实例令牌）。

租户实例不再持有网关 admin token：
* ``GATEWAY_INSTANCE_ID`` + ``GATEWAY_INSTANCE_TOKEN`` → ``/api/v1/gateway/instance/*``；
* 迁移期若未配置实例令牌，可由渠道服务按 ``GATEWAY_LEGACY_ADMIN_ENABLED`` 回退旧 admin token
  （本客户端只提供两种调用方法，回退决策在调用方）。

客户端不依赖 ``app.core``，便于网关独立包与租户后端共用；失败抛
:class:`GatewayCallError`，由调用方决定回退本地配置。
"""

from __future__ import annotations

from typing import Any

import httpx


class GatewayCallError(Exception):
    """网关调用失败（网络/HTTP 状态/响应格式）。"""

    def __init__(self, message: str, *, status_code: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise ValueError("gateway base_url 不能为空")
        self.timeout = float(timeout)
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(self.timeout))
        self._owns_client = http_client is None

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GatewayClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ 通用
    @staticmethod
    def _error_from_response(response: httpx.Response) -> GatewayCallError:
        code = ""
        message = f"网关返回 HTTP {response.status_code}"
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or "")
                    message = str(error.get("message") or message)
                elif isinstance(error, str):
                    code = error
        except ValueError:
            pass
        return GatewayCallError(message, status_code=response.status_code, code=code)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                params=params,
                json=json_body,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise GatewayCallError(f"网关不可达：{exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise self._error_from_response(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise GatewayCallError("网关返回非 JSON 响应", status_code=response.status_code) from exc
        if not isinstance(data, dict):
            raise GatewayCallError("网关返回格式不正确", status_code=response.status_code)
        return data

    @staticmethod
    def _instance_headers(instance_id: str, instance_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {instance_token}",
            "X-Gateway-Instance-Id": str(instance_id),
            "Accept": "application/json",
        }

    @staticmethod
    def _admin_headers(admin_token: str) -> dict[str, str]:
        return {"X-Gateway-Token": admin_token, "Accept": "application/json"}

    # ------------------------------------------------------------------ 新链（实例令牌）
    def fetch_instance_channel_app(
        self,
        *,
        instance_id: str,
        instance_token: str,
        channel: str,
        environment: str,
    ) -> dict[str, Any]:
        """读取本实例被授权的渠道应用配置（返回含 app_secret 的 dict）。"""
        return self._request(
            "GET",
            f"/api/v1/gateway/instance/channel-apps/{channel}/{environment}",
            headers=self._instance_headers(instance_id, instance_token),
        )

    def create_oauth_state(
        self,
        *,
        instance_id: str,
        instance_token: str,
        channel: str,
        environment: str,
        store_id: int | str,
        redirect_path: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/gateway/instance/oauth/states",
            headers=self._instance_headers(instance_id, instance_token),
            json_body={
                "channel": channel,
                "environment": environment,
                "store_id": int(store_id),
                "redirect_path": redirect_path,
            },
        )

    def acquire_refresh_lock(
        self,
        *,
        instance_id: str,
        instance_token: str,
        lock_key: str,
        owner: str,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"lock_key": lock_key, "owner": owner}
        if ttl_seconds is not None:
            body["ttl_seconds"] = int(ttl_seconds)
        return self._request(
            "POST",
            "/api/v1/gateway/instance/refresh-locks",
            headers=self._instance_headers(instance_id, instance_token),
            json_body=body,
        )

    def release_refresh_lock(
        self,
        *,
        instance_id: str,
        instance_token: str,
        lock_key: str,
        owner: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/gateway/instance/refresh-locks/{lock_key}/release",
            headers=self._instance_headers(instance_id, instance_token),
            json_body={"owner": owner},
        )

    # ------------------------------------------------------------------ 迁移期（旧 admin token）
    def fetch_legacy_channel_app(
        self, *, admin_token: str, channel: str, environment: str
    ) -> dict[str, Any]:
        """迁移期旧路径：受网关 legacy 开关限制，验证通过后应停用。"""
        return self._request(
            "GET",
            f"/api/v1/gateway/channel-apps/{channel}/{environment}/internal",
            headers=self._admin_headers(admin_token),
        )


__all__ = ["GatewayCallError", "GatewayClient"]
