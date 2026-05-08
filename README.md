# 人格升华（astrbot_plugin_persona_sublimation）

人格升华是一个 AstrBot 插件，用于捕获实际发往 LLM 的请求快照，并在插件内部提供人格观察、补丁审批、快照基线、模板模块与多人格 profile 管理能力。

它不是 AstrBot 原生 persona 系统的替代品，而是包在原生 persona 外面的一层「观察 / 归档 / 审批 / 应用」管理插件。真正修改人格时仍调用 AstrBot 的 `persona_manager.update_persona`。

## 功能特性

- 捕获 AstrBot 发往 LLM 的 `ProviderRequest` 快照
- 独立 Web 页面查看 prompts、system prompts、contexts、tools、媒体 URL 等信息
- 支持按 `session_id` 查看历史请求
- 支持多 persona 的观察记录、补丁、profile 计数与隔离
- 支持通用人格模板和模块资产
- 支持 persona prompt 快照 / 基线归档
- 支持补丁审批流：`pending -> approved -> applied`
- 应用补丁前检查 base prompt，避免覆盖已被其他地方修改过的人格
- 支持从旧 `persona-evolution` SKILL 幂等迁移数据
- 修复 SQLite 捕获库超过阈值时可能卡死主事件循环的自 DoS 问题

## 运行方式

插件启用后会启动一个独立 aiohttp 服务，默认地址：

```text
http://127.0.0.1:7833/
```

默认只监听本机。页面不依赖 AstrBot Dashboard，也不走 Dashboard 鉴权。

## 安全警告

本插件页面和 API **没有内置鉴权**。

页面/API 会展示完整 prompts、system prompts、contexts、媒体 URL 和工具信息，其中可能包含隐私、密钥片段、业务数据或工具参数。

默认配置为：

```text
bind_host = 127.0.0.1
```

如果改成 `0.0.0.0` 或 `::`，就等于把完整 LLM 请求快照暴露给能访问该端口的网络。需要外网访问时，请自行加反向代理、访问控制和鉴权。

## HTTP API

### 捕获历史

- `GET /`：前端页面
- `GET /api/captures?limit=&offset=&session_id=`：请求快照列表
- `GET /api/captures/<id>`：请求快照详情
- `GET /api/sessions`：有捕获记录的 session 列表

### Persona 管理视图

- `GET /api/personas`：AstrBot 当前 persona 列表，并展示 observation/patch 计数
- `GET /api/personas/<persona_id>`：指定 persona 详情

### Observations

- `GET /api/observations?persona_id=&limit=`：观察记录列表
- `POST /api/observations`：创建观察记录

示例：

```json
{
  "persona_id": "lingjiu-2",
  "source": "owner-feedback",
  "content": "说话再短一点",
  "interpretation": "普通聊天应减少流程化说明",
  "emotion": "认真记录"
}
```

### Patches

- `GET /api/patches?persona_id=&limit=`：补丁列表
- `POST /api/patches`：创建补丁，默认 `pending`
- `POST /api/patches/<patch_id>/approve`：审批补丁
- `POST /api/patches/<patch_id>/apply`：应用补丁

应用补丁要求：

1. 补丁状态必须是 `approved`
2. 只更新补丁所属的 `persona_id`
3. 当前 persona 的 prompt 必须与补丁的 `base_prompt` 匹配，避免误覆盖

### Templates / Modules

- `GET /api/templates`：模板/模块资产列表
- `POST /api/templates`：创建或更新模板/模块

可用于保存通用人格模板、模块片段、NSFW 模块、RolePlay 模块、运维备注模块等。

### Snapshots / Baselines

- `GET /api/snapshots?persona_id=&limit=`：快照列表
- `GET /api/snapshots/<snapshot_id>`：查看单个快照正文
- `POST /api/snapshots`：创建快照

`POST /api/snapshots` 不传 `content` 时，会从当前 AstrBot persona 读取 `system_prompt` 创建快照，不会修改 persona。

### Profiles

- `GET /api/profiles`：列出 persona profile，并附 observation/patch 计数
- `POST /api/profiles/<persona_id>`：更新某个人格的 display_name、archetype、notes、template 关联

### 旧 SKILL 迁移

- `POST /api/migrate-skill`

用于从旧的 `/root/AstrBot/data/skills/persona-evolution` 迁移：

- `observation_notes_<pid>.md`
- `persona_patches_<pid>.json`
- `persona_current_<pid>.md`
- 人格模板与模块资产

迁移是幂等的，只做归档/可视化，不会自动修改任何 AstrBot persona。

## 存储说明

插件数据存放在 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_persona_sublimation/captures.sqlite3
```

这个 SQLite 是插件自己的捕获与归档库，不是 AstrBot 主数据库，也不是长期记忆库。

里面保存：

- LLM 请求快照
- observation 观察记录
- patch 补丁记录
- templates/modules 模板与模块资产
- snapshots/baselines prompt 快照
- profiles 人格补充资料

如果库异常或过大，可以在停用/停止插件后删除 `captures.sqlite3`。删除后只会丢失本插件保存的历史快照和归档数据，不会删除 AstrBot 原版 persona、长期记忆或核心配置。

## 容量控制与自 DoS 修复

插件会按 `max_records` 保留捕获记录。数据库超过 50 MiB 时，会按有限批次删除最早记录，并在循环外受限执行 `VACUUM`。

清理逻辑不会再依赖「SQLite DELETE 后主库文件大小立刻下降」作为退出条件，避免捕获库过大时陷入无限循环导致 AstrBot 主事件循环卡死。

捕获写库已放入线程执行，避免同步 SQLite 操作阻塞主事件循环。

## 配置项

- `enabled`：是否启用捕获，默认 `true`
- `max_records`：最大记录条数，默认 `500`
- `capture_full_contexts`：是否完整保存 contexts，默认 `true`
- `bind_host`：独立页面监听地址，默认 `127.0.0.1`
- `port`：独立页面端口，默认 `7833`

## 与原版 persona 的兼容性

兼容。

插件不会替换或改造 AstrBot 原生 persona 数据结构。原版 persona 仍由 AstrBot 主库管理，Dashboard 人格列表、会话绑定 persona、conversation 中的 `persona_id` 都照常工作。

插件只额外保存围绕 persona 的管理数据。真正应用补丁时调用：

```python
self.context.persona_manager.update_persona(
    persona_id=..., 
    system_prompt=...
)
```

## 聊天指令与 LLM 工具

当前版本不暴露聊天指令，也不暴露 LLM Tool。

代码中只注册了：

```python
@filter.on_llm_request(priority=-100)
```

用于只读捕获 LLM 请求快照。普通聊天不会新增 slash/text 指令，也不会给模型额外工具调用入口。

## 从旧 persona-evolution SKILL 迁移

旧 `persona-evolution` SKILL 已退场。后续人格迭代应使用本插件 API。

旧目录可以保留一个 deprecated 指针，避免运行时技能清单异常，但不要继续使用旧 `apply_persona.py` 或旧 SKILL 流程写入人格。

## 开发验证

```bash
PYTHONPATH=/root/AstrBot python -m py_compile main.py
ruff format .
ruff check .
```

## License

MIT
