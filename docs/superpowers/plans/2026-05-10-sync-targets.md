# 多目标站点渠道同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为本站渠道增加多个 `sub2api` / `new-api` 目标站点导入、关联和保存后自动同步能力。

**Architecture:** 新增目标站点、渠道同步关联和同步事件三类持久化模型；用纯 payload builder 隔离字段映射；用目标站点 HTTP client 统一鉴权和响应解析；由 `channel_sync` 服务协调首次导入、同名拦截、手动同步和保存后自动同步。UI 延续当前 FastAPI + Jinja2 服务端渲染模式。

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, Jinja2, httpx, pytest, uv。

---

## File Structure

- Modify `app/models.py`: add `SyncTarget`, `ChannelSyncLink`, `SyncEvent`; add `Channel.sync_links` relationship.
- Create `app/services/sync_payloads.py`: pure helpers for remote name, model list, `sub2api` group ID parsing, payload generation, and log redaction.
- Create `app/services/sync_clients.py`: async HTTP clients for `sub2api` and `new-api`, including auth headers, response envelope parsing, same-name lookup, create/update, and connection test.
- Create `app/services/channel_sync.py`: orchestration service for first import, automatic update, manual update, link state, target delete guard, and sync event logging.
- Modify `app/main.py`: add routes for sync targets and channel sync links; wire automatic sync into channel save and model refresh.
- Modify `app/templates/base.html`: add sidebar link to sync target management.
- Create `app/templates/sync_targets.html`: list/create/edit sync targets in the existing panel/table style.
- Create `app/templates/partials/channel_sync_panel.html`: render channel detail sync links and import form.
- Modify `app/templates/channel_detail.html`: include the channel sync panel.
- Create `tests/test_sync_models.py`: persistence tests for new models and local cascade behavior.
- Create `tests/test_sync_payloads.py`: unit tests for mapping, parsing, and redaction.
- Create `tests/test_sync_clients.py`: mock-transport tests for target auth headers and response parsing.
- Create `tests/test_channel_sync.py`: service tests for conflict blocking, successful import, failed update status, and manual retry.
- Create `tests/test_sync_forms.py`: route helper tests for target auth config and `sub2api` group ID validation.
- Modify `README.md`: document sync target purpose, target auth fields, and sync semantics.

---

### Task 1: Persistence Models

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_sync_models.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_sync_models.py`:

```python
import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Channel, ChannelSyncLink, SyncEvent, SyncTarget


def test_sync_target_link_and_event_persist(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(
        name="sub2api-main",
        target_type="sub2api",
        base_url="https://sub2api.test",
        name_prefix="union_",
        auth_config_json=json.dumps({"admin_api_key": "admin-key"}),
    )
    db_session.add_all([channel, target])
    db_session.commit()

    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=target.id,
        remote_type="account",
        remote_id="42",
        remote_name="union_c1",
        sub2api_group_ids_json="[1, 2]",
        sub2api_priority=50,
        sub2api_concurrency=3,
        last_sync_status="success",
    )
    db_session.add(link)
    db_session.commit()

    event = SyncEvent(
        channel_id=channel.id,
        target_id=target.id,
        link_id=link.id,
        action="import_create",
        success=True,
        status_code=200,
        message="created",
        request_json="{}",
        response_json="{}",
    )
    db_session.add(event)
    db_session.commit()

    stored = db_session.query(ChannelSyncLink).one()
    assert stored.channel.name == "c1"
    assert stored.target.name == "sub2api-main"
    assert stored.sub2api_priority == 50
    assert db_session.query(SyncEvent).one().message == "created"


def test_channel_target_pair_is_unique(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(name="new-api", target_type="new_api", base_url="https://newapi.test", auth_config_json="{}")
    db_session.add_all([channel, target])
    db_session.commit()

    db_session.add(ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="channel"))
    db_session.commit()

    db_session.add(ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="channel"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_deleting_channel_removes_local_links_not_events(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(name="new-api", target_type="new_api", base_url="https://newapi.test", auth_config_json="{}")
    db_session.add_all([channel, target])
    db_session.commit()

    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="channel", remote_id="10")
    db_session.add(link)
    db_session.commit()
    db_session.add(SyncEvent(channel_id=channel.id, target_id=target.id, link_id=link.id, action="manual_update", success=True))
    db_session.commit()

    db_session.delete(channel)
    db_session.commit()

    assert db_session.query(ChannelSyncLink).count() == 0
    assert db_session.query(SyncEvent).count() == 1
```

- [ ] **Step 2: Run model tests and verify they fail**

Run:

```powershell
uv run pytest tests/test_sync_models.py -q
```

Expected: FAIL with import errors for `SyncTarget`, `ChannelSyncLink`, or `SyncEvent`.

- [ ] **Step 3: Add SQLAlchemy models**

Modify `app/models.py` imports:

```python
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
```

Add relationship to `Channel`:

```python
    sync_links: Mapped[list["ChannelSyncLink"]] = relationship(cascade="all, delete-orphan", back_populates="channel")
```

Add models after `NotificationChannel`:

```python
class SyncTarget(Base):
    __tablename__ = "sync_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    name_prefix: Mapped[str] = mapped_column(String(80), default="union_", nullable=False)
    auth_config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    default_config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc, nullable=False)

    sync_links: Mapped[list["ChannelSyncLink"]] = relationship(cascade="all, delete-orphan", back_populates="target")


