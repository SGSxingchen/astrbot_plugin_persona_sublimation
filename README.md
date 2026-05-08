# 人格升华（astrbot_plugin_persona_sublimation）

人格升华是一个 AstrBot 插件，用于捕获实际发往 LLM 的请求快照，并在插件内部提供人格观察、补丁审批、快照基线、模块与多人格 profile 管理能力。

它不是 AstrBot 原生 persona 系统的替代品，而是包在原生 persona 外面的一层「观察 / 归档 / 审批 / 应用」工作流插件。真正修改人格时仍调用 AstrBot 的 `persona_manager.update_persona`。

## 功能特性

- 捕获 AstrBot 发往 LLM 的 `ProviderRequest` 快照
- 独立 Web 页面查看 prompts、system prompts、contexts、tools、媒体 URL 等信息
- 中文 Web 工作台：按 persona 隔离维护 observation、patch、snapshot、module、profile，支持完整的人类审核工作流和资料维护
- 安全 LLM Tools：允许模型参与观察、查询、起草 pending 调整和留存快照，但不能直接应用人格调整
- 支持按 `session_id` 查看历史请求
- 支持多 persona 的观察记录、补丁、profile 计数与隔离
- 支持通用人格模块资产
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

## 前端人格工作台

`GET /` 提供纯静态 HTML/JS/CSS 的人类工作台，无构建链。页面包含：

- Persona 选择与状态卡片：按 `persona_id` 切换上下文，显示 observation/patch 计数和 prompt 长度
- Observations：记录一条观察、查看上下文、保存记录或移除无效记录
- Patches：起草人格调整、保存 pending 草案、查看 diff、放弃草案、审批并二次确认后应用；也支持人类显式点击“直接审批并应用”
- Snapshots：留存当前版本、查看版本内容、修改备注、移除快照，并可由此起草 pending 调整
- Modules：收纳模块、确认后查看敏感内容、更新内容、移除模块，并可由此起草 pending 调整
- Module Links：把模块关联到当前 persona，调整顺序、启用/停用、解除关联，并可由当前模块清单起草 pending 调整
- Profiles：维护当前 persona 的显示名、原型、备注和模块关联，支持保存资料与清空资料
- Captures：保留 LLM 请求捕获列表和详情查看

切换 persona 后，observations、patches、snapshots、profile 会按 `persona_id` 重新加载。页面不会自动应用任何补丁。快照和模块不会直接写入 AstrBot persona；它们只能先生成 pending patch，再由人类审批/应用。

“模块关联”是独立的装配清单：它只记录某个 persona 关联了哪些模块资产、顺序、角色、启用状态和备注，不会直接修改 AstrBot 原版 persona。要把模块清单真正写入 persona，需要点击“由模块清单起草调整”，生成 pending patch 后再审批/应用。


## 操作逻辑

详细 IA 与边界见 [`docs/operation_logic.md`](docs/operation_logic.md)。核心流程：

```text
观察记录 -> 起草调整 -> 人类审批 -> 应用到人格
模块库 -> 关联到当前人格 -> 当前人格模块清单 -> 由模块清单起草调整 -> 人类审批 -> 应用到人格
版本留存 -> 由版本起草调整 -> 人类审批 -> 应用到人格
```

只有前端人类确认的“应用到人格”和“直接审批并应用到人格”会写入 AstrBot 原版 persona。模块关联、模块查看、快照、档案、观察、请求记录和 LLM Tools 都只写插件数据或生成 pending 草案。

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

### 调试与数据整理

- `GET /api/debug/data-summary`：返回只含计数、ID、长度、sha256 前缀的安全数据摘要，不返回 prompt/content 正文
- `POST /api/debug/cleanup`：幂等整理数据模型；默认 dry-run，仅当 body 显式传 `{"apply": true}`、`{"commit": true}` 或 `{"write": true}` 才写库

