# 多目标站点渠道同步设计

## 背景

本项目当前管理本地 AI 渠道，包含渠道名称、类型、Base URL、API Key、启用状态、探测模型、模型快照和余额/告警能力。用户希望在本站中关联多个 `sub2api` 或 `new-api` 站点，并能一键把一个或多个本站渠道导入到这些目标站点。导入后，本站渠道的 `apiurl`、`key`、模型列表等修改应自动同步到已关联的目标对象。

参考源码结论：

- `new-api` 的目标对象是 `model.Channel`，管理路由在 `/api/channel` 下。创建接口使用 `POST /api/channel/`，更新接口使用 `PUT /api/channel/`。管理鉴权需要 `Authorization` 与 `New-Api-User` 请求头。
- `sub2api` 中真正承载上游 API 凭据的是 `Account`，不是 `Channel`。管理路由在 `/api/v1/admin/accounts` 下。管理鉴权支持 `x-api-key: <admin-api-key>`。
- 本项目当前没有数据库迁移框架，启动时使用 `Base.metadata.create_all()`。新增表可以直接由 SQLAlchemy 创建；如后续新增已有表字段，需要补轻量兼容逻辑。

## 目标

- 支持在本站配置多个目标站点，每个目标站点可为 `sub2api` 或 `new-api`。
- 支持从渠道详情页将本站渠道首次导入目标站点并建立本地关联。
- 支持一个本站渠道关联多个目标站点。
- 支持后续保存本站渠道、刷新模型列表成功后，自动同步到所有已启用的关联目标。
- 同步失败不阻塞本站本地保存，但必须记录失败状态和可读错误。
- 删除本站渠道或本地关联时，不删除远端对象。

## 非目标

- 首版不实现后台任务队列、指数退避和长期重试调度。
- 首版不在本站删除远端 `sub2api Account` 或 `new-api Channel`。
- 首版不自动复用同名远端对象。
- 首版不支持远端对象人工选择绑定。远端对象 ID 由首次导入创建成功后写入。
- 首版不同步 `new-api` 的所有高级字段，例如渠道分组、权重、自动禁用策略、参数覆盖和 Header 覆盖。

## 需求结论

用户确认的关键规则：

- 采用“保存即同步 + 失败记录”：本站渠道保存成功后自动同步关联目标；失败只记录，不回滚本站保存。
- `sub2api` 侧导入目标为 `Account`。
- `sub2api` 关联需要额外维护并同步 `group_ids`、`priority`、`concurrency`。
- `sub2api` 专属参数放在“渠道与目标站点的关联配置”上，而不是放在本站渠道或目标站点默认配置上。
- 模型列表同步使用本站当前已发现模型快照，即 `ChannelModel` 表中的模型。
- 首次导入时，远端名称为 `目标站点名称前缀 + 本站渠道名`。
- 目标站点名称前缀默认 `union_`，可以在目标站点管理中按站点修改。
- 首次导入前必须检查远端是否已存在同名对象；存在则拦截并提示人工处理，不能自动覆盖、自动改名或自动复用。
- 删除本站渠道时只删除本地渠道和本地关联，不删除远端对象。
- `new-api` 目标站点使用固定表单字段保存 `Authorization Token` 和 `New-Api-User`。

## 推荐方案

采用“服务端同步器 + 目标站点/关联表”的方案。

这个方案新增目标站点管理、渠道同步关联、同步日志和同步服务。首次导入由本站直接请求目标站点创建对象；创建成功后保存远端 ID。后续同步按远端 ID 更新同一个远端对象。

不采用后台任务队列作为首版方案，因为当前项目没有任务队列语义。保存即同步足以覆盖需求，也让错误更直接可见。后续如果目标站点数量变多或网络不稳定，可以在同一数据模型上扩展异步队列。

## 数据模型

### `sync_targets`

保存目标站点配置。

字段：

- `id`: 主键。
- `name`: 站点名称。
- `target_type`: 目标类型，取值 `sub2api` 或 `new_api`。
- `base_url`: 目标站点 Base URL，保存时去掉尾部 `/`。
- `enabled`: 是否启用。
- `name_prefix`: 创建远端对象时使用的名称前缀，默认 `union_`。
- `auth_config_json`: 鉴权配置 JSON。
- `default_config_json`: 默认配置 JSON，首版保留扩展位。
- `created_at`: 创建时间。
- `updated_at`: 更新时间。

