# 人格升华（astrbot_plugin_persona_sublimation）

人格升华是 AstrBot persona 的离线编排与审核插件。统一后的核心路径只有一条：

```text
选人格 → 配模块 → 生成调整草案 → 看差异 → 审批/应用到人格
```

原版 AstrBot persona 是唯一真源。插件不复制 persona，只读取当前 prompt、记录辅助数据、生成 pending 调整草案；真正写回只发生在人类确认“应用到人格”或“直接审批并应用到人格”时，并通过 `persona_manager.update_persona(...)` 完成。

> 当前维护要求：插件关闭期间只能做离线维护、静态检查和临时 SQLite 测试。不要通过 Dashboard、reload、systemctl/journalctl、重启服务或访问实机端口来启用/验证插件。

## 核心模型

- **人格**：AstrBot 原生 persona，只读展示；应用草案时才写回。
- **模块**：唯一的 prompt 片段资产概念。历史表/API 可仍叫 `persona_templates` / `/api/templates`，但前端和文档统一称“模块”。
- **人格模块清单**：`persona_module_links` 记录某 persona 关联的模块、启停、顺序、角色、备注；它不会直接改 persona。
- **调整草案**：`persona_patches` 是唯一能最终写入 persona 的中间层；所有模块组合、版本回放、人工编辑都必须先生成草案。
- **版本记录**：`persona_snapshots` 只做留档、对照和回滚起草参考。
- **观察**：`persona_observations` 记录反馈和事实，给起草提供依据。
- **请求捕获**：`llm_request_captures` 只读排查，不进入人格编辑流。
- **档案**：`persona_profiles` 是可选备注，不承担模块关联。

详见 [`docs/unified_design.md`](docs/unified_design.md) 与 [`docs/module_format.md`](docs/module_format.md)。

## 前端工作台

普通工作台按六个页面组织：

1. **人格**：选择 persona、查看 prompt 摘要、留存版本、维护可选备注。
2. **模块装配**：管理模块库；把模块关联到当前人格；启用/停用/排序；由当前模块清单起草调整。
3. **调整草案**：查看草案、diff、审批，并从这里应用到人格。这是唯一应用入口。
4. **版本记录**：查看快照、改备注、由版本起草调整。
5. **观察**：记录/查看观察，作为后续起草依据。
6. **请求捕获**：只读查看实际 LLM 请求快照，用于排查。

页面/API 无内置鉴权，会展示完整 prompts、contexts、工具信息和媒体 URL。只应在可信网络或受控反向代理后使用。

## HTTP API

### 普通用户接口

#### 人格

- `GET /api/personas`：列出 persona 摘要。
- `GET /api/personas/<persona_id>`：读取原版 persona 当前详情。
- `POST /api/snapshots`：留存当前 persona 版本。

#### 模块装配

历史路径保留 `/api/templates` 兼容名，含义统一为“模块”。

- `GET /api/templates`：列出模块。
- `POST /api/templates`：创建模块，metadata 会规范为 `kind=module`。
- `GET /api/templates/<module_id>`：查看模块。
- `PATCH/POST /api/templates/<module_id>`：更新模块。
- `POST /api/templates/<module_id>/patch`：由单个模块起草 pending 调整。
- `GET /api/personas/<persona_id>/modules`：列出当前人格模块清单。
- `POST /api/personas/<persona_id>/modules`：关联模块到当前人格。
- `PATCH /api/personas/<persona_id>/modules/<link_id>`：调整角色、启停、顺序、备注。
- `DELETE /api/personas/<persona_id>/modules/<link_id>`：解除关联，不删除模块本体。
- `POST /api/personas/<persona_id>/modules/patch`：由已启用模块清单起草 pending 调整。

#### 调整草案

- `GET /api/patches?persona_id=<persona_id>`：列草案。
- `POST /api/patches`：创建 pending 草案。
- `GET /api/patches/<patch_id>`：查看草案详情和 diff。
- `PATCH/POST /api/patches/<patch_id>`：更新 pending 草案。
- `POST /api/patches/<patch_id>/approve`：审批 pending 草案。
- `POST /api/patches/<patch_id>/apply`：应用已审批草案；若请求体显式 `auto_approve=true`，表示同一次人类确认中直接审批并应用。
- `DELETE /api/patches/<patch_id>`：仅放弃 pending 草案。

