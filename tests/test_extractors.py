import pytest

from app.services.extractors import NEWAPI_TEMPLATE, SUB2API_TEMPLATE, eval_expr, run_extractor


def test_eval_expr_supports_paths_and_math():
    data = {"data": {"quota": 1000, "used_quota": 500}}

    assert eval_expr({"path": "data.quota"}, data) == 1000
    assert eval_expr({"divide": [{"add": [{"path": "data.quota"}, {"path": "data.used_quota"}]}, {"const": 500}]}, data) == 3


@pytest.mark.asyncio
async def test_sub2api_template_extracts_with_mock_transport(monkeypatch):
    import httpx

    original_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer key"
        return httpx.Response(200, json={"quota": {"remaining": 12.5, "unit": "USD"}, "is_active": True})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=httpx.MockTransport(handler)))
    result = await run_extractor(SUB2API_TEMPLATE, {"baseUrl": "https://example.test", "apiKey": "key"})

    assert result.is_valid
    assert result.remaining == 12.5
    assert result.unit == "USD"


@pytest.mark.asyncio
async def test_newapi_template_handles_invalid_response(monkeypatch):
    import httpx

    original_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "message": "bad token"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=httpx.MockTransport(handler)))
    result = await run_extractor(NEWAPI_TEMPLATE, {"baseUrl": "https://example.test", "apiKey": "key", "accessToken": "token", "userId": "1"})

    assert not result.is_valid
    assert result.invalid_message == "bad token"