`auth_config_json` 结构：

```json
{
  "admin_api_key": "admin-xxx"
}
```

```json
{
  "authorization": "Bearer xxx",
  "new_api_user": "1"
}
```

约束：

- `name` 唯一，避免 UI、日志和关联选择中出现同名目标站点。
- `target_type` 只允许 `sub2api` 或 `new_api`。

密钥存储：

- 目标站点鉴权密钥首版按现有项目策略明文保存在本地 SQLite 中，与当前渠道 `api_key` 的存储方式一致。
- 页面展示时不做完整回显，编辑时使用空密码字段语义：未填写则保留旧值，填写则覆盖。

### `channel_sync_links`

保存本站渠道与目标站点远端对象的关联。

字段：

- `id`: 主键。
- `channel_id`: 本站渠道 ID。
- `target_id`: 目标站点 ID。
- `remote_type`: 远端对象类型，`sub2api` 使用 `account`，`new-api` 使用 `channel`。
- `remote_id`: 远端对象 ID。首次导入成功后写入。
- `remote_name`: 首次导入时生成并创建成功的远端名称。
- `sync_enabled`: 是否启用自动同步。
- `sub2api_group_ids_json`: `sub2api` 账号分组 ID JSON 数组，例如 `[1, 2]`。
- `sub2api_priority`: `sub2api` 账号优先级。
- `sub2api_concurrency`: `sub2api` 账号并发数。
- `last_sync_status`: `never`、`success` 或 `failed`。
- `last_sync_error`: 最近一次同步错误。
- `last_synced_at`: 最近一次成功同步时间。
- `created_at`: 创建时间。
- `updated_at`: 更新时间。

约束：

- `(channel_id, target_id)` 唯一，避免同一本站渠道重复关联同一目标站点。
- `channel_id` 和 `target_id` 级联删除本地关联。

### `sync_events`

保存同步事件日志，用于排障。

字段：

- `id`: 主键。
- `channel_id`: 本站渠道 ID，可为空以容忍目标被删除后的历史。
- `target_id`: 目标站点 ID，可为空以容忍目标被删除后的历史。
- `link_id`: 关联 ID，可为空。
- `action`: `import_create`、`auto_update`、`manual_update`、`test_connection` 等。
- `success`: 是否成功。
- `status_code`: 目标接口 HTTP 状态码。
- `message`: 可读消息。
- `request_json`: 脱敏后的请求摘要。
- `response_json`: 脱敏后的响应摘要。
- `created_at`: 创建时间。

敏感字段必须在写入日志前脱敏：

- `api_key`
- `key`
- `authorization`
- `admin_api_key`

## 远端字段映射

### 本站渠道到 `new-api Channel`

远端类型：`channel`。

创建名称：

- `remote_name = sync_target.name_prefix + channel.name`
- 创建成功后固定保存在 `channel_sync_links.remote_name`。

字段映射：

- `name`: `remote_name`
- `type`: 本站 `provider_type=openai` 映射为 `1`；`provider_type=claude` 映射为 `14`
- `key`: `channel.api_key`
- `base_url`: `channel.base_url`
- `models`: 本站当前模型快照按逗号拼接，例如 `gpt-4,gpt-4o`
- `test_model`: `channel.probe_model`
- `status`: `channel.enabled` 为真时 `1`，否则 `2`
- `group`: 首版固定 `default`

首次创建请求：

```json
{
  "mode": "single",
  "channel": {
    "name": "union_example",
    "type": 1,
    "key": "sk-xxx",
    "base_url": "https://example.com",
    "models": "gpt-4,gpt-4o",
    "test_model": "gpt-4o",
    "status": 1,
    "group": "default"
  }
}
```

后续更新请求使用 `PUT /api/channel/`，传包含 `id` 的完整 channel payload。为了降低覆盖远端高级字段的风险，实现时应先 `GET /api/channel/{id}` 读取现有对象，再只覆盖本站负责的字段后提交。