cleanup 只做保守操作：规范 `persona_templates.metadata.kind/source/role/module_id/content_sha256`、补齐 `lingjiu-2` 的默认模块关联、移除完全相同 `(persona_id, source, content)` 的重复 observation、移除完全相同 `(persona_id, source, source_path, content_sha256)` 的重复 snapshot。不会移除 patch 历史，也不会修改 AstrBot 原版 persona。

### Persona 视图

- `GET /api/personas`：AstrBot 当前 persona 列表，并展示 observation/patch 计数
- `GET /api/personas/<persona_id>`：指定 persona 详情
- `GET /api/personas/<persona_id>/modules`：列出当前 persona 的模块关联清单，带模块摘要
- `POST /api/personas/<persona_id>/modules`：关联模块，body 包含 `template_id`、`role`、`enabled`、`order_index`、`notes`
- `PATCH /api/personas/<persona_id>/modules/<link_id>`：调整模块关联的角色、启用状态、顺序、备注
- `DELETE /api/personas/<persona_id>/modules/<link_id>`：解除关联，不删除模块本体
- `POST /api/personas/<persona_id>/modules/patch`：由当前启用模块清单组合生成 pending patch，不直接应用

### Observations

- `GET /api/observations?persona_id=&limit=`：观察记录列表
- `POST /api/observations`：创建观察记录
- `GET /api/observations/<id>`：查看单条观察记录
- `PATCH/POST /api/observations/<id>`：更新单条观察记录；可带 `persona_id` 做匹配校验
- `DELETE /api/observations/<id>?persona_id=`：移除单条观察记录；可带 `persona_id` 做匹配校验

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
- `GET /api/patches/<patch_id>`：查看单个补丁和 diff
- `PATCH /api/patches/<patch_id>` 或 `POST /api/patches/<patch_id>`：更新 pending 补丁草案，可改 trigger、changes、base_prompt、proposed_prompt
- `DELETE /api/patches/<patch_id>`：移除 pending 补丁。非 pending（已审批/已应用）不会被移除
- `POST /api/patches/<patch_id>/approve`：审批补丁
- `POST /api/patches/<patch_id>/apply`：应用补丁

`POST /api/patches` 支持两种模式：

1. 直接传 `proposed_prompt`：立即生成基于 `base_prompt` 的 diff
2. 只传 `changes` / `notes`：保存结构化 pending 草案，前端或人类后续再调整 `proposed_prompt`

应用补丁要求：

1. 补丁状态必须是 `approved`
2. 只更新补丁所属的 `persona_id`
3. 当前 persona 的 prompt 必须与补丁的 `base_prompt` 匹配，避免误覆盖

`POST /api/patches/<patch_id>/apply` 也支持显式直接审批 pending 补丁：

```json
{
  "auto_approve": true,
  "approved_by": "frontend-human"
}
```

也可以使用等价字段：

```json
{
  "approve": true,
  "approved_by": "frontend-human"
}
```

只有当请求体里显式传入 `auto_approve=true` 或 `approve=true` 时，pending 补丁才允许在 apply 内先审批再应用；普通 `POST /apply` 仍要求补丁已经是 `approved`。这只表示 HTTP API 的人类操作者在同一次点击中审批并应用；不是后台自动审批。前端按钮会二次确认，后端仍执行 base prompt 检查，只写补丁所属 `persona_id`，并记录 `approved_by` / `applied_at` / `status=applied`。

### Modules

- `GET /api/templates`：模块资产列表
- `GET /api/templates/<template_id>`：查看单个模块正文
- `POST /api/templates`：创建或更新模块
- `PATCH/POST /api/templates/<template_id>`：更新模块说明、正文、变量和 metadata；从 skill-migration 迁来的模块也允许更新
- `DELETE /api/templates/<template_id>`：移除模块
- `POST /api/templates/<template_id>/patch`：从模块正文生成 pending 补丁草案，不直接应用到 persona