应用时后端会校验当前 persona prompt 与草案 `base_prompt` 一致，避免覆盖并发改动。

#### 版本记录

- `GET /api/snapshots?persona_id=<persona_id>`：列版本。
- `GET /api/snapshots/<snapshot_id>`：查看版本。
- `PATCH/POST /api/snapshots/<snapshot_id>`：更新标签/备注。
- `DELETE /api/snapshots/<snapshot_id>`：移除插件快照。
- `POST /api/snapshots/<snapshot_id>/patch`：由版本起草 pending 调整。

#### 观察

- `GET /api/observations?persona_id=<persona_id>`：列观察。
- `POST /api/observations`：记录观察。
- `GET /api/observations/<id>`：查看观察。
- `PATCH/POST /api/observations/<id>`：更新观察。
- `DELETE /api/observations/<id>`：移除观察。

#### 档案备注

- `GET /api/profiles`：列档案摘要。
- `GET /api/profiles/<persona_id>`：查看档案。
- `POST/PATCH /api/profiles/<persona_id>`：保存显示名、原型、备注、metadata。`template_id` 仅为旧字段兼容，不作为模块关联。
- `DELETE /api/profiles/<persona_id>`：清空插件档案。

#### 请求捕获

- `GET /api/captures?limit=50&session_id=<session_id>`：只读列捕获。
- `GET /api/captures/<id>`：只读查看捕获详情。
- `GET /api/sessions`：列 session 摘要。

### 维护接口

维护接口不进入普通前端主流程：

- `GET /api/debug/data-summary`：安全摘要，仅返回计数、ID、长度和 hash 前缀。
- `POST /api/debug/cleanup`：幂等整理数据模型，默认 dry-run；只有显式传 `{"apply": true}`、`{"commit": true}` 或 `{"write": true}` 才会写插件库。
- `POST /api/debug/modules/normalize`：只整理模块 metadata，默认 dry-run；返回 body-free metadata diff，不返回模块正文，不进入普通前端主流程。
- `POST /api/migrate-skill`：把旧 skill 文件导入为模块，人工确认后再关联到 persona。

cleanup 只整理插件 SQLite：规范模块 metadata、移除完全重复观察/快照。模块 normalize 只补齐/校正 `kind/module_id/role/source/content_sha256/sensitive/tags/recommended_order` 等 metadata，不返回正文。二者都不会删除 patch 历史，也不会修改 AstrBot 原版 persona。

## LLM Tools

LLM Tools 只允许查询、记录和起草，不允许审批、应用、删除或维护清理。

保留工具：

- `persona_sublimation_list_personas`
- `persona_sublimation_add_observation`
- `persona_sublimation_list_observations`
- `persona_sublimation_create_patch_draft`
- `persona_sublimation_list_patches`
- `persona_sublimation_get_patch`
- `persona_sublimation_create_snapshot`
- `persona_sublimation_list_snapshots`
- `persona_sublimation_list_modules`
- `persona_sublimation_get_module`
- `persona_sublimation_list_persona_modules`
- `persona_sublimation_link_module`
- `persona_sublimation_unlink_module`
- `persona_sublimation_create_patch_from_module`
- `persona_sublimation_create_patch_from_modules`
- `persona_sublimation_create_patch_from_snapshot`

不暴露：apply、approve、delete、cleanup、debug、profile delete。复杂数组入参使用 JSON string，避免工具 schema 出现缺少 `items` 的数组定义。

## 存储

默认数据库：`data/plugin_data/astrbot_plugin_persona_sublimation/captures.sqlite3`。

表：

- `llm_request_captures`：请求捕获。
- `persona_observations`：观察。
- `persona_patches`：调整草案/审批/应用审计。
- `persona_templates`：兼容表名，概念为模块；格式规范见 `docs/module_format.md`。
- `persona_module_links`：人格模块装配清单。
- `persona_snapshots`：版本记录。
- `persona_profiles`：可选档案备注。

不要直接改真实 `captures.sqlite3`。离线验证请复制到临时目录或使用临时 SQLite。

## 开发验证

离线可执行：

```bash
python -m py_compile main.py
ruff format --check .
ruff check .
node --check /tmp/persona_sublimation_index.js
```

可用临时 SQLite 验证模块 link/list/update、由模块起草草案、apply 的 base prompt 安全检查、cleanup dry-run 幂等。不要启用插件，不要 reload，不要访问实机端口。

## License

MIT