### 本站渠道到 `sub2api Account`

远端类型：`account`。

创建名称：

- `remote_name = sync_target.name_prefix + channel.name`
- 创建成功后固定保存在 `channel_sync_links.remote_name`。

字段映射：

- `name`: `remote_name`
- `platform`: 本站 `provider_type=openai` 映射为 `openai`；`provider_type=claude` 映射为 `anthropic`
- `type`: 固定 `apikey`
- `credentials.api_key`: `channel.api_key`
- `credentials.base_url`: `channel.base_url`
- `credentials.model_mapping`: 本站当前模型快照生成 `{ "model": "model" }`
- `group_ids`: 来自关联配置 `sub2api_group_ids_json`
- `priority`: 来自关联配置 `sub2api_priority`
- `concurrency`: 来自关联配置 `sub2api_concurrency`
- `status`: 创建时默认 `active`；后续 `channel.enabled` 为假时更新为 `disabled`，为真时更新为 `active`

请求示例：

```json
{
  "name": "union_example",
  "platform": "openai",
  "type": "apikey",
  "credentials": {
    "api_key": "sk-xxx",
    "base_url": "https://example.com",
    "model_mapping": {
      "gpt-4": "gpt-4",
      "gpt-4o": "gpt-4o"
    }
  },
  "group_ids": [1, 2],
  "priority": 50,
  "concurrency": 3
}
```

后续更新使用 `PUT /api/v1/admin/accounts/{remote_id}`。为避免清掉远端额外配置，实现时应先读取远端 account，再合并本站负责字段后提交。

## HTTP 客户端

新增服务模块建议：

- `app/services/sync_clients.py`: 负责目标站点 HTTP 请求、鉴权头、响应解析和错误归一化。
- `app/services/sync_payloads.py`: 负责把本站 `Channel`、`ChannelModel` 和 `ChannelSyncLink` 转换成远端 payload。
- `app/services/channel_sync.py`: 负责导入、同名检查、自动同步、手动重试、记录状态和事件。

`sub2api` 鉴权头：

```text
x-api-key: <admin_api_key>
```

`new-api` 鉴权头：

```text
Authorization: <authorization>
New-Api-User: <new_api_user>
```

如果 `authorization` 没有 `Bearer ` 前缀，服务层可以原样发送，不强行补前缀，避免兼容问题。UI 文案提示用户按目标站点要求填写完整值。

## 同步流程

### 目标站点管理

新增页面 `/sync-targets`。

功能：

- 列出目标站点。
- 新增目标站点。
- 编辑目标站点。
- 删除目标站点。
- 测试连接。

删除规则：

- 如果目标站点存在 `channel_sync_links` 关联，则拦截删除，提示先删除关联。

测试连接：

- `sub2api`: 请求 `GET /api/v1/admin/accounts?page=1&page_size=1`。
- `new-api`: 请求 `GET /api/channel/?p=1&page_size=1`。

### 渠道详情页同步面板

在渠道详情页新增“目标同步”面板。

展示：

- 目标站点名称和类型。
- 远端对象类型、ID 和名称。
- 是否启用自动同步。
- 最近同步状态。
- 最近同步时间。
- 最近错误摘要。

操作：

- 新增关联并导入。
- 手动同步。
- 暂停/恢复自动同步。
- 删除本地关联。

`sub2api` 关联表单额外字段：

- `group_ids`: 逗号分隔输入，例如 `1,2,3`。
- `priority`: 整数。
- `concurrency`: 整数。

### 首次导入

入口：

```text
POST /channels/{channel_id}/sync-links
```

流程：

1. 读取本站渠道、目标站点和当前模型快照。
2. 校验目标站点启用且鉴权配置完整。
3. 校验当前渠道尚未关联该目标站点。
4. 生成 `remote_name = target.name_prefix + channel.name`。
5. 调用目标站点查询接口检查同名对象。
6. 如果目标站点存在同名对象，拦截并提示人工处理。
7. 如果不存在，创建远端对象。
8. 创建成功后保存 `channel_sync_links`，写入 `remote_id`、`remote_name`、`last_sync_status=success` 和 `last_synced_at`。
9. 写入 `sync_events`。

