# 人格升华统一设计

本设计把插件收敛为一条清晰的人类审核路径：

```text
选人格 → 配模块 → 生成调整草案 → 看差异 → 审批/应用到人格
```

插件只做辅助编排与留档。原版 AstrBot persona 始终是真源；插件不会复制 persona，也不会让观察、模块、版本或档案绕过草案层直接写入 persona。

## 核心概念定义

- **人格（personas）**：AstrBot 原生 persona。插件只读取当前 `system_prompt`、工具、技能等摘要；最终写回只能通过 AstrBot `persona_manager.update_persona(...)`。
- **模块（modules）**：唯一的 prompt 片段资产概念。模块可以是规则、角色设定、NSFW/RolePlay 片段、运维备注等，但对用户统一称为“模块”。历史表名/API 兼容 `persona_templates` / `/api/templates`。
- **人格模块清单（persona_module_links）**：某个人格启用了哪些模块、顺序、角色和备注。它只是装配清单，不会自动改变 persona。
- **调整草案（patches）**：唯一可以最终写入 AstrBot persona 的中间层。所有模块组合、版本回放、人工编辑都必须先形成 pending 草案，再由人类看 diff、审批、应用。
- **版本（snapshots）**：应用前后或人工留存的 prompt 版本记录，用于审计、对照和回滚起草参考。
- **观察（observations）**：来自主人反馈、会话复盘或调试捕获的事实记录，用于后续起草依据。
- **请求捕获（captures）**：只读调试记录，记录实际发往 LLM 的请求快照；不得混入人格编辑主流程。
- **档案（profiles）**：可选备注、显示名或原型信息。档案不承担模块关联职责，也不能直接改变 persona。

## 数据模型

### `personas`（只读外部真源）

来源：AstrBot 原版 persona 管理器。

职责：
- 提供当前 `persona_id`、`system_prompt`、工具/技能摘要。
- 在应用已审批草案时作为写回目标。

不该做：
- 插件不复制 persona 表。
- 模块、观察、快照、档案不能直接写入 persona。
- 不在插件内部维护“另一份人格真相”。

### `modules`（兼容表：`persona_templates`）

职责：
- 存储可复用 prompt 片段资产。
- metadata 统一：`kind=module`，并包含 `role`、`source`、`module_id`、`content_sha256`。
- 作为“由模块清单起草调整”的输入。

不该做：
- 不再对用户显示旧称；前端和内部语义统一为“模块”。
- 不直接应用到 persona。
- 不表达某个人格是否启用；启用关系必须放在 `persona_module_links`。

### `persona_module_links`

职责：
- 记录 `persona_id` 与 `module_id/template_id` 的装配关系。
- 维护 `enabled`、`order_index`、`role`、`notes`。
- 按启用与顺序生成组合草案。

不该做：
- 不删除模块本体。
- 不直接写 persona。
- 不由 `profiles.template_id` 替代。

### `patches`（表：`persona_patches`）

职责：
- 保存 pending/approved/applied 调整草案。
- 保存 `base_prompt`、`proposed_prompt`、`diff`、结构化 changes、审批人和应用时间。
- 执行应用前必须校验当前 persona prompt 与 `base_prompt` 一致。

不该做：
- cleanup 不删除 patch 历史。
- 未审批草案不能静默写入 persona。
- LLM Tool 不暴露 apply/approve/delete。

### `snapshots`（表：`persona_snapshots`）

职责：
- 留存 persona prompt 版本，用于审计、回滚参考和“由版本起草调整”。
- 以内容 hash 去重，避免重复快照膨胀。

不该做：
- 不直接覆盖 persona。
- 不替代 patch 审批链路。

### `observations`（表：`persona_observations`）

职责：
- 记录事实、反馈、解读、情绪标记和来源。
- 给后续起草调整提供证据。

不该做：
- 不直接改变 prompt。
- 不作为版本或模块资产存储。

### `captures`（表：`llm_request_captures`）

职责：
- 只读排查：保存 prompt、上下文、媒体 URL、工具信息、session/persona 摘要。

不该做：
- 不进入人格编辑流。
- 不自动生成或应用调整。
- 普通前端只提供查看，不提供清理/调试主按钮。

### `profiles`（表：`persona_profiles`）

职责：
- 保存显示名、原型、备注、metadata 等可选资料。
- 帮助人类理解 persona 背景。

不该做：
- 不承担模块关联；旧 `template_id` 字段仅为兼容，不再作为前端入口。
- 不直接参与 prompt 写入。

## 前端页面 IA

### 1. 人格

- 选择 persona。
- 查看当前 prompt 摘要和必要时展开的当前 prompt。
- 留存当前版本。
- 维护可选档案备注，但不在这里关联模块。

### 2. 模块装配

- 模块库：创建、查看、更新模块，统一显示为“模块”。
- 当前人格已启用模块：展示角色、启停状态、顺序、备注。
- 关联到当前人格：通过 `persona_module_links` 关联。
- 启用/停用/排序：只调整清单。
- 由模块清单起草调整：生成 pending 草案，不直接应用。

### 3. 调整草案

- 列出草案和状态。
- 查看 `base_prompt`、`proposed_prompt` 与 diff。
- 明确提示：这是唯一应用入口。
- 操作：审批、应用到人格、直接审批并应用到人格。

### 4. 版本记录

- 查看快照、编辑备注。
- 由版本起草调整，进入 pending 草案。
- 删除/移除只影响插件快照，不影响原版 persona。

### 5. 观察

- 记录/查看反馈、事实、解读和情绪标记。
- 观察只作为后续起草依据。

### 6. 请求捕获

- 按 session 或时间查看捕获请求。
- 只读排查，不进入应用链路。

## LLM Tool 边界

允许保留的安全工具：
- 查状态：`persona_sublimation_list_personas`。
- 记观察/列观察：`persona_sublimation_add_observation`、`persona_sublimation_list_observations`。
- 起草调整/列草案/看草案摘要：`persona_sublimation_create_patch_draft`、`persona_sublimation_list_patches`、`persona_sublimation_get_patch`。
- 列模块/看模块摘要：`persona_sublimation_list_modules`、`persona_sublimation_get_module`。
- 建快照/列快照：`persona_sublimation_create_snapshot`、`persona_sublimation_list_snapshots`。
- 模块清单：`persona_sublimation_list_persona_modules`、`persona_sublimation_link_module`、`persona_sublimation_unlink_module`。
- 由模块/模块清单/快照起草：`persona_sublimation_create_patch_from_module`、`persona_sublimation_create_patch_from_modules`、`persona_sublimation_create_patch_from_snapshot`。

禁止暴露给 LLM：
- approve/apply/direct apply。
- 删除 patch、删除模块、删除观察、删除 profile。
- cleanup/debug/数据维护接口。
- 展开完整 persona prompt 或敏感模块正文。

Schema 约束：
- 工具入参尽量使用 string/number/boolean。
- 复杂数组用 JSON string，例如 `changes_json`。
- 若 schema 出现 `type=array` 必须有 `items`。

## 迁移策略

- 旧 skill 资产进入“导入模块”：写入兼容表 `persona_templates`，metadata 规范为 `kind=module`，人工确认后再通过 `persona_module_links` 关联到 persona。
- 旧 `profile.template_id` 不再作为模块关联入口；模块关联统一迁到 `persona_module_links`。
- cleanup/debug 只作为维护接口，默认 dry-run，不进入普通前端主流程。
- 请求捕获保留只读排查，不自动生成或应用调整。