class ChannelSyncLink(Base):
    __tablename__ = "channel_sync_links"
    __table_args__ = (UniqueConstraint("channel_id", "target_id", name="uq_channel_sync_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    target_id: Mapped[int] = mapped_column(ForeignKey("sync_targets.id"), nullable=False)
    remote_type: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    remote_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sub2api_group_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    sub2api_priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    sub2api_concurrency: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    last_sync_status: Mapped[str] = mapped_column(String(32), default="never", nullable=False)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc, nullable=False)

    channel: Mapped[Channel] = relationship(back_populates="sync_links")
    target: Mapped[SyncTarget] = relationship(back_populates="sync_links")


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"), nullable=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("sync_targets.id", ondelete="SET NULL"), nullable=True)
    link_id: Mapped[int | None] = mapped_column(ForeignKey("channel_sync_links.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
```

- [ ] **Step 4: Run model tests and verify they pass**

Run:

```powershell
uv run pytest tests/test_sync_models.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit persistence models**

Run:

```powershell
git add app/models.py tests/test_sync_models.py
git commit -m "feat: add sync target persistence models"
```

---

### Task 2: Payload Builders

**Files:**
- Create: `app/services/sync_payloads.py`
- Test: `tests/test_sync_payloads.py`

- [ ] **Step 1: Write failing payload tests**

Create `tests/test_sync_payloads.py`:

```python
import json

import pytest

from app.models import Channel, ChannelSyncLink, SyncTarget
from app.services.sync_payloads import (
    build_model_mapping,
    build_newapi_channel_payload,
    build_remote_name,
    build_sub2api_account_payload,
    parse_group_ids,
    redact_sensitive,
)


def test_build_remote_name_uses_target_prefix():
    target = SyncTarget(name="target", target_type="new_api", base_url="https://newapi.test", name_prefix="union_")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="secret")

    assert build_remote_name(target, channel) == "union_main"


def test_newapi_payload_maps_openai_channel():
    channel = Channel(
        id=7,
        name="main",
        provider_type="openai",
        base_url="https://upstream.test",
        api_key="sk-live",
        enabled=True,
        probe_model="gpt-4o",
    )

    payload = build_newapi_channel_payload(channel, ["gpt-4", "gpt-4o"], remote_name="union_main", remote_id="42")

    assert payload["id"] == 42
    assert payload["name"] == "union_main"
    assert payload["type"] == 1
    assert payload["key"] == "sk-live"
    assert payload["base_url"] == "https://upstream.test"
    assert payload["models"] == "gpt-4,gpt-4o"
    assert payload["test_model"] == "gpt-4o"
    assert payload["status"] == 1
    assert payload["group"] == "default"


def test_newapi_payload_maps_claude_disabled_channel():
    channel = Channel(name="claude", provider_type="claude", base_url="https://claude.test", api_key="ak", enabled=False)

    payload = build_newapi_channel_payload(channel, [], remote_name="union_claude")

    assert payload["type"] == 14
    assert payload["models"] == ""
    assert payload["status"] == 2


def test_sub2api_payload_maps_account_fields():
    channel = Channel(
        name="main",
        provider_type="openai",
        base_url="https://upstream.test",
        api_key="sk-live",
        enabled=True,
    )
    link = ChannelSyncLink(sub2api_group_ids_json="[1, 2]", sub2api_priority=60, sub2api_concurrency=5)

    payload = build_sub2api_account_payload(channel, ["gpt-4", "gpt-4o"], link, remote_name="union_main")

    assert payload["name"] == "union_main"
    assert payload["platform"] == "openai"
    assert payload["type"] == "apikey"
    assert payload["credentials"]["api_key"] == "sk-live"
    assert payload["credentials"]["base_url"] == "https://upstream.test"
    assert payload["credentials"]["model_mapping"] == {"gpt-4": "gpt-4", "gpt-4o": "gpt-4o"}
    assert payload["group_ids"] == [1, 2]
    assert payload["priority"] == 60
    assert payload["concurrency"] == 5
    assert payload["status"] == "active"


def test_sub2api_payload_maps_claude_disabled_account():
    channel = Channel(name="claude", provider_type="claude", base_url="https://claude.test", api_key="ak", enabled=False)
    link = ChannelSyncLink(sub2api_group_ids_json="[]", sub2api_priority=50, sub2api_concurrency=3)

    payload = build_sub2api_account_payload(channel, [], link, remote_name="union_claude")

    assert payload["platform"] == "anthropic"
    assert payload["credentials"]["model_mapping"] == {}
    assert payload["status"] == "disabled"


def test_group_id_parser_accepts_commas_and_json():
    assert parse_group_ids("1, 2,3") == [1, 2, 3]
    assert parse_group_ids("[4, 5]") == [4, 5]
    assert parse_group_ids("") == []
    assert parse_group_ids(None) == []


def test_group_id_parser_rejects_invalid_values():
    with pytest.raises(ValueError, match="分组 ID"):
        parse_group_ids("1,a")


def test_build_model_mapping_sorts_and_deduplicates():
    assert build_model_mapping(["b", "a", "a"]) == {"a": "a", "b": "b"}


def test_redact_sensitive_masks_nested_secrets():
    raw = {
        "authorization": "Bearer token",
        "credentials": {"api_key": "sk-live", "base_url": "https://example.test"},
        "items": [{"key": "secret"}, {"admin_api_key": "admin-secret"}],
    }

    redacted = redact_sensitive(raw)

    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["credentials"]["api_key"] == "[REDACTED]"
    assert redacted["credentials"]["base_url"] == "https://example.test"
    assert redacted["items"][0]["key"] == "[REDACTED]"
    assert redacted["items"][1]["admin_api_key"] == "[REDACTED]"
    json.dumps(redacted)
```

- [ ] **Step 2: Run payload tests and verify they fail**

Run:

```powershell
uv run pytest tests/test_sync_payloads.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.sync_payloads'`.

- [ ] **Step 3: Implement payload builders**

Create `app/services/sync_payloads.py`:

```python
from __future__ import annotations

import json
from typing import Any

from app.models import Channel, ChannelSyncLink, SyncTarget


NEW_API_OPENAI_TYPE = 1
NEW_API_ANTHROPIC_TYPE = 14
SENSITIVE_KEYS = {"api_key", "key", "authorization", "admin_api_key"}


def build_remote_name(target: SyncTarget, channel: Channel) -> str:
    return f"{target.name_prefix or 'union_'}{channel.name}"


def build_model_mapping(model_ids: list[str]) -> dict[str, str]:
    return {model_id: model_id for model_id in sorted({item.strip() for item in model_ids if item.strip()})}


def parse_group_ids(value: str | None) -> list[int]:
    if value is None or not str(value).strip():
        return []
    raw = str(value).strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"分组 ID JSON 无效：{exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError("分组 ID 必须是数组或逗号分隔整数。")
        values = parsed
    else:
        values = [part.strip() for part in raw.split(",") if part.strip()]
    result: list[int] = []
    for item in values:
        try:
            group_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("分组 ID 必须是整数。") from exc
        if group_id <= 0:
            raise ValueError("分组 ID 必须大于 0。")
        result.append(group_id)
    return result


def dumps_group_ids(group_ids: list[int]) -> str:
    return json.dumps(group_ids, ensure_ascii=False)


def provider_to_newapi_type(provider_type: str) -> int:
    if provider_type == "claude":
        return NEW_API_ANTHROPIC_TYPE
    return NEW_API_OPENAI_TYPE


def provider_to_sub2api_platform(provider_type: str) -> str:
    if provider_type == "claude":
        return "anthropic"
    return "openai"


def build_newapi_channel_payload(
    channel: Channel,
    model_ids: list[str],
    *,
    remote_name: str,
    remote_id: str | int | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(existing or {})
    if remote_id not in (None, ""):
        payload["id"] = int(remote_id)
    payload.update(
        {
            "name": remote_name,
            "type": provider_to_newapi_type(channel.provider_type),
            "key": channel.api_key,
            "base_url": channel.base_url,
            "models": ",".join(sorted({model_id for model_id in model_ids if model_id})),
            "test_model": channel.probe_model or "",
            "status": 1 if channel.enabled else 2,
            "group": payload.get("group") or "default",
        }
    )
    return payload


def build_sub2api_account_payload(
    channel: Channel,
    model_ids: list[str],
    link: ChannelSyncLink,
    *,
    remote_name: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(existing or {})
    credentials = dict(payload.get("credentials") or {})
    credentials.update(
        {
            "api_key": channel.api_key,
            "base_url": channel.base_url,
            "model_mapping": build_model_mapping(model_ids),
        }
    )
    payload.update(
        {
            "name": remote_name,
            "platform": provider_to_sub2api_platform(channel.provider_type),
            "type": "apikey",
            "credentials": credentials,
            "group_ids": parse_group_ids(link.sub2api_group_ids_json),
            "priority": link.sub2api_priority,
            "concurrency": link.sub2api_concurrency,
            "status": "active" if channel.enabled else "disabled",
        }
    )
    return payload


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value
```

- [ ] **Step 4: Run payload tests and verify they pass**

Run:

```powershell
uv run pytest tests/test_sync_payloads.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit payload builders**

Run:

```powershell
git add app/services/sync_payloads.py tests/test_sync_payloads.py
git commit -m "feat: add sync payload builders"
```

---

### Task 3: Target HTTP Clients

**Files:**
- Create: `app/services/sync_clients.py`
- Test: `tests/test_sync_clients.py`

- [ ] **Step 1: Write failing client tests**

Create `tests/test_sync_clients.py`:

```python
import json

import httpx
import pytest

from app.models import SyncTarget
from app.services.sync_clients import NewAPIClient, Sub2APIClient, SyncClientError, client_for_target


@pytest.mark.asyncio
async def test_sub2api_client_sends_admin_key_and_finds_account():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["x-api-key"] == "admin-secret"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "items": [{"id": 12, "name": "union_main"}],
                    "total": 1,
                    "page": 1,
                    "page_size": 20,
                    "pages": 1,
                },
            },
        )

    target = SyncTarget(
        name="sub",
        target_type="sub2api",
        base_url="https://sub.test",
        auth_config_json=json.dumps({"admin_api_key": "admin-secret"}),
    )
    client = Sub2APIClient(target, transport=httpx.MockTransport(handler))

    found = await client.find_account_by_name("union_main")

    assert found == {"id": 12, "name": "union_main"}
    assert requests[0].url.path == "/api/v1/admin/accounts"


