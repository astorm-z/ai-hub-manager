# 同步目标分组多选 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在渠道同步关联表单中为 `sub2api` 和 `new-api` 目标站点支持远端分组列表获取、多选保存和后续自动同步。

**Architecture:** 在现有同步目标客户端中新增分组列表方法，在同步 payload 层新增 `new-api` 分组字符串规范化，在 `ChannelSyncLink` 保存 `newapi_groups`，并由渠道详情上下文为模板准备每个可关联目标的分组选项和失败兜底状态。数据库兼容通过启动时幂等补列实现。

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, Jinja2, httpx, pytest, uv。

---

## File Structure

- Modify `app/models.py`: add `ChannelSyncLink.newapi_groups`.
- Modify `app/database.py`: add idempotent SQLite column migration for existing `channel_sync_links`.
- Modify `app/services/sync_payloads.py`: add `normalize_newapi_groups` and make `build_newapi_channel_payload` accept group configuration.
- Modify `app/services/sync_clients.py`: add `list_groups` to `Sub2APIClient` and `NewAPIClient`.
- Modify `app/services/channel_sync.py`: accept and persist `newapi_groups`, only validate sub2api priority/concurrency for sub2api targets, and pass saved groups into new-api payloads.
- Modify `app/main.py`: accept `newapi_groups` from the form and build channel sync context with remote group choices.
- Modify `app/templates/partials/channel_sync_panel.html`: render per-target sub2api/new-api group multi-selects with manual fallback inputs.
- Modify tests in `tests/test_sync_models.py`, `tests/test_sync_payloads.py`, `tests/test_sync_clients.py`, `tests/test_channel_sync.py`, and `tests/test_sync_forms.py`.

---

### Task 1: Persist new-api group selections

- [x] Write failing tests asserting `ChannelSyncLink.newapi_groups` persists and old SQLite tables get the column with default `default`.
- [x] Implement model field and startup migration.
- [x] Run `uv run pytest tests/test_sync_models.py -q`.

### Task 2: Payload parsing and new-api sync value

- [x] Write failing tests for `normalize_newapi_groups`, explicit new-api group payloads, and preserved existing payload overwrite.
- [x] Implement group normalization and pass `newapi_groups` into `build_newapi_channel_payload`.
- [x] Run `uv run pytest tests/test_sync_payloads.py -q`.

### Task 3: Remote group list clients

- [x] Write failing tests for `Sub2APIClient.list_groups()` and `NewAPIClient.list_groups()`.
- [x] Implement both methods using `/api/v1/admin/groups/all` and `/api/group/`.
- [x] Run `uv run pytest tests/test_sync_clients.py -q`.

### Task 4: Link creation and automatic sync

- [x] Write failing service tests proving new-api groups are saved during import and reused during updates.
- [x] Update `create_channel_sync_link` and `sync_existing_link` to carry `newapi_groups`.
- [x] Run `uv run pytest tests/test_channel_sync.py -q`.

### Task 5: Form rendering and submission

- [x] Write failing route/template tests proving sub2api/new-api group selects render and fallback inputs render on list failures.
- [x] Update route form parameters, context builder, and template JavaScript.
- [x] Run `uv run pytest tests/test_sync_forms.py -q`.

### Task 6: Verification

- [x] Run focused tests: `uv run pytest tests/test_sync_models.py tests/test_sync_payloads.py tests/test_sync_clients.py tests/test_channel_sync.py tests/test_sync_forms.py -q`.
- [x] Run full suite: `uv run pytest -q`.
