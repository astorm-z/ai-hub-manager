import json

import httpx
import pytest

from app.models import SyncTarget
from app.services.sync_clients import NewAPIClient, Sub2APIClient, SyncClientError, client_for_target


def sub2api_target(auth_config_json: str | None = None) -> SyncTarget:
    return SyncTarget(
        name="sub",
        target_type="sub2api",
        base_url="https://sub.test",
        auth_config_json=auth_config_json or json.dumps({"admin_api_key": "admin-secret"}),
    )


def newapi_target(auth_config_json: str | None = None) -> SyncTarget:
    return SyncTarget(
        name="new",
        target_type="new_api",
        base_url="https://new.test",
        auth_config_json=auth_config_json
        or json.dumps({"authorization": "Bearer token", "new_api_user": "1"}),
    )


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

    target = sub2api_target()
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

    target = newapi_target()
    client = NewAPIClient(target, transport=httpx.MockTransport(handler))

    found = await client.find_channel_by_name("union_main")

    assert found == {"id": 9, "name": "union_main"}
    assert requests[0].url.path == "/api/channel/search"


@pytest.mark.asyncio
async def test_client_raises_for_error_envelope():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "message": "bad token"})

    target = newapi_target()
    client = NewAPIClient(target, transport=httpx.MockTransport(handler))

    with pytest.raises(SyncClientError, match="bad token"):
        await client.test_connection()


@pytest.mark.asyncio
async def test_sub2api_find_account_checks_later_pages_for_exact_name():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        items = [{"id": 1, "name": "union_main_old"}] if page == 1 else [{"id": 2, "name": "union_main"}]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": items,
                    "total": 2,
                    "page": page,
                    "page_size": 20,
                    "pages": 2,
                },
            },
        )

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    found = await client.find_account_by_name("union_main")

    assert found == {"id": 2, "name": "union_main"}
    assert [request.url.params["page"] for request in requests] == ["1", "2"]


@pytest.mark.asyncio
async def test_newapi_find_channel_checks_later_pages_for_exact_name():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["p"])
        items = [{"id": 1, "name": "union_main_old"}] if page == 1 else [{"id": 2, "name": "union_main"}]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": items,
                    "total": 2,
                    "page": page,
                    "page_size": 20,
                    "pages": 2,
                },
            },
        )

    client = NewAPIClient(newapi_target(), transport=httpx.MockTransport(handler))

    found = await client.find_channel_by_name("union_main")

    assert found == {"id": 2, "name": "union_main"}
    assert [request.url.params["p"] for request in requests] == ["1", "2"]


@pytest.mark.asyncio
async def test_sub2api_find_account_requires_exact_name():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [{"id": 1, "name": "union_main_old"}],
                    "page": 1,
                    "page_size": 20,
                    "pages": 1,
                },
            },
        )

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    assert await client.find_account_by_name("union_main") is None


@pytest.mark.asyncio
async def test_sub2api_get_account_accepts_direct_entity_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/admin/accounts/42"
        return httpx.Response(200, json={"id": 42, "name": "union_main"})

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    found = await client.get_account(42)

    assert found == {"id": 42, "name": "union_main"}


@pytest.mark.asyncio
async def test_http_error_status_raises_sync_client_error_with_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "server exploded"})

    client = NewAPIClient(newapi_target(), transport=httpx.MockTransport(handler))

    with pytest.raises(SyncClientError) as exc_info:
        await client.test_connection()

    assert exc_info.value.status_code == 500
    assert "server exploded" in exc_info.value.response


@pytest.mark.asyncio
async def test_non_json_response_raises_sync_client_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = NewAPIClient(newapi_target(), transport=httpx.MockTransport(handler))

    with pytest.raises(SyncClientError, match="非 JSON"):
        await client.test_connection()


@pytest.mark.asyncio
async def test_sub2api_error_envelope_raises_sync_client_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 401, "message": "bad admin key"})

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    with pytest.raises(SyncClientError, match="bad admin key"):
        await client.test_connection()


@pytest.mark.parametrize("raw_auth", ["{", "[]"])
def test_invalid_auth_json_raises_sync_client_error(raw_auth: str):
    target = SyncTarget(name="bad", target_type="new_api", base_url="https://new.test", auth_config_json=raw_auth)

    with pytest.raises(SyncClientError):
        NewAPIClient(target)


def test_missing_admin_api_key_raises_sync_client_error():
    client = Sub2APIClient(sub2api_target("{}"))

    with pytest.raises(SyncClientError, match="admin_api_key"):
        client.headers()


def test_blank_admin_api_key_raises_sync_client_error():
    client = Sub2APIClient(sub2api_target(json.dumps({"admin_api_key": "   "})))

    with pytest.raises(SyncClientError, match="admin_api_key"):
        client.headers()


@pytest.mark.parametrize(
    "raw_auth, expected",
    [
        (json.dumps({"new_api_user": "1"}), "authorization"),
        (json.dumps({"authorization": "Bearer token"}), "new_api_user"),
    ],
)
def test_missing_newapi_auth_raises_sync_client_error(raw_auth: str, expected: str):
    client = NewAPIClient(newapi_target(raw_auth))

    with pytest.raises(SyncClientError, match=expected):
        client.headers()


@pytest.mark.parametrize(
    "raw_auth, expected",
    [
        (json.dumps({"authorization": "   ", "new_api_user": "1"}), "authorization"),
        (json.dumps({"authorization": "Bearer token", "new_api_user": "   "}), "new_api_user"),
    ],
)
def test_blank_newapi_auth_raises_sync_client_error(raw_auth: str, expected: str):
    client = NewAPIClient(newapi_target(raw_auth))

    with pytest.raises(SyncClientError, match=expected):
        client.headers()