可用于保存通用人格模块、模块片段、NSFW 模块、RolePlay 模块、运维备注模块等。

历史 HTTP 接口仍沿用 `/api/templates` 命名以保持兼容；前端统一显示为“模块”，准确含义是“模块资产”。

### Snapshots / Baselines

- `GET /api/snapshots?persona_id=&limit=`：快照列表
- `GET /api/snapshots/<snapshot_id>`：查看单个快照正文
- `POST /api/snapshots`：创建快照
- `PATCH/POST /api/snapshots/<snapshot_id>`：更新快照标签和 metadata，不改原版 persona
- `DELETE /api/snapshots/<snapshot_id>?persona_id=`：移除插件快照，不影响原版 persona
- `POST /api/snapshots/<snapshot_id>/patch`：从快照正文生成 pending 补丁草案，不直接应用到 persona

`POST /api/snapshots` 不传 `content` 时，会从当前 AstrBot persona 读取 `system_prompt` 创建快照，不会修改 persona。

### Profiles

- `GET /api/profiles`：列出 persona profile，并附 observation/patch 计数
- `GET /api/profiles/<persona_id>`：查看某 persona 的 profile；不存在时返回空 profile
- `POST /api/profiles/<persona_id>`：更新某个人格的 display_name、archetype、notes、template 关联
- `PATCH /api/profiles/<persona_id>`：同上，更新 profile
- `DELETE /api/profiles/<persona_id>`：清空插件内 profile，不移除原版 persona

### 旧 SKILL 迁移

- `POST /api/migrate-skill`

用于从旧的 `/root/AstrBot/data/skills/persona-evolution` 迁移：

- `observation_notes_<pid>.md`
- `persona_patches_<pid>.json`
- `persona_current_<pid>.md`
- 人格模块资产

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
- modules 模块资产（HTTP 兼容名仍为 templates）
- snapshots/baselines prompt 快照
- profiles 人格补充资料

如果库异常或过大，可以在停用/停止插件后移除 `captures.sqlite3`。移除后只会丢失本插件保存的历史快照和归档数据，不会移除 AstrBot 原版 persona、长期记忆或核心配置。

## 数据模型

插件当前保留历史表名以兼容旧前端/API，但推荐按以下概念理解：

- `llm_request_captures`（旧文档有时简称 captures）：请求捕获历史，只记录实际发往 LLM 的请求快照，用于追溯上下文；不参与人格装配。
- `persona_observations`：人格观察。来源可以是人工反馈、LLM tool 留存或旧 skill 迁移；去重只针对完全相同的 `persona_id + source + content`。
- `persona_patches`：人格调整草案/审批/应用记录。它是审计历史，cleanup 不删除；只有人类审批并通过 base prompt 校验后才会调用 AstrBot `persona_manager.update_persona`。
- `persona_snapshots`：人格 prompt 版本快照/基线。用于回滚参考或生成 pending patch；不直接写原版 persona。
- `persona_templates`：兼容表名，新的概念名是 `persona_modules` / “模块资产”。`metadata.kind` 统一为 `module`；`metadata.module_id` 等于稳定的 `template_id`；`metadata.role` 表示 `meta/persona/system/nsfw/roleplay/ops/custom` 等装配角色；`metadata.source` 表示 `manual/skill-migration/data-cleanup` 等来源。
- `persona_module_links`：persona 与模块资产的装配关系，包含 `persona_id`、`template_id`、`role`、`enabled`、`order_index`、`notes`。它只是清单，不会自动修改 AstrBot persona；需要由模块清单生成 pending patch 后人工审批应用。
- `persona_profiles`：persona 的补充资料（显示名、原型、备注）。历史字段 `template_id` 仅为兼容旧 UI/API，不再承担模块关联职责；模块关联应使用 `persona_module_links`。

旧 `persona-evolution` skill 迁移策略：

