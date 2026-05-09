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
    for raw in ("1,a", "0", "-1", "[0]", "[-1]", "[1.2]"):
        with pytest.raises(ValueError, match="分组 ID"):
            parse_group_ids(raw)


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