同名检查：

- `new-api`: 优先使用 `/api/channel/search?keyword=<remote_name>`，按返回项 `name` 精确匹配；如接口响应结构变化，回退到分页列表。
- `sub2api`: 使用 `/api/v1/admin/accounts?search=<remote_name>`，按返回项 `name` 精确匹配。

首次导入失败：

- 不创建本地关联。
- 写入 `sync_events`。
- 用 flash 显示错误。

### 自动同步

触发点：

- 渠道保存成功后。
- 刷新模型成功后。
- 手动同步按钮。

自动同步范围：

- `sync_enabled=True`
- 目标站点 `enabled=True`
- `remote_id` 已存在

同步失败：

- 不回滚本站保存。
- 更新关联：`last_sync_status=failed`、`last_sync_error=<error>`。
- 写入 `sync_events`。
- 渠道详情页显示失败状态。

同步成功：

- 更新关联：`last_sync_status=success`、`last_sync_error=NULL`、`last_synced_at=now`。
- 写入 `sync_events`。

### 删除

本站渠道删除：

- 删除本地渠道。
- 级联删除本地同步关联。
- 不删除远端对象。

本地同步关联删除：

- 只删除本地 `channel_sync_links`。
- 不删除远端对象。

目标站点删除：

- 如果没有关联，允许删除。
- 如果存在关联，拦截并提示先删除关联。

## 页面和路由

新增路由：

```text
GET  /sync-targets
POST /sync-targets
GET  /sync-targets/{target_id}/edit
POST /sync-targets/{target_id}
POST /sync-targets/{target_id}/delete
POST /sync-targets/{target_id}/test

POST /channels/{channel_id}/sync-links
POST /channels/{channel_id}/sync-links/{link_id}/sync
POST /channels/{channel_id}/sync-links/{link_id}/toggle
POST /channels/{channel_id}/sync-links/{link_id}/delete
```

新增模板：

- `app/templates/sync_targets.html`
- `app/templates/sync_target_form.html`，也可首版合并到列表页。
- `app/templates/partials/channel_sync_panel.html`

导航：

- 在 `base.html` 增加“同步目标”入口。

## 错误处理

错误类型：

- 目标站点未启用。
- 鉴权配置缺失。
- 目标站点鉴权失败。
- 目标站点请求超时。
- 目标站点返回非 JSON。
- 目标站点返回 `success=false`。
- 首次导入同名冲突。
- 远端对象 ID 不存在。
- `sub2api group_ids` 输入不是逗号分隔整数。

错误展示：

- 表单错误使用 flash。
- 关联同步错误展示在渠道详情页同步面板。
- 同步日志页面首版不单独做；可先只在数据库记录，后续需要再加 UI。

## 测试计划

单元测试：

- `Channel -> new-api Channel payload`
- `Channel -> sub2api Account payload`
- 空模型列表时 payload 生成合理结果。
- `sub2api group_ids` 解析：
  - `1,2,3` -> `[1,2,3]`
  - 空字符串 -> `[]`
  - `1,a` -> 报错
- 敏感字段脱敏。
- 目标站点鉴权头：
  - `sub2api` 使用 `x-api-key`
  - `new-api` 使用 `Authorization` 和 `New-Api-User`

服务测试：

- 首次导入时远端同名对象存在，导入被拦截且不创建 link。
- 首次导入成功后保存 `remote_id` 和 `remote_name`。
- 自动同步失败不回滚本地渠道保存，并更新 link 错误状态。
- 手动重试成功后清空错误状态。

路由测试：

- 创建目标站点。
- 编辑目标站点名称前缀。
- 删除有关联的目标站点被拦截。
- 渠道详情页能渲染同步关联。

## 后续扩展

- 增加异步同步队列和自动重试。
- 支持选择已有远端对象进行绑定。
- 支持远端删除选项。
- 支持 `new-api` 的 `group`、`priority`、`weight` 等站点级或关联级配置。
- 增加同步日志 UI。
- 支持批量选择多个本站渠道一次导入多个目标站点。
