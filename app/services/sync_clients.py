from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import urlsplit

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
    normalized_base_url = _normalize_base_url(base_url)
    return f"{normalized_base_url}/{path.lstrip('/')}"


def _normalize_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SyncClientError("Base URL 无效，请填写类似 https://target.example.com 的完整地址。")
    return normalized


def _items_from_paginated(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("items", [])
    else:
        return []

    return [item for item in raw_items if isinstance(item, dict)]


def _has_more_pages(data: Any, page: int, page_size: int, item_count: int) -> bool:
    if isinstance(data, dict) and isinstance(data.get("pages"), int):
        return page < data["pages"]
    return item_count >= page_size


def _find_named_item(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in items:
        if item.get("name") == name:
            return item
    return None


def _clean_group_name(value: Any) -> str:
    return str(value or "").strip()


def _dedupe_group_records(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for group in groups:
        value = str(group.get("value") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(group)
    return unique


class BaseTargetClient:
    timeout = 20.0
    max_pages = 100

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
        admin_api_key = str(self.auth.get("admin_api_key") or "").strip()
        if not admin_api_key:
            raise SyncClientError("Sub2API 目标缺少 admin_api_key。")
        return {"x-api-key": admin_api_key}

    def unwrap_response(self, response: httpx.Response) -> Any:
        data = super().unwrap_response(response)
        if isinstance(data, dict) and "code" in data and data.get("code") != 0:
            message = data.get("message") or "Sub2API 目标返回错误。"
            raise SyncClientError(str(message), status_code=response.status_code, response=data)
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def test_connection(self) -> SyncClientResult:
        return await self.request("GET", "/api/v1/admin/accounts", params={"page": 1, "page_size": 1})

    async def list_groups(self, platform: str | None = None) -> list[dict[str, Any]]:
        params = {"platform": platform} if platform else None
        result = await self.request("GET", "/api/v1/admin/groups/all", params=params)
        raw_groups = result.data
        if isinstance(raw_groups, dict) and isinstance(raw_groups.get("items"), list):
            raw_groups = raw_groups["items"]
        if not isinstance(raw_groups, list):
            raise SyncClientError("Sub2API 分组响应格式无效。", status_code=result.status_code, response=result.data)

        groups: list[dict[str, Any]] = []
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            group_id = item.get("id")
            if group_id is None:
                continue
            name = _clean_group_name(item.get("name")) or str(group_id)
            group_platform = _clean_group_name(item.get("platform"))
            status = _clean_group_name(item.get("status"))
            label_parts = [name]
            meta = [value for value in (group_platform, status if status and status != "active" else "") if value]
            if meta:
                label_parts.append(f"（{'，'.join(meta)}）")
            groups.append(
                {
                    "id": group_id,
                    "value": str(group_id),
                    "name": name,
                    "label": "".join(label_parts),
                    "platform": group_platform,
                    "status": status,
                }
            )
        return _dedupe_group_records(groups)

    async def find_account_by_name(self, name: str) -> dict[str, Any] | None:
        page_size = 20
        for page in range(1, self.max_pages + 1):
            result = await self.request(
                "GET",
                "/api/v1/admin/accounts",
                params={"page": page, "page_size": page_size, "search": name},
            )
            items = _items_from_paginated(result.data)
            found = _find_named_item(items, name)
            if found is not None:
                return found
            if not _has_more_pages(result.data, page, page_size, len(items)):
                return None
        return None

    async def get_account(self, remote_id: str | int) -> dict[str, Any]:
        result = await self.request("GET", f"/api/v1/admin/accounts/{remote_id}")
        if not isinstance(result.data, dict):
            raise SyncClientError("Sub2API 账号响应格式无效。", status_code=result.status_code, response=result.data)
        return result.data

    async def create_account(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("POST", "/api/v1/admin/accounts", json=payload)

    async def update_account(self, remote_id: str | int, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("PUT", f"/api/v1/admin/accounts/{remote_id}", json=payload)

    async def delete_account(self, remote_id: str | int) -> SyncClientResult:
        return await self.request("DELETE", f"/api/v1/admin/accounts/{remote_id}")


class NewAPIClient(BaseTargetClient):
    def headers(self) -> dict[str, str]:
        authorization = str(self.auth.get("authorization") or "").strip()
        new_api_user = str(self.auth.get("new_api_user") or "").strip()
        if not authorization:
            raise SyncClientError("New API 目标缺少 authorization。")
        if not new_api_user:
            raise SyncClientError("New API 目标缺少 new_api_user。")
        return {"Authorization": authorization, "New-Api-User": new_api_user}

    def unwrap_response(self, response: httpx.Response) -> Any:
        data = super().unwrap_response(response)
        if isinstance(data, dict) and data.get("success") is False:
            message = data.get("message") or "New API 目标返回错误。"
            raise SyncClientError(str(message), status_code=response.status_code, response=data)
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def test_connection(self) -> SyncClientResult:
        return await self.request("GET", "/api/channel/", params={"p": 1, "page_size": 1})

    async def list_groups(self) -> list[dict[str, str]]:
        result = await self.request("GET", "/api/group/")
        if not isinstance(result.data, list):
            raise SyncClientError("New API 分组响应格式无效。", status_code=result.status_code, response=result.data)

        groups: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in result.data:
            name = _clean_group_name(item)
            if not name or name in seen:
                continue
            seen.add(name)
            groups.append({"name": name, "value": name, "label": name})
        return groups

    async def find_channel_by_name(self, name: str) -> dict[str, Any] | None:
        page_size = 20
        for page in range(1, self.max_pages + 1):
            result = await self.request(
                "GET",
                "/api/channel/search",
                params={"keyword": name, "p": page, "page_size": page_size},
            )
            items = _items_from_paginated(result.data)
            found = _find_named_item(items, name)
            if found is not None:
                return found
            if not _has_more_pages(result.data, page, page_size, len(items)):
                return None
        return None

    async def get_channel(self, remote_id: str | int) -> dict[str, Any]:
        result = await self.request("GET", f"/api/channel/{remote_id}")
        if not isinstance(result.data, dict):
            raise SyncClientError("New API 频道响应格式无效。", status_code=result.status_code, response=result.data)
        return result.data

    async def create_channel(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("POST", "/api/channel/", json={"mode": "single", "channel": payload})

    async def update_channel(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("PUT", "/api/channel/", json=payload)

    async def delete_channel(self, remote_id: str | int) -> SyncClientResult:
        return await self.request("DELETE", f"/api/channel/{remote_id}")


def client_for_target(
    target: SyncTarget,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BaseTargetClient:
    if target.target_type == "sub2api":
        return Sub2APIClient(target, transport=transport)
    if target.target_type == "new_api":
        return NewAPIClient(target, transport=transport)
    raise SyncClientError(f"未知同步目标类型：{target.target_type}")