@pytest.mark.asyncio
async def test_sub2api_test_connection_uses_page_size_one():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["page"] == "1"
        assert request.url.params["page_size"] == "1"
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    await client.test_connection()


@pytest.mark.asyncio
async def test_sub2api_list_groups_normalizes_group_records():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/admin/groups/all"
        assert request.url.params["platform"] == "openai"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": [
                    {"id": 1, "name": "default", "platform": "openai", "status": "active"},
                    {"id": 2, "name": "claude", "platform": "anthropic", "status": "disabled"},
                    {"id": None, "name": "broken"},
                ],
            },
        )

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    groups = await client.list_groups(platform="openai")

    assert groups == [
        {"id": 1, "value": "1", "name": "default", "label": "default（openai）", "platform": "openai", "status": "active"},
        {"id": 2, "value": "2", "name": "claude", "label": "claude（anthropic，disabled）", "platform": "anthropic", "status": "disabled"},
    ]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_sub2api_list_groups_accepts_paginated_shape():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/admin/groups/all"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "items": [
                        {"id": 3, "name": "openai-default", "platform": "openai", "status": "active"},
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 20,
                    "pages": 1,
                },
            },
        )

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    groups = await client.list_groups()

    assert groups == [
        {"id": 3, "value": "3", "name": "openai-default", "label": "openai-default（openai）", "platform": "openai", "status": "active"},
    ]


@pytest.mark.asyncio
async def test_sub2api_test_connection_rejects_base_url_without_host():
    target = sub2api_target()
    target.base_url = "https:///s2a.astorm.cn"
    client = Sub2APIClient(target)

    with pytest.raises(SyncClientError, match="Base URL 无效"):
        await client.test_connection()


@pytest.mark.asyncio
async def test_newapi_test_connection_uses_page_size_one():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["p"] == "1"
        assert request.url.params["page_size"] == "1"
        return httpx.Response(200, json={"success": True, "data": {"items": []}})

    client = NewAPIClient(newapi_target(), transport=httpx.MockTransport(handler))

    await client.test_connection()


@pytest.mark.asyncio
async def test_newapi_list_groups_normalizes_group_names():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/group/"
        return httpx.Response(
            200,
            json={
                "success": True,
                "message": "",
                "data": ["default", " claude-code ", "", "claude-code-ot", "claude-code"],
            },
        )

    client = NewAPIClient(newapi_target(), transport=httpx.MockTransport(handler))

    groups = await client.list_groups()

    assert groups == [
        {"name": "default", "value": "default", "label": "default"},
        {"name": "claude-code", "value": "claude-code", "label": "claude-code"},
        {"name": "claude-code-ot", "value": "claude-code-ot", "label": "claude-code-ot"},
    ]


@pytest.mark.asyncio
async def test_sub2api_create_account_sends_post_body_and_path():
    payload = {"name": "union_main", "status": "active"}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/admin/accounts"
        assert json.loads(request.content) == payload
        return httpx.Response(200, json={"code": 0, "data": {"id": 7}})

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    result = await client.create_account(payload)

    assert result.data == {"id": 7}


@pytest.mark.asyncio
async def test_sub2api_update_account_sends_put_body_and_path():
    payload = {"name": "union_main", "status": "disabled"}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/v1/admin/accounts/7"
        assert json.loads(request.content) == payload
        return httpx.Response(200, json={"code": 0, "data": {"id": 7}})

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    result = await client.update_account(7, payload)

    assert result.data == {"id": 7}


@pytest.mark.asyncio
async def test_sub2api_delete_account_sends_delete_and_path():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/admin/accounts/7"
        return httpx.Response(200, json={"code": 0, "data": {"id": 7}})

    client = Sub2APIClient(sub2api_target(), transport=httpx.MockTransport(handler))

    result = await client.delete_account(7)

    assert result.data == {"id": 7}


@pytest.mark.asyncio
async def test_newapi_create_channel_sends_wrapped_post_body_and_path():
    payload = {"name": "union_main", "status": 1}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/channel/"
        assert json.loads(request.content) == {"mode": "single", "channel": payload}
        return httpx.Response(200, json={"success": True, "data": {"id": 8}})

    client = NewAPIClient(newapi_target(), transport=httpx.MockTransport(handler))

    result = await client.create_channel(payload)

    assert result.data == {"id": 8}


@pytest.mark.asyncio
async def test_newapi_update_channel_sends_put_body_and_path():
    payload = {"id": 8, "name": "union_main", "status": 2}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/channel/"
        assert json.loads(request.content) == payload
        return httpx.Response(200, json={"success": True, "data": {"id": 8}})

    client = NewAPIClient(newapi_target(), transport=httpx.MockTransport(handler))

    result = await client.update_channel(payload)

    assert result.data == {"id": 8}


@pytest.mark.asyncio
async def test_newapi_delete_channel_sends_delete_and_path():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/channel/8"
        return httpx.Response(200, json={"success": True, "data": {"id": 8}})

    client = NewAPIClient(newapi_target(), transport=httpx.MockTransport(handler))

    result = await client.delete_channel(8)

    assert result.data == {"id": 8}


def test_client_factory_selects_target_type():
    sub = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    new = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", auth_config_json="{}")

    assert isinstance(client_for_target(sub), Sub2APIClient)
    assert isinstance(client_for_target(new), NewAPIClient)


def test_client_factory_rejects_unknown_target_type():
    target = SyncTarget(name="other", target_type="other", base_url="https://other.test", auth_config_json="{}")

    with pytest.raises(SyncClientError, match="未知同步目标类型"):
        client_for_target(target)
