# 人格升华操作逻辑

本文档约定前端工作台是“人类操作地点”，LLM Tools 是辅助记录、查询和起草的入口。任何自动化入口都不能绕过人类审批把内容写入 AstrBot 原版 persona。

## 前端 IA 与用户路径

```text
当前人格
  ├─ 选择 persona_id / 查看状态摘要 / 查看当前提示词
  └─ 留存当前版本（只写插件快照）

观察
  ├─ 记录反馈或会话观察
  ├─ 保存记录 / 更新记录
  └─ 移除无效记录

模块
  ├─ 模块库：收纳、查看、更新、移除模块
  ├─ 关联到当前人格：选择模块、角色、顺序、备注
  ├─ 当前人格模块清单：启用/停用、上移/下移、解除关联
  └─ 由模块清单起草调整：生成 pending 草案，不直接应用

调整
  ├─ 起草调整 / 保存草案 / 放弃草案
  ├─ 查看 diff 和状态
  ├─ 审批：pending -> approved
  ├─ 应用到人格：approved -> applied，并写入 AstrBot persona
  └─ 直接审批并应用到人格：一次人类确认完成 approve + apply

版本
  ├─ 留存当前版本：保存当前 system_prompt 快照
  ├─ 查看/改备注/移除快照
  └─ 由版本起草调整：生成 pending 草案，不直接应用

档案
  ├─ 保存资料：显示名、原型、备注、关联模块 ID
  └─ 清空资料：只清插件 profile

请求记录
  └─ 只读排查：查看捕获的 LLM 请求、上下文和工具信息
```

### 模块到人格的标准路径

```text
模块库
  -> 关联到当前人格
  -> 当前人格模块清单（排序、角色、启用/停用）
  -> 由模块清单起草调整
  -> 人类检查草案/diff
  -> 审批
  -> 应用到人格
```

模块关联是“装配关系”，不是写入行为。模块库查看、模块关联、模块启停、快照留存、由模块/版本起草调整都只写插件自己的 SQLite 数据，不会直接改 AstrBot 原版 persona。

## 真正写入 AstrBot 原版 persona 的动作

只有以下前端人类确认动作会调用 `persona_manager.update_persona(...)`：

1. **应用到人格**：对已审批草案执行 apply。
2. **直接审批并应用到人格**：同一次人类确认中先审批 pending 草案，再应用。

这两个动作都必须通过 base prompt 检查，避免覆盖已被其他地方改变的人格。除此之外，观察、模块、版本、档案、请求记录和 LLM Tools 都不直接写入原版 persona。

## LLM Tool 操作边界

LLM Tools 用于延续旧 SKILLS 模式中的辅助工作，但权限低于前端人类操作：

允许：

- 记录观察。
- 查询 persona 状态摘要。
- 起草 pending 调整。
- 列出模块库摘要。
- 关联模块到 persona。
- 查看当前 persona 模块清单。
- 由模块清单起草 pending 调整。
- 由快照/模块起草 pending 调整。
- 创建和列出快照摘要。

禁止/不提供：

- 不提供 approve/apply 工具。
- 不提供“直接审批并应用”工具。
- 默认不返回完整 `system_prompt`、完整补丁正文或敏感模块正文。
- 不默认选择某个 persona 写入；所有写插件数据的工具都要求显式 `persona_id`。

工具返回以摘要为主：`id`、状态、长度、hash、时间、说明和 preview；敏感正文需要人类在前端确认查看。

## Schema 约束

OpenAI function/tool schema 中 `type: array` 必须带 `items`。为避免 AstrBot docstring/类型推导产生 invalid schema，LLM Tool 入参尽量使用 `string`、`number`、`boolean` 等标量。结构化调整项通过 `changes_json` 字符串传入，由后端解析为数组保存。