@pytest.mark.asyncio
async def test_newapi_client_sends_fixed_headers_and_finds_channel():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer token"
        assert request.headers["new-api-user"] == "1"
        return httpx.Response(
            200,
            json={
                "success": True,
                "message": "",
                "data": {
                    "items": [{"id": 9, "name": "union_main"}],
                    "total": 1,
                    "type_counts": {"1": 1},
                },
            },
        )

    target = SyncTarget(
        name="new",
        target_type="new_api",
        base_url="https://new.test",
        auth_config_json=json.dumps({"authorization": "Bearer token", "new_api_user": "1"}),
    )
    client = NewAPIClient(target, transport=httpx.MockTransport(handler))

    found = await client.find_channel_by_name("union_main")

    assert found == {"id": 9, "name": "union_main"}
    assert requests[0].url.path == "/api/channel/search"


@pytest.mark.asyncio
async def test_client_raises_for_error_envelope():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "message": "bad token"})

    target = SyncTarget(
        name="new",
        target_type="new_api",
        base_url="https://new.test",
        auth_config_json=json.dumps({"authorization": "Bearer token", "new_api_user": "1"}),
    )
    client = NewAPIClient(target, transport=httpx.MockTransport(handler))

    with pytest.raises(SyncClientError, match="bad token"):
        await client.test_connection()


def test_client_factory_selects_target_type():
    sub = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    new = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", auth_config_json="{}")

    assert isinstance(client_for_target(sub), Sub2APIClient)
    assert isinstance(client_for_target(new), NewAPIClient)
```

- [ ] **Step 2: Run client tests and verify they fail**

Run:

```powershell
uv run pytest tests/test_sync_clients.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.sync_clients'`.

- [ ] **Step 3: Implement target HTTP clients**

Create `app/services/sync_clients.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import SyncTarget


class SyncClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


@dataclass
class SyncClientResult:
    status_code: int
    data: Any
    raw: Any


def load_target_auth(target: SyncTarget) -> dict[str, Any]:
    try:
        config = json.loads(target.auth_config_json or "{}")
    except json.JSONDecodeError as exc:
        raise SyncClientError(f"目标站点鉴权配置 JSON 无效：{exc}") from exc
    if not isinstance(config, dict):
        raise SyncClientError("目标站点鉴权配置必须是 JSON 对象。")
    return config


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


class BaseTargetClient:
    def __init__(self, target: SyncTarget, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.target = target
        self.transport = transport

    def headers(self) -> dict[str, str]:
        return {}

    async def request(self, method: str, path: str, **kwargs: Any) -> SyncClientResult:
        timeout = kwargs.pop("timeout", 20)
        url = _join_url(self.target.base_url, path)
        headers = {**self.headers(), **kwargs.pop("headers", {})}
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise SyncClientError(f"请求目标站点失败：{exc}") from exc
        try:
            raw = response.json()
        except ValueError as exc:
            raise SyncClientError("目标站点返回非 JSON 响应。", status_code=response.status_code, response=response.text[:500]) from exc
        data = self.unwrap_response(raw, response.status_code)
        return SyncClientResult(status_code=response.status_code, data=data, raw=raw)

    def unwrap_response(self, raw: Any, status_code: int) -> Any:
        if status_code >= 400:
            raise SyncClientError(f"目标站点返回 HTTP {status_code}", status_code=status_code, response=raw)
        return raw


class Sub2APIClient(BaseTargetClient):
    def headers(self) -> dict[str, str]:
        auth = load_target_auth(self.target)
        api_key = str(auth.get("admin_api_key") or "").strip()
        if not api_key:
            raise SyncClientError("sub2api 目标缺少 Admin API Key。")
        return {"x-api-key": api_key}

    def unwrap_response(self, raw: Any, status_code: int) -> Any:
        if status_code >= 400:
            raise SyncClientError(f"sub2api 返回 HTTP {status_code}", status_code=status_code, response=raw)
        if isinstance(raw, dict) and raw.get("code", 0) != 0:
            raise SyncClientError(str(raw.get("message") or "sub2api 请求失败"), status_code=status_code, response=raw)
        return raw.get("data") if isinstance(raw, dict) and "data" in raw else raw

    async def test_connection(self) -> SyncClientResult:
        return await self.request("GET", "/api/v1/admin/accounts", params={"page": 1, "page_size": 1})

    async def find_account_by_name(self, name: str) -> dict[str, Any] | None:
        result = await self.request("GET", "/api/v1/admin/accounts", params={"page": 1, "page_size": 20, "search": name})
        return _find_named_item(_items_from_paginated(result.data), name)

    async def get_account(self, remote_id: str) -> dict[str, Any]:
        result = await self.request("GET", f"/api/v1/admin/accounts/{remote_id}")
        if not isinstance(result.data, dict):
            raise SyncClientError("sub2api 账号响应格式无效。", status_code=result.status_code, response=result.raw)
        return result.data

    async def create_account(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("POST", "/api/v1/admin/accounts", json=payload)

    async def update_account(self, remote_id: str, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("PUT", f"/api/v1/admin/accounts/{remote_id}", json=payload)


class NewAPIClient(BaseTargetClient):
    def headers(self) -> dict[str, str]:
        auth = load_target_auth(self.target)
        authorization = str(auth.get("authorization") or "").strip()
        user_id = str(auth.get("new_api_user") or "").strip()
        if not authorization or not user_id:
            raise SyncClientError("new-api 目标缺少 Authorization Token 或 New-Api-User。")
        return {"Authorization": authorization, "New-Api-User": user_id}

    def unwrap_response(self, raw: Any, status_code: int) -> Any:
        if status_code >= 400:
            raise SyncClientError(f"new-api 返回 HTTP {status_code}", status_code=status_code, response=raw)
        if isinstance(raw, dict) and raw.get("success") is False:
            raise SyncClientError(str(raw.get("message") or "new-api 请求失败"), status_code=status_code, response=raw)
        return raw.get("data") if isinstance(raw, dict) and "data" in raw else raw

    async def test_connection(self) -> SyncClientResult:
        return await self.request("GET", "/api/channel/", params={"p": 1, "page_size": 1})

    async def find_channel_by_name(self, name: str) -> dict[str, Any] | None:
        result = await self.request("GET", "/api/channel/search", params={"keyword": name, "p": 1, "page_size": 20})
        return _find_named_item(_items_from_paginated(result.data), name)

    async def get_channel(self, remote_id: str) -> dict[str, Any]:
        result = await self.request("GET", f"/api/channel/{remote_id}")
        if not isinstance(result.data, dict):
            raise SyncClientError("new-api 渠道响应格式无效。", status_code=result.status_code, response=result.raw)
        return result.data

    async def create_channel(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("POST", "/api/channel/", json={"mode": "single", "channel": payload})

    async def update_channel(self, payload: dict[str, Any]) -> SyncClientResult:
        return await self.request("PUT", "/api/channel/", json=payload)


def _items_from_paginated(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _find_named_item(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in items:
        if str(item.get("name") or "") == name:
            return item
    return None


def client_for_target(target: SyncTarget, *, transport: httpx.AsyncBaseTransport | None = None) -> BaseTargetClient:
    if target.target_type == "sub2api":
        return Sub2APIClient(target, transport=transport)
    if target.target_type == "new_api":
        return NewAPIClient(target, transport=transport)
    raise SyncClientError(f"未知目标站点类型：{target.target_type}")
```

- [ ] **Step 4: Run client tests and verify they pass**

Run:

