# AI 中转渠道管理

一个本地部署的 AI 中转渠道管理网站，使用 FastAPI、SQLite 和服务端渲染页面实现。

## 功能

- 管理 OpenAI 兼容和 Claude/Anthropic 兼容渠道。
- 保存渠道 Base URL、API Key、启用状态、探测模型和检查间隔。
- 获取模型列表，手动测试模型请求，记录可用性检测历史。
- 使用安全 JSON 余额提取器查询余额，内置 `sub2api` 和 `newapi` 模板。
- 内置定时监控，检测渠道可用性、余额变化和模型列表变化。
- 支持错误次数阈值、模型变动、余额阈值告警。
- 支持 SMTP 邮件通知和自定义 HTTP GET/POST 通知模板。
- 支持配置多个 sub2api/new-api 目标站点，将本站渠道导入远端并在后续保存时同步。
- 单管理员登录；管理员密码哈希保存，渠道密钥按需求明文保存到本地 SQLite。

## 启动

本机没有 Python 时，先使用 `uv` 创建虚拟环境并安装依赖：

```powershell
uv venv
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 3670
```

打开：

```text
http://127.0.0.1:3670
```

首次访问会进入管理员初始化页面。

## 测试

```powershell
uv run pytest
```

## 配置

- `HOST`：默认 `127.0.0.1`
- `PORT`：默认 `3670`
- `DATABASE_URL`：默认 `sqlite:///data/ai_hub_manager.db`
- `SESSION_SECRET`：生产环境建议设置为随机长字符串
- `SCHEDULER_ENABLED=0`：禁用内置定时监控

## 多目标同步

在“同步目标”页面配置目标站点：

- `sub2api`：填写站点 Base URL 和 Admin API Key。
- `new-api`：填写站点 Base URL、Authorization Token 和 New-Api-User。

在渠道详情页可以将当前渠道导入目标站点。首次导入会创建远端对象，名称为目标站点名称前缀加本站渠道名，默认前缀为 `union_`。如果远端已存在同名对象，导入会被拦截，需要先在目标站点人工处理。后续保存本站渠道或刷新模型成功后，会自动同步已启用的关联；同步失败不会回滚本站保存，但会记录错误。删除同步关联时默认只删除本地关联，也可以勾选同时从目标站点删除对应远端对象。

## 余额提取器格式

提取器使用 JSON，不执行 JavaScript。模板变量支持 `{{baseUrl}}`、`{{apiKey}}` 和渠道配置里的自定义变量。

表达式支持：

- `{"path": "data.quota"}`
- `{"const": "USD"}`
- `{"first": [...]}`
- `{"add": [...]}`
- `{"divide": [left, right]}`
- `{"default": [value, fallback]}`

## 通知模板变量

HTTP 和邮件通知支持 `$message`、`$channel_name`、`$alert_type`、`$severity`、`$created_at`，以及告警 payload 中的字段，例如 `$remaining`、`$unit`、`$error_count`、`$window_minutes`、`$added_models`、`$removed_models`、`$old_models`、`$new_models`。
