# 模块格式规范

模块是插件内唯一的 prompt 片段资产。历史存储表仍为 `persona_templates`，历史 API 仍为 `/api/templates`，但语义统一为 module。模块不是最终 prompt 成品，不会直接应用到 AstrBot persona；必须先关联到某个 persona 的模块清单，再由清单起草调整草案，经人类审批后才能写回原版 persona。

## 稳定命名

- `module_id` 是模块稳定 ID；兼容存储字段为 `template_id`。
- `template_id` 与 `metadata.module_id` 应保持一致。旧 API 可继续传 `template_id`，新代码和文档优先称 `module_id`。
- ID 建议只用小写字母、数字、`-`、`_`，避免空格和中文。例如：
  - `skill-meta_preamble`
  - `skill-persona_lingjiu-2`
  - `manual-tone-short-reply`
- 旧 skill 迁移资产使用 `skill-<旧文件 stem>`，便于追溯来源，不在清洗阶段重命名。

## 名称与展示名

- 表字段 `name` 保存短名称，适合列表展示和人工识别。
- 可选 `metadata.display_name` 保存更友好的展示名；缺省时前端使用 `name`。
- `description` 用于说明模块用途、适用范围和注意事项，不放模块正文。

## metadata 字段

规范化后的 `metadata_json` 至少包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | string | 固定为 `module`。若旧值不是 `module`，保留到 `legacy_kind`。 |
| `module_id` | string | 稳定模块 ID，应等于 `template_id`。 |
| `role` | string | 模块角色：`meta` / `persona` / `system` / `nsfw` / `roleplay` / `ops` / `custom`。 |
| `source` | string | 来源：`manual` / `skill-migration` / `imported` / `data-cleanup`。缺省手写模块为 `manual`，`skill-*` 或来自 `persona-evolution` 的模块为 `skill-migration`。 |
| `content_sha256` | string | UTF-8 正文 SHA-256，用于确认正文未漂移。 |
| `sensitive` | boolean | 敏感模块标记。`nsfw`、`ops`、带 `intimate` tag 的模块必须为 `true`。 |
| `tags` | string[] | 可选标签，例如 `nsfw`、`ops`、`intimate`。 |
| `recommended_order` | number | 推荐装配顺序。只是提示，不等同于 persona 清单里的 `order_index`。 |
| `notes` | string | 可选维护备注，不放正文。 |

### role 约定

- `meta`：装配说明、结构约束、模块组合前言。
- `persona`：角色人格设定。`persona_intimate_*` 选择 `role=persona`，并用 `tags=["intimate"]` 与 `sensitive=true` 标记亲密/敏感属性，不额外扩展 `intimate` role。
- `system`：通用系统规则或硬约束。
- `nsfw`：NSFW 片段。
- `roleplay`：角色扮演片段。
- `ops`：运维、待办、维护策略或风险备注。
- `custom`：暂无法归类的人工模块。

### recommended_order 建议

默认推荐顺序：

1. `meta`：10
2. 核心 `persona`：20
3. `persona` + `intimate` tag：25
4. `system`：30
5. `nsfw`：40
6. `roleplay`：50
7. `ops`：90
8. `custom`：100

真实应用顺序以 `persona_module_links.order_index` 为准；`recommended_order` 只帮助人类装配时理解模块位置。

## 旧 skill 文件 role 推断

从 `/root/AstrBot/data/skills/persona-evolution` 迁移到模块库时，当前规范按文件名推断：

| 旧文件 | module_id/template_id | role | source | sensitive/tags |
| --- | --- | --- | --- | --- |
| `meta_preamble.md` | `skill-meta_preamble` | `meta` | `skill-migration` | `sensitive=false` |
| `persona_lingjiu-2.md` | `skill-persona_lingjiu-2` | `persona` | `skill-migration` | `sensitive=false` |
| `system_rules.md` | `skill-system_rules` | `system` | `skill-migration` | `sensitive=false` |
| `nsfw_module.md` | `skill-nsfw_module` | `nsfw` | `skill-migration` | `sensitive=true`, `tags=["nsfw"]` |
| `roleplay_module.md` | `skill-roleplay_module` | `roleplay` | `skill-migration` | `sensitive=false` |
| `persona_intimate_lingjiu-2.md` | `skill-persona_intimate_lingjiu-2` | `persona` | `skill-migration` | `sensitive=true`, `tags=["intimate"]` |
| `pending_ops_module_lingjiu-2.md` | `skill-pending_ops_module_lingjiu-2` | `ops` | `skill-migration` | `sensitive=true`, `tags=["ops"]` |

## 正文格式建议

模块正文应是可装配的 prompt 片段，不是旧 SKILL 文件的执行说明。建议格式：

```text
# Module: <module_id>
# Role: <role>
# Notes: <一句话说明，避免历史迁移噪音>

<正文 prompt 片段>
```

清洗要求：

- 保留真正要参与起草的人格/规则/片段正文。
- 移除或转移旧 SKILL 的执行脚本说明、迁移历史、操作命令、维护日志。
- 不在正文里混入“如何运行脚本”“如何安装 skill”“历史 apply 记录”等说明；这些放到 `description` 或 `metadata.notes`。
- 不直接改真实库正文；先在临时 SQLite dry-run/apply 验证，再给出清洗计划，等维护者确认。

## 维护接口

- `POST /api/debug/modules/normalize`：模块 metadata 规范化维护接口，默认 dry-run；只返回 metadata diff、ID、名称、正文长度和 hash 前缀，不返回正文。
- 只有显式传 `{"apply": true}`、`{"commit": true}` 或 `{"write": true}` 才写插件库。
- 该接口不进入普通前端主流程，不启用插件、不 reload 时可在临时库脚本中直接调用同名内部函数验证幂等。