```powershell
uv run pytest tests/test_sync_clients.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit target clients**

Run:

```powershell
git add app/services/sync_clients.py tests/test_sync_clients.py
git commit -m "feat: add sync target clients"
```

---

### Task 4: Sync Orchestration Service

**Files:**
- Create: `app/services/channel_sync.py`
- Test: `tests/test_channel_sync.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/test_channel_sync.py`:

```python
import json

import pytest

from app.models import Channel, ChannelModel, ChannelSyncLink, SyncEvent, SyncTarget
from app.services.channel_sync import create_channel_sync_link, sync_existing_link
from app.services.sync_clients import SyncClientError, SyncClientResult


class FakeSub2APIClient:
    def __init__(self, *, existing=None, fail_update=False):
        self.existing = existing
        self.fail_update = fail_update
        self.created_payload = None
        self.updated_payload = None

    async def find_account_by_name(self, name):
        return self.existing

    async def create_account(self, payload):
        self.created_payload = payload
        return SyncClientResult(200, {"id": 42, **payload}, {"code": 0, "data": {"id": 42, **payload}})

    async def get_account(self, remote_id):
        return {"id": int(remote_id), "credentials": {"keep": "value"}}

    async def update_account(self, remote_id, payload):
        if self.fail_update:
            raise SyncClientError("update failed", status_code=500, response={"message": "bad"})
        self.updated_payload = payload
        return SyncClientResult(200, {"id": int(remote_id), **payload}, {"code": 0, "data": {"id": int(remote_id)}})


class FakeNewAPIClient:
    def __init__(self, *, existing=None):
        self.existing = existing
        self.created_payload = None

    async def find_channel_by_name(self, name):
        if self.created_payload and self.created_payload["name"] == name:
            return {"id": 9, "name": name}
        return self.existing

    async def create_channel(self, payload):
        self.created_payload = payload
        return SyncClientResult(200, {"success": True}, {"success": True})

    async def get_channel(self, remote_id):
        return {"id": int(remote_id), "name": "union_main", "group": "default"}

    async def update_channel(self, payload):
        return SyncClientResult(200, {"success": True}, {"success": True})


def _add_channel_with_models(db_session):
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="sk-live")
    db_session.add(channel)
    db_session.commit()
    db_session.add_all(
        [
            ChannelModel(channel_id=channel.id, model_id="gpt-4"),
            ChannelModel(channel_id=channel.id, model_id="gpt-4o"),
        ]
    )
    db_session.commit()
    return channel


