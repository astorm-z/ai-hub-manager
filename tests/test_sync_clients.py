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