1. observation 以 `source='skill-migration'` 归档，重复内容跳过。
2. patch 使用旧 `patch_id`；若旧数据没有 `patch_id`，则基于 patch 内容生成稳定 ID，避免重复灌入。
3. `persona_current_<pid>.md` 作为 snapshot，ID 包含内容 sha256 前缀，重复快照跳过。
4. 旧 skill 文件迁入 `persona_templates` 但按模块资产管理，稳定 ID 形如 `skill-meta_preamble`、`skill-nsfw_module`。
5. 对 `lingjiu-2` 幂等补齐默认 `persona_module_links`；这一步只写插件库的装配清单，不写 AstrBot 原版 persona。

## 容量控制与自 DoS 修复

插件会按 `max_records` 保留捕获记录。数据库超过 50 MiB 时，会按有限批次移除最早记录，并在循环外受限执行 `VACUUM`。

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

## 聊天指令与 LLM Tools

当前版本不暴露聊天指令。

## LLM Tool 边界

本插件会暴露一组受限 LLM Tools，用于延续旧 SKILLS 模式里的“观察 / 起草 / 查询 / 留存”工作流。

安全边界：

- LLM Tools 可以记录 observation、查看摘要、起草 pending patch、留存当前快照。
- LLM Tools 默认不返回完整 `system_prompt`、补丁正文或敏感模块正文；只有个别查看工具在显式 `include_content=true` 时才会返回更多内容，且敏感模块仍会隐藏。
- LLM Tools 不提供 apply 能力，不能审批或应用补丁；真正写入 persona 的入口仍在前端人格工作台，由人类点击“审批 / 应用 / 直接审批并应用”触发。
- 所有会写入插件数据的工具都要求明确 `persona_id`，不会默认写到某个固定人格。

当前工具：

- `persona_sublimation_list_personas()`：列出 persona 摘要、prompt 长度、观察/补丁/快照计数。
- `persona_sublimation_add_observation(persona_id, content, source='', interpretation='', emotion='')`：记录一条观察。
- `persona_sublimation_list_observations(persona_id, limit=10)`：列观察摘要。
- `persona_sublimation_create_patch_draft(persona_id, trigger, proposed_prompt='', changes_json='', notes='', base_prompt='')`：起草 pending 调整，不审批不应用；结构化调整项以 JSON 字符串传入，避免 array schema 兼容问题。
- `persona_sublimation_list_patches(persona_id, status='', limit=10)`：列调整摘要。
- `persona_sublimation_get_patch(patch_id, include_content=false)`：查看调整；默认不返回完整 base/proposed prompt。
- `persona_sublimation_create_snapshot(persona_id, label='', description='')`：留存当前人格快照，不修改 persona。
- `persona_sublimation_list_snapshots(persona_id, limit=10)`：列快照摘要。
- `persona_sublimation_list_templates(limit=20)`：列模块摘要，不展开正文。
- `persona_sublimation_get_template(template_id, include_content=false)`：查看模块；默认不展开正文，敏感模块仍隐藏。
- `persona_sublimation_list_persona_modules(persona_id)`：列当前 persona 的模块关联清单。
- `persona_sublimation_link_module(persona_id, template_id, role='', enabled=true, order_index=0, notes='')`：把模块关联到 persona；只记录关系，不修改 persona。
- `persona_sublimation_unlink_module(persona_id, link_id)`：解除模块关联，不删除模块本体。
- `persona_sublimation_create_patch_from_modules(persona_id, notes='')`：由当前启用模块清单起草 pending 调整。
- `persona_sublimation_generate_patch_from_snapshot(persona_id, snapshot_id, trigger='')`：由快照起草 pending 调整。
- `persona_sublimation_generate_patch_from_template(persona_id, template_id, trigger='')`：由模块起草 pending 调整。

代码中仍保留只读捕获 hook：

```python
@filter.on_llm_request(priority=-100)
```

用于只读捕获 LLM 请求快照。普通聊天不会新增 slash/text 指令。

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