@pytest.mark.asyncio
async def test_create_link_blocks_same_remote_name(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()

    client = FakeSub2APIClient(existing={"id": 1, "name": "union_main"})

    with pytest.raises(ValueError, match="已存在同名对象"):
        await create_channel_sync_link(db_session, channel, target, "1,2", 50, 3, client=client)

    assert db_session.query(ChannelSyncLink).count() == 0
    assert db_session.query(SyncEvent).count() == 1


@pytest.mark.asyncio
async def test_create_sub2api_link_persists_remote_id(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()

    client = FakeSub2APIClient()
    link = await create_channel_sync_link(db_session, channel, target, "1,2", 60, 5, client=client)

    assert link.remote_type == "account"
    assert link.remote_id == "42"
    assert link.remote_name == "union_main"
    assert link.last_sync_status == "success"
    assert json.loads(link.sub2api_group_ids_json) == [1, 2]
    assert client.created_payload["credentials"]["model_mapping"] == {"gpt-4": "gpt-4", "gpt-4o": "gpt-4o"}


@pytest.mark.asyncio
async def test_create_newapi_link_searches_created_channel_id(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()

    client = FakeNewAPIClient()
    link = await create_channel_sync_link(db_session, channel, target, "", 50, 3, client=client)

    assert link.remote_type == "channel"
    assert link.remote_id == "9"
    assert client.created_payload["models"] == "gpt-4,gpt-4o"


@pytest.mark.asyncio
async def test_sync_failure_updates_link_error_without_raising(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42", remote_name="union_main")
    db_session.add(link)
    db_session.commit()

    result = await sync_existing_link(db_session, link, client=FakeSub2APIClient(fail_update=True), action="manual_update")

    assert not result
    assert link.last_sync_status == "failed"
    assert "update failed" in link.last_sync_error
    assert db_session.query(SyncEvent).filter(SyncEvent.success.is_(False)).count() == 1


@pytest.mark.asyncio
async def test_manual_retry_success_clears_error(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()
    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=target.id,
        remote_type="account",
        remote_id="42",
        remote_name="union_main",
        last_sync_status="failed",
        last_sync_error="old",
    )
    db_session.add(link)
    db_session.commit()

    result = await sync_existing_link(db_session, link, client=FakeSub2APIClient(), action="manual_update")

    assert result
    assert link.last_sync_status == "success"
    assert link.last_sync_error is None
    assert link.last_synced_at is not None
```

- [ ] **Step 2: Run service tests and verify they fail**

Run:

```powershell
uv run pytest tests/test_channel_sync.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.channel_sync'`.

- [ ] **Step 3: Implement sync orchestration**

Create `app/services/channel_sync.py`:

```python
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import Channel, ChannelModel, ChannelSyncLink, SyncEvent, SyncTarget, now_utc
from app.services.sync_clients import BaseTargetClient, SyncClientError, client_for_target
from app.services.sync_payloads import (
    build_newapi_channel_payload,
    build_remote_name,
    build_sub2api_account_payload,
    dumps_group_ids,
    parse_group_ids,
    redact_sensitive,
)


def channel_model_ids(db: Session, channel_id: int) -> list[str]:
    rows = db.query(ChannelModel).filter(ChannelModel.channel_id == channel_id).order_by(ChannelModel.model_id).all()
    return [row.model_id for row in rows]


def record_sync_event(
    db: Session,
    *,
    channel_id: int | None,
    target_id: int | None,
    link_id: int | None,
    action: str,
    success: bool,
    status_code: int | None = None,
    message: str | None = None,
    request_payload: Any = None,
    response_payload: Any = None,
) -> SyncEvent:
    event = SyncEvent(
        channel_id=channel_id,
        target_id=target_id,
        link_id=link_id,
        action=action,
        success=success,
        status_code=status_code,
        message=message,
        request_json=json.dumps(redact_sensitive(request_payload), ensure_ascii=False) if request_payload is not None else None,
        response_json=json.dumps(redact_sensitive(response_payload), ensure_ascii=False) if response_payload is not None else None,
    )
    db.add(event)
    return event


async def create_channel_sync_link(
    db: Session,
    channel: Channel,
    target: SyncTarget,
    group_ids: str | None,
    priority: int,
    concurrency: int,
    *,
    client: BaseTargetClient | None = None,
) -> ChannelSyncLink:
    if not target.enabled:
        raise ValueError("目标站点未启用。")
    existing = db.query(ChannelSyncLink).filter(ChannelSyncLink.channel_id == channel.id, ChannelSyncLink.target_id == target.id).first()
    if existing:
        raise ValueError("该渠道已关联此目标站点。")
    remote_name = build_remote_name(target, channel)
    client = client or client_for_target(target)
    model_ids = channel_model_ids(db, channel.id)
    group_id_list = parse_group_ids(group_ids)
    remote_type = "account" if target.target_type == "sub2api" else "channel"
    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=target.id,
        remote_type=remote_type,
        remote_name=remote_name,
        sub2api_group_ids_json=dumps_group_ids(group_id_list),
        sub2api_priority=priority,
        sub2api_concurrency=concurrency,
    )

    try:
        if target.target_type == "sub2api":
            found = await client.find_account_by_name(remote_name)  # type: ignore[attr-defined]
            if found:
                raise ValueError("目标站点已存在同名对象，请先手动改名或删除后再导入。")
            payload = build_sub2api_account_payload(channel, model_ids, link, remote_name=remote_name)
            result = await client.create_account(payload)  # type: ignore[attr-defined]
            remote_id = _extract_remote_id(result.data)
        else:
            found = await client.find_channel_by_name(remote_name)  # type: ignore[attr-defined]
            if found:
                raise ValueError("目标站点已存在同名对象，请先手动改名或删除后再导入。")
            payload = build_newapi_channel_payload(channel, model_ids, remote_name=remote_name)
            result = await client.create_channel(payload)  # type: ignore[attr-defined]
            created = await client.find_channel_by_name(remote_name)  # type: ignore[attr-defined]
            if not created:
                raise SyncClientError("new-api 创建成功但无法查询到新渠道 ID。", status_code=result.status_code, response=result.raw)
            remote_id = _extract_remote_id(created)
        link.remote_id = str(remote_id)
        link.last_sync_status = "success"
        link.last_sync_error = None
        link.last_synced_at = now_utc()
        db.add(link)
        db.flush()
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            link_id=link.id,
            action="import_create",
            success=True,
            status_code=result.status_code,
            message="导入成功",
            request_payload=payload,
            response_payload=result.raw,
        )
        db.commit()
        db.refresh(link)
        return link
    except ValueError as exc:
        record_sync_event(db, channel_id=channel.id, target_id=target.id, link_id=None, action="import_create", success=False, message=str(exc))
        db.commit()
        raise
    except SyncClientError as exc:
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            link_id=None,
            action="import_create",
            success=False,
            status_code=exc.status_code,
            message=str(exc),
            response_payload=exc.response,
        )
        db.commit()
        raise ValueError(str(exc)) from exc


async def sync_existing_link(
    db: Session,
    link: ChannelSyncLink,
    *,
    client: BaseTargetClient | None = None,
    action: str = "auto_update",
) -> bool:
    channel = link.channel
    target = link.target
    if not link.sync_enabled or not target.enabled or not link.remote_id:
        return True
    client = client or client_for_target(target)
    model_ids = channel_model_ids(db, channel.id)
    try:
        if link.remote_type == "account":
            existing = await client.get_account(link.remote_id)  # type: ignore[attr-defined]
            payload = build_sub2api_account_payload(channel, model_ids, link, remote_name=link.remote_name or build_remote_name(target, channel), existing=existing)
            result = await client.update_account(link.remote_id, payload)  # type: ignore[attr-defined]
        else:
            existing = await client.get_channel(link.remote_id)  # type: ignore[attr-defined]
            payload = build_newapi_channel_payload(channel, model_ids, remote_name=link.remote_name or build_remote_name(target, channel), remote_id=link.remote_id, existing=existing)
            result = await client.update_channel(payload)  # type: ignore[attr-defined]
        link.last_sync_status = "success"
        link.last_sync_error = None
        link.last_synced_at = now_utc()
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            link_id=link.id,
            action=action,
            success=True,
            status_code=result.status_code,
            message="同步成功",
            request_payload=payload,
            response_payload=result.raw,
        )
        db.commit()
        return True
    except SyncClientError as exc:
        link.last_sync_status = "failed"
        link.last_sync_error = str(exc)
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            link_id=link.id,
            action=action,
            success=False,
            status_code=exc.status_code,
            message=str(exc),
            response_payload=exc.response,
        )
        db.commit()
        return False


async def sync_channel_links(db: Session, channel: Channel, *, action: str = "auto_update") -> tuple[int, int]:
    links = (
        db.query(ChannelSyncLink)
        .filter(ChannelSyncLink.channel_id == channel.id, ChannelSyncLink.sync_enabled.is_(True), ChannelSyncLink.remote_id.isnot(None))
        .all()
    )
    success = 0
    failed = 0
    for link in links:
        if await sync_existing_link(db, link, action=action):
            success += 1
        else:
            failed += 1
    return success, failed


def _extract_remote_id(data: Any) -> Any:
    if isinstance(data, dict):
        if data.get("id") not in (None, ""):
            return data["id"]
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get("id") not in (None, ""):
            return inner["id"]
    raise SyncClientError("目标站点响应中缺少远端 ID。", response=data)
```

- [ ] **Step 4: Run service tests and verify they pass**

Run:

```powershell
uv run pytest tests/test_channel_sync.py -q
```

Expected: PASS.

- [ ] **Step 5: Run related unit tests**

Run:

```powershell
uv run pytest tests/test_sync_models.py tests/test_sync_payloads.py tests/test_sync_clients.py tests/test_channel_sync.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit sync service**

Run:

```powershell
git add app/services/channel_sync.py tests/test_channel_sync.py
git commit -m "feat: add channel sync service"
```

---

### Task 5: Sync Target Forms and Routes

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/base.html`
- Create: `app/templates/sync_targets.html`
- Test: `tests/test_sync_forms.py`

- [ ] **Step 1: Write failing form helper tests**

Create `tests/test_sync_forms.py`:

```python
import json

import pytest

from app.main import build_sync_target_config, sync_target_form_from_item
from app.models import SyncTarget


def test_build_sub2api_target_config():
    config = build_sync_target_config(
        target_type="sub2api",
        sub2api_admin_api_key=" admin-secret ",
        newapi_authorization="",
        newapi_user="",
    )

    assert config == {"admin_api_key": "admin-secret"}


def test_build_newapi_target_config():
    config = build_sync_target_config(
        target_type="new_api",
        sub2api_admin_api_key="",
        newapi_authorization=" Bearer token ",
        newapi_user=" 1 ",
    )

    assert config == {"authorization": "Bearer token", "new_api_user": "1"}


def test_sync_target_config_rejects_missing_secret():
    with pytest.raises(ValueError, match="Admin API Key"):
        build_sync_target_config(target_type="sub2api", sub2api_admin_api_key="", newapi_authorization="", newapi_user="")


def test_sync_target_form_masks_secrets():
    target = SyncTarget(
        name="new",
        target_type="new_api",
        base_url="https://new.test",
        name_prefix="union_",
        auth_config_json=json.dumps({"authorization": "Bearer token", "new_api_user": "1"}),
    )

    form = sync_target_form_from_item(target)

    assert form["newapi_authorization"] == ""
    assert form["newapi_user"] == "1"
    assert form["secret_configured"]
```

- [ ] **Step 2: Run form tests and verify they fail**

Run:

```powershell
uv run pytest tests/test_sync_forms.py -q
```

Expected: FAIL because `build_sync_target_config` and `sync_target_form_from_item` do not exist.

- [ ] **Step 3: Add imports and form helper functions**

Modify `app/main.py` imports:

```python
from app.models import AlertEvent, AlertRule, BalanceSnapshot, Channel, ChannelModel, ChannelSyncLink, ExtractorTemplate, HealthCheck, NotificationChannel, SyncTarget, User, now_utc
from app.services.channel_sync import create_channel_sync_link, sync_channel_links, sync_existing_link
from app.services.sync_clients import SyncClientError, client_for_target
```

Add helper functions near notification form helpers:

```python
def build_sync_target_config(
    *,
    target_type: str,
    sub2api_admin_api_key: str = "",
    newapi_authorization: str = "",
    newapi_user: str = "",
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = dict(existing or {})
    if target_type == "sub2api":
        api_key = sub2api_admin_api_key.strip() or str(existing.get("admin_api_key") or "")
        if not api_key:
            raise ValueError("sub2api Admin API Key 必填。")
        return {"admin_api_key": api_key}
    if target_type == "new_api":
        authorization = newapi_authorization.strip() or str(existing.get("authorization") or "")
        user_id = newapi_user.strip() or str(existing.get("new_api_user") or "")
        if not authorization:
            raise ValueError("new-api Authorization Token 必填。")
        if not user_id:
            raise ValueError("new-api New-Api-User 必填。")
        return {"authorization": authorization, "new_api_user": user_id}
    raise ValueError("目标站点类型无效。")


def sync_target_auth_config(item: SyncTarget) -> dict[str, Any]:
    try:
        parsed = json.loads(item.auth_config_json or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def sync_target_form_from_item(item: SyncTarget | None = None) -> dict[str, Any]:
    if item is None:
        return {
            "name": "",
            "target_type": "sub2api",
            "base_url": "",
            "name_prefix": "union_",
            "enabled": True,
            "sub2api_admin_api_key": "",
            "newapi_authorization": "",
            "newapi_user": "",
            "secret_configured": False,
        }
    config = sync_target_auth_config(item)
    return {
        "name": item.name,
        "target_type": item.target_type,
        "base_url": item.base_url,
        "name_prefix": item.name_prefix,
        "enabled": item.enabled,
        "sub2api_admin_api_key": "",
        "newapi_authorization": "",
        "newapi_user": str(config.get("new_api_user") or ""),
        "secret_configured": bool(config.get("admin_api_key") or config.get("authorization")),
    }
```

- [ ] **Step 4: Add sync target routes**

Add these routes before channel detail dynamic route so `/sync-targets` cannot be captured by `/channels/{channel_id}`:

```python
@app.get("/sync-targets", response_class=HTMLResponse)
def sync_targets_page(request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    targets = db.query(SyncTarget).order_by(SyncTarget.created_at.desc()).all()
    return render(request, "sync_targets.html", {"targets": targets, "form": sync_target_form_from_item(), "mode": "create", "user": user})


@app.post("/sync-targets")
def create_sync_target(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    target_type: Annotated[str, Form()],
    base_url: Annotated[str, Form()],
    name_prefix: Annotated[str, Form()] = "union_",
    enabled: Annotated[bool | None, Form()] = None,
    sub2api_admin_api_key: Annotated[str, Form()] = "",
    newapi_authorization: Annotated[str, Form()] = "",
    newapi_user: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        auth_config = build_sync_target_config(
            target_type=target_type,
            sub2api_admin_api_key=sub2api_admin_api_key,
            newapi_authorization=newapi_authorization,
            newapi_user=newapi_user,
        )
    except ValueError as exc:
        return flash_redirect("/sync-targets", str(exc), "error")
    target = SyncTarget(
        name=name.strip(),
        target_type=target_type,
        base_url=base_url.strip().rstrip("/"),
        enabled=bool(enabled),
        name_prefix=name_prefix.strip() or "union_",
        auth_config_json=json.dumps(auth_config, ensure_ascii=False),
    )
    db.add(target)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return flash_redirect("/sync-targets", f"目标站点保存失败：{exc}", "error")
    return flash_redirect("/sync-targets", "目标站点已创建。")


@app.get("/sync-targets/{target_id}/edit", response_class=HTMLResponse)
def edit_sync_target_page(target_id: int, request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    target = _get_sync_target(db, target_id)
    targets = db.query(SyncTarget).order_by(SyncTarget.created_at.desc()).all()
    return render(request, "sync_targets.html", {"targets": targets, "form": sync_target_form_from_item(target), "mode": "edit", "editing": target, "user": user})


@app.post("/sync-targets/{target_id}")
def update_sync_target(
    target_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    target_type: Annotated[str, Form()],
    base_url: Annotated[str, Form()],
    name_prefix: Annotated[str, Form()] = "union_",
    enabled: Annotated[bool | None, Form()] = None,
    sub2api_admin_api_key: Annotated[str, Form()] = "",
    newapi_authorization: Annotated[str, Form()] = "",
    newapi_user: Annotated[str, Form()] = "",
) -> RedirectResponse:
    target = _get_sync_target(db, target_id)
    try:
        auth_config = build_sync_target_config(
            target_type=target_type,
            sub2api_admin_api_key=sub2api_admin_api_key,
            newapi_authorization=newapi_authorization,
            newapi_user=newapi_user,
            existing=sync_target_auth_config(target),
        )
    except ValueError as exc:
        return flash_redirect(f"/sync-targets/{target.id}/edit", str(exc), "error")
    target.name = name.strip()
    target.target_type = target_type
    target.base_url = base_url.strip().rstrip("/")
    target.enabled = bool(enabled)
    target.name_prefix = name_prefix.strip() or "union_"
    target.auth_config_json = json.dumps(auth_config, ensure_ascii=False)
    db.commit()
    return flash_redirect("/sync-targets", "目标站点已保存。")


@app.post("/sync-targets/{target_id}/delete")
def delete_sync_target(target_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    target = _get_sync_target(db, target_id)
    if db.query(ChannelSyncLink).filter(ChannelSyncLink.target_id == target.id).first():
        return flash_redirect("/sync-targets", "目标站点已有渠道关联，请先删除关联。", "error")
    db.delete(target)
    db.commit()
    return flash_redirect("/sync-targets", "目标站点已删除。")


@app.post("/sync-targets/{target_id}/test")
async def test_sync_target(target_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    target = _get_sync_target(db, target_id)
    try:
        await client_for_target(target).test_connection()
    except SyncClientError as exc:
        return flash_redirect("/sync-targets", f"连接测试失败：{exc}", "error")
    return flash_redirect("/sync-targets", "连接测试成功。")
```

Add helper:

```python
def _get_sync_target(db: Session, target_id: int) -> SyncTarget:
    target = db.get(SyncTarget, target_id)
    if not target:
        raise HTTPException(status_code=404)
    return target
```

- [ ] **Step 5: Add sync target page template**

Create `app/templates/sync_targets.html`:

```html
{% extends "base.html" %}
{% block content %}
  <div class="topbar">
    <div class="title">
      <h1>同步目标</h1>
      <p>管理 sub2api 和 new-api 目标站点。</p>
    </div>
  </div>

  <section class="panel">
    <h2>{{ "编辑目标站点" if mode == "edit" else "新增目标站点" }}</h2>
    <form method="post" action="{{ '/sync-targets/' ~ editing.id if mode == 'edit' else '/sync-targets' }}" class="grid">
      <div class="form-grid">
        <div class="field">
          <label for="name">名称</label>
          <input id="name" name="name" value="{{ form.name }}" required>
        </div>
        <div class="field">
          <label for="target_type">类型</label>
          <select id="target_type" name="target_type">
            <option value="sub2api" {% if form.target_type == "sub2api" %}selected{% endif %}>sub2api</option>
            <option value="new_api" {% if form.target_type == "new_api" %}selected{% endif %}>new-api</option>
          </select>
        </div>
        <div class="field full">
          <label for="base_url">Base URL</label>
          <input id="base_url" name="base_url" value="{{ form.base_url }}" placeholder="https://example.com" required>
        </div>
        <div class="field">
          <label for="name_prefix">名称前缀</label>
          <input id="name_prefix" name="name_prefix" value="{{ form.name_prefix or 'union_' }}" required>
        </div>
        <div class="field">
          <label class="check-row">
            <input type="checkbox" name="enabled" value="true" {% if form.enabled %}checked{% endif %}>
            启用目标站点
          </label>
        </div>
        <div class="field full target-sub2api-only">
          <label for="sub2api_admin_api_key">Admin API Key</label>
          <input id="sub2api_admin_api_key" name="sub2api_admin_api_key" value="" autocomplete="off" {% if mode != "edit" %}required{% endif %}>
          {% if form.secret_configured and form.target_type == "sub2api" %}<div class="muted small">已保存密钥；留空表示不修改。</div>{% endif %}
        </div>
        <div class="field full target-newapi-only">
          <label for="newapi_authorization">Authorization Token</label>
          <input id="newapi_authorization" name="newapi_authorization" value="" autocomplete="off" {% if mode != "edit" %}required{% endif %}>
          {% if form.secret_configured and form.target_type == "new_api" %}<div class="muted small">已保存 Token；留空表示不修改。</div>{% endif %}
        </div>
        <div class="field target-newapi-only">
          <label for="newapi_user">New-Api-User</label>
          <input id="newapi_user" name="newapi_user" value="{{ form.newapi_user }}">
        </div>
      </div>
      <div class="actions">
        <button type="submit">{{ "保存目标站点" if mode == "edit" else "创建目标站点" }}</button>
        {% if mode == "edit" %}<a class="button secondary" href="/sync-targets">取消</a>{% endif %}
      </div>
    </form>
  </section>

  <section class="panel">
    <h2>目标站点</h2>
    <table>
      <thead>
        <tr>
          <th>名称</th>
          <th>类型</th>
          <th>Base URL</th>
          <th>前缀</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        {% for target in targets %}
          <tr>
            <td>{{ target.name }}</td>
            <td>{{ "sub2api" if target.target_type == "sub2api" else "new-api" }}</td>
            <td class="small">{{ target.base_url }}</td>
            <td><code>{{ target.name_prefix }}</code></td>
            <td>
              {% if target.enabled %}
                <span class="badge success">启用</span>
              {% else %}
                <span class="badge">停用</span>
              {% endif %}
            </td>
            <td>
              <div class="actions">
                <a class="button secondary" href="/sync-targets/{{ target.id }}/edit">编辑</a>
                <form class="inline-form" method="post" action="/sync-targets/{{ target.id }}/test">
                  <button class="secondary" type="submit">测试</button>
                </form>
                <form class="inline-form" method="post" action="/sync-targets/{{ target.id }}/delete" onsubmit="return confirm('确定删除这个目标站点吗？');">
                  <button class="danger" type="submit">删除</button>
                </form>
              </div>
            </td>
          </tr>
        {% else %}
          <tr><td colspan="6" class="muted">还没有目标站点。</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </section>

  <script>
    (() => {
      const typeField = document.querySelector("#target_type");
      const sub2apiFields = document.querySelectorAll(".target-sub2api-only");
      const newapiFields = document.querySelectorAll(".target-newapi-only");
      const updateFields = () => {
        const isSub2API = typeField.value === "sub2api";
        sub2apiFields.forEach((node) => node.style.display = isSub2API ? "" : "none");
        newapiFields.forEach((node) => node.style.display = isSub2API ? "none" : "");
      };
      typeField.addEventListener("change", updateFields);
      updateFields();
    })();
  </script>
{% endblock %}
```

- [ ] **Step 6: Add sidebar link**

Modify `app/templates/base.html` nav:

```html
          <a href="/sync-targets">同步目标</a>
```

Place it after “渠道”.

- [ ] **Step 7: Run form and existing route helper tests**

Run:

```powershell
uv run pytest tests/test_sync_forms.py tests/test_auth.py tests/test_notifications.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit sync target UI and routes**

Run:

```powershell
git add app/main.py app/templates/base.html app/templates/sync_targets.html tests/test_sync_forms.py
git commit -m "feat: add sync target management"
```

---

### Task 6: Channel Sync Panel and Sync Routes

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/channel_detail.html`
- Create: `app/templates/partials/channel_sync_panel.html`
- Test: `tests/test_sync_forms.py`

- [ ] **Step 1: Add failing channel sync context test**

Append to `tests/test_sync_forms.py`:

```python
from app.main import channel_sync_context
from app.models import Channel, ChannelSyncLink, SyncTarget


def test_channel_sync_context_lists_enabled_targets_and_existing_links(db_session):
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="sk-live")
    enabled_target = SyncTarget(name="enabled", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    disabled_target = SyncTarget(name="disabled", target_type="new_api", base_url="https://new.test", enabled=False, auth_config_json="{}")
    db_session.add_all([channel, enabled_target, disabled_target])
    db_session.commit()
    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=enabled_target.id,
        remote_type="account",
        remote_id="42",
        remote_name="union_main",
    )
    db_session.add(link)
    db_session.commit()

    context = channel_sync_context(db_session, channel)

    assert [target.name for target in context["sync_targets"]] == ["enabled"]
    assert context["sync_links"] == [link]
    assert context["linked_target_ids"] == {enabled_target.id}
```

- [ ] **Step 2: Run form tests and verify the new test fails**

Run:

```powershell
uv run pytest tests/test_sync_forms.py -q
```

Expected: FAIL with `ImportError` for `channel_sync_context`.

- [ ] **Step 3: Add channel sync context helper**

Add to `app/main.py` near other route helper functions:

```python
def channel_sync_context(db: Session, channel: Channel) -> dict[str, Any]:
    sync_targets = db.query(SyncTarget).filter(SyncTarget.enabled.is_(True)).order_by(SyncTarget.name).all()
    sync_links = db.query(ChannelSyncLink).filter(ChannelSyncLink.channel_id == channel.id).order_by(ChannelSyncLink.created_at.desc()).all()
    linked_target_ids = {item.target_id for item in sync_links}
    return {
        "sync_targets": sync_targets,
        "sync_links": sync_links,
        "linked_target_ids": linked_target_ids,
    }
```

- [ ] **Step 4: Add channel detail context and sync link routes**

Modify `channel_detail` in `app/main.py` to include sync context:

```python
    context = {"channel": channel, "extractors": extractors, "rule": rule, "models": models, "checks": check_rows, "balances": balances, "user": user}
    context.update(channel_sync_context(db, channel))
    return render(request, "channel_detail.html", context)
```

Add routes after channel actions:

```python
@app.post("/channels/{channel_id}/sync-links")
async def create_channel_sync(
    channel_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    target_id: Annotated[int, Form()],
    sub2api_group_ids: Annotated[str, Form()] = "",
    sub2api_priority: Annotated[int, Form()] = 50,
    sub2api_concurrency: Annotated[int, Form()] = 3,
) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    target = _get_sync_target(db, target_id)
    try:
        await create_channel_sync_link(db, channel, target, sub2api_group_ids, sub2api_priority, sub2api_concurrency)
    except ValueError as exc:
        return flash_redirect(f"/channels/{channel.id}", str(exc), "error")
    return flash_redirect(f"/channels/{channel.id}", "目标站点导入成功。")


@app.post("/channels/{channel_id}/sync-links/{link_id}/sync")
async def manual_sync_channel_link(channel_id: int, link_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    link = _get_channel_sync_link(db, channel.id, link_id)
    success = await sync_existing_link(db, link, action="manual_update")
    if success:
        return flash_redirect(f"/channels/{channel.id}", "同步成功。")
    return flash_redirect(f"/channels/{channel.id}", link.last_sync_error or "同步失败。", "error")


@app.post("/channels/{channel_id}/sync-links/{link_id}/toggle")
def toggle_channel_sync_link(channel_id: int, link_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    link = _get_channel_sync_link(db, channel.id, link_id)
    link.sync_enabled = not link.sync_enabled
    db.commit()
    return flash_redirect(f"/channels/{channel.id}", "同步关联已更新。")


@app.post("/channels/{channel_id}/sync-links/{link_id}/delete")
def delete_channel_sync_link(channel_id: int, link_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    link = _get_channel_sync_link(db, channel.id, link_id)
    db.delete(link)
    db.commit()
    return flash_redirect(f"/channels/{channel.id}", "本地同步关联已删除，远端对象未删除。")
```

Add helper:

```python
def _get_channel_sync_link(db: Session, channel_id: int, link_id: int) -> ChannelSyncLink:
    link = db.get(ChannelSyncLink, link_id)
    if not link or link.channel_id != channel_id:
        raise HTTPException(status_code=404)
    return link
```

- [ ] **Step 5: Wire automatic sync after channel save and model refresh**

Convert `update_channel` to async:

```python
@app.post("/channels/{channel_id}")
async def update_channel(
```

After `db.commit()` in `update_channel`, add:

```python
    success, failed = await sync_channel_links(db, channel, action="auto_update")
    if failed:
        return flash_redirect(f"/channels/{channel.id}", f"渠道已保存；同步成功 {success} 个，失败 {failed} 个。", "error")
    if success:
        return flash_redirect(f"/channels/{channel.id}", f"渠道已保存；已同步 {success} 个目标。")
```

Keep the existing final redirect for no sync links:

```python
    return flash_redirect(f"/channels/{channel.id}", "渠道已保存。")
```

In `refresh_models`, after `result = await refresh_channel_models(db, channel)`, add:

```python
    if result.success:
        await sync_channel_links(db, channel, action="auto_update")
```

Do not change behavior for failed model refresh.

- [ ] **Step 6: Add channel sync panel partial**

Create `app/templates/partials/channel_sync_panel.html`:

```html
<section class="panel">
  <div class="panel-heading">
    <div>
      <h2>目标同步</h2>
      <p class="muted small">首次导入会在目标站点创建新对象；删除本地关联不会删除远端对象。</p>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>目标</th>
        <th>远端</th>
        <th>状态</th>
        <th>最近同步</th>
        <th>操作</th>
      </tr>
    </thead>
    <tbody>
      {% for link in sync_links %}
        <tr>
          <td>
            {{ link.target.name }}
            <div class="muted small">{{ "sub2api" if link.target.target_type == "sub2api" else "new-api" }}</div>
          </td>
          <td>
            {{ link.remote_name or "-" }}
            <div class="muted small">{{ link.remote_type }} #{{ link.remote_id or "-" }}</div>
            {% if link.remote_type == "account" %}
              <div class="muted small">分组 {{ link.sub2api_group_ids_json }} · 优先级 {{ link.sub2api_priority }} · 并发 {{ link.sub2api_concurrency }}</div>
            {% endif %}
          </td>
          <td>
            {% if not link.sync_enabled %}
              <span class="badge">暂停</span>
            {% elif link.last_sync_status == "success" %}
              <span class="badge success">成功</span>
            {% elif link.last_sync_status == "failed" %}
              <span class="badge error">失败</span>
            {% else %}
              <span class="badge">未同步</span>
            {% endif %}
            {% if link.last_sync_error %}
              <div class="muted small">{{ link.last_sync_error }}</div>
            {% endif %}
          </td>
          <td>{{ link.last_synced_at|format_dt if link.last_synced_at else "-" }}</td>
          <td>
            <div class="actions">
              <form class="inline-form" method="post" action="/channels/{{ channel.id }}/sync-links/{{ link.id }}/sync">
                <button class="secondary" type="submit">同步</button>
              </form>
              <form class="inline-form" method="post" action="/channels/{{ channel.id }}/sync-links/{{ link.id }}/toggle">
                <button class="secondary" type="submit">{{ "恢复" if not link.sync_enabled else "暂停" }}</button>
              </form>
              <form class="inline-form" method="post" action="/channels/{{ channel.id }}/sync-links/{{ link.id }}/delete" onsubmit="return confirm('只删除本地关联，不会删除远端对象。确定继续吗？');">
                <button class="danger" type="submit">删除关联</button>
              </form>
            </div>
          </td>
        </tr>
      {% else %}
        <tr><td colspan="5" class="muted">还没有同步关联。</td></tr>
      {% endfor %}
    </tbody>
  </table>

  <h3>导入到目标站点</h3>
  <form method="post" action="/channels/{{ channel.id }}/sync-links" class="grid">
    <div class="form-grid">
      <div class="field">
        <label for="sync_target_id">目标站点</label>
        <select id="sync_target_id" name="target_id" required>
          {% for target in sync_targets %}
            {% if target.id not in linked_target_ids %}
              <option value="{{ target.id }}" data-target-type="{{ target.target_type }}">{{ target.name }}（{{ "sub2api" if target.target_type == "sub2api" else "new-api" }}）</option>
            {% endif %}
          {% endfor %}
        </select>
      </div>
      <div class="field sync-sub2api-only">
        <label for="sub2api_group_ids">sub2api 分组 ID</label>
        <input id="sub2api_group_ids" name="sub2api_group_ids" placeholder="1,2,3">
      </div>
      <div class="field sync-sub2api-only">
        <label for="sub2api_priority">sub2api 优先级</label>
        <input id="sub2api_priority" name="sub2api_priority" type="number" value="50">
      </div>
      <div class="field sync-sub2api-only">
        <label for="sub2api_concurrency">sub2api 并发</label>
        <input id="sub2api_concurrency" name="sub2api_concurrency" type="number" min="1" value="3">
      </div>
    </div>
    <div>
      <button type="submit" {% if sync_targets|length == linked_target_ids|length %}disabled{% endif %}>导入并关联</button>
    </div>
  </form>
</section>

<script>
  (() => {
    const target = document.querySelector("#sync_target_id");
    const sub2apiFields = document.querySelectorAll(".sync-sub2api-only");
    if (!target) return;
    const updateFields = () => {
      const selected = target.selectedOptions[0];
      const isSub2API = selected && selected.dataset.targetType === "sub2api";
      sub2apiFields.forEach((node) => node.style.display = isSub2API ? "" : "none");
    };
    target.addEventListener("change", updateFields);
    updateFields();
  })();
</script>
```

- [ ] **Step 7: Include sync panel in channel detail**

Modify `app/templates/channel_detail.html`, after the “渠道配置” section:

```html
  {% include "partials/channel_sync_panel.html" %}
```

- [ ] **Step 8: Run sync and monitoring tests**

Run:

```powershell
uv run pytest tests/test_channel_sync.py tests/test_sync_forms.py tests/test_monitoring.py tests/test_status_lanes.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit channel sync panel**

Run:

```powershell
git add app/main.py app/templates/channel_detail.html app/templates/partials/channel_sync_panel.html tests/test_sync_forms.py
git commit -m "feat: add channel sync controls"
```

---

### Task 7: Documentation and Full Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README feature list**

Modify `README.md` feature list to include:

```markdown
- 支持配置多个 sub2api/new-api 目标站点，将本站渠道导入远端并在后续保存时同步。
```

Add a short configuration section after “配置”:

```markdown
## 多目标同步

在“同步目标”页面配置目标站点：

- `sub2api`：填写站点 Base URL 和 Admin API Key。
- `new-api`：填写站点 Base URL、Authorization Token 和 New-Api-User。

在渠道详情页可以将当前渠道导入目标站点。首次导入会创建远端对象，名称为目标站点名称前缀加本站渠道名，默认前缀为 `union_`。如果远端已存在同名对象，导入会被拦截，需要先在目标站点人工处理。后续保存本站渠道或刷新模型成功后，会自动同步已启用的关联；同步失败不会回滚本站保存，但会记录错误。
```

- [ ] **Step 2: Run targeted tests**

Run:

```powershell
uv run pytest tests/test_sync_models.py tests/test_sync_payloads.py tests/test_sync_clients.py tests/test_channel_sync.py tests/test_sync_forms.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```powershell
uv run pytest
```

Expected: PASS.

- [ ] **Step 4: Start local dev server for smoke testing**

Run:

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 3670
```

Expected:

```text
Uvicorn running on http://127.0.0.1:3670
```

Open:

```text
http://127.0.0.1:3670
```

Smoke checks:

- Sidebar shows “同步目标”.
- `/sync-targets` renders.
- Creating a target with missing secret shows a flash error.
- Existing channel detail page renders with “目标同步” panel.

- [ ] **Step 5: Commit documentation**

Run:

```powershell
git add README.md
git commit -m "docs: describe sync targets"
```

---

## Final Verification

Run:

```powershell
uv run pytest
git status --short
```

Expected:

- All tests pass.
- `git status --short` shows no unintended uncommitted files.

If the dev server is still running after smoke testing, stop it with `Ctrl+C` before final handoff.
