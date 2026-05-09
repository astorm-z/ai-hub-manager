from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import httpx

from app.models import SyncTarget


class SyncClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


@dataclass(frozen=True)
class SyncClientResult:
    status_code: int
    data: Any
    raw: Any


def load_target_auth(target: SyncTarget) -> dict[str, Any]:
    try:
        auth = json.loads(target.auth_config_json or "{}")
    except json.JSONDecodeError as exc:
        raise SyncClientError("目标站点认证配置不是有效 JSON。") from exc

    if not isinstance(auth, dict):
        raise SyncClientError("目标站点认证配置必须是 JSON 对象。")

    return auth


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _items_from_paginated(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("items", [])
    else:
        return []

    return [item for item in raw_items if isinstance(item, dict)]


def _find_named_item(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in items:
        if item.get("name") == name:
            return item
    return None


class BaseTargetClient:
    timeout = 20.0

    def __init__(self, target: SyncTarget, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.target = target
        self.auth = load_target_auth(target)
        self.transport = transport

    def headers(self) -> dict[str, str]:
        return {}

    async def request(self, method: str, path: str, **kwargs: Any) -> SyncClientResult:
        url = _join_url(self.target.base_url, path)

        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.request(method, url, headers=self.headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise SyncClientError(f"请求目标站点失败：{exc}") from exc

        data = self.unwrap_response(response)
        return SyncClientResult(status_code=response.status_code, data=data, raw=response)

    def unwrap_response(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            raise SyncClientError(
                f"目标站点返回 HTTP {response.status_code}。",
                status_code=response.status_code,
                response=response.text[:500],
            )

        try:
            return response.json()
        except ValueError as exc:
            raise SyncClientError(
                "目标站点返回非 JSON 响应。",
                status_code=response.status_code,
                response=response.text[:500],
            ) from exc


class Sub2APIClient(BaseTargetClient):
    def headers(self) -> dict[str, str]:
        admin_api_key = self.auth.get("admin_api_key")
        if not admin_api_key:
            raise SyncClientError("Sub2API 目标缺少 admin_api_key。")
        return {"x-api-key": str(admin_api_key)}

    def unwrap_response(self, response: httpx.Response) -> Any:
        data = super().unwrap_response(response)
        if isinstance(data, dict) and data.get("code") != 0:
            message = data.get("message") or "Sub2API 目标返回错误。"
            raise SyncClientError(str(message), status_code=response.status_code, response=data)
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def test_connection(self) -> SyncClientResult:
        return await self.request("GET", "/api/v1/admin/accounts", params={"page": 1, "page_size": 20})

    async def find_account_by_name(self, name: str) -> dict[str, Any] | None:
        result = await self.request(
            "GET",
            "/api/v1/admin/accounts",
            params={"page": 1, "page_size": 20, "search": name},
        )
        return _find_named_item(_items_from_paginated(result.data), name)

    async def get_account(self, remote_id: str | int) -> dict[str, Any]:
        result = await self.request("GET", f"/api/v1/admin/accounts/{remote_id}")
        if not isinstance(result.data, dict):
            raise SyncClientError("Sub2API 账号响应格式无效。", status_code=result.status_code, response=result.data)
        return result.data

    async def create_account(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("POST", "/api/v1/admin/accounts", json=payload)

    async def update_account(self, remote_id: str | int, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("PUT", f"/api/v1/admin/accounts/{remote_id}", json=payload)


class NewAPIClient(BaseTargetClient):
    def headers(self) -> dict[str, str]:
        authorization = self.auth.get("authorization")
        new_api_user = self.auth.get("new_api_user")
        if not authorization:
            raise SyncClientError("New API 目标缺少 authorization。")
        if not new_api_user:
            raise SyncClientError("New API 目标缺少 new_api_user。")
        return {"Authorization": str(authorization), "New-Api-User": str(new_api_user)}

    def unwrap_response(self, response: httpx.Response) -> Any:
        data = super().unwrap_response(response)
        if isinstance(data, dict) and data.get("success") is False:
            message = data.get("message") or "New API 目标返回错误。"
            raise SyncClientError(str(message), status_code=response.status_code, response=data)
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def test_connection(self) -> SyncClientResult:
        return await self.request("GET", "/api/channel/", params={"p": 1, "page_size": 20})

    async def find_channel_by_name(self, name: str) -> dict[str, Any] | None:
        result = await self.request(
            "GET",
            "/api/channel/search",
            params={"keyword": name, "p": 1, "page_size": 20},
        )
        return _find_named_item(_items_from_paginated(result.data), name)

    async def get_channel(self, remote_id: str | int) -> dict[str, Any]:
        result = await self.request("GET", f"/api/channel/{remote_id}")
        if not isinstance(result.data, dict):
            raise SyncClientError("New API 频道响应格式无效。", status_code=result.status_code, response=result.data)
        return result.data

    async def create_channel(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("POST", "/api/channel/", json={"mode": "single", "channel": payload})

    async def update_channel(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("PUT", "/api/channel/", json=payload)


def client_for_target(
    target: SyncTarget,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BaseTargetClient:
    if target.target_type == "sub2api":
        return Sub2APIClient(target, transport=transport)
    if target.target_type == "new_api":
        return NewAPIClient(target, transport=transport)
    raise SyncClientError(f"未知同步目标类型：{target.target_type}")
