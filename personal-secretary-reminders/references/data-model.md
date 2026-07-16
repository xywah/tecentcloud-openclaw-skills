# 数据模型与脚本接口

## 设计边界

数据库默认位于 `~/.openclaw/data/personal-secretary-reminders/reminders.sqlite3`。使用 UTC ISO 时间存储，按 `settings.timezone` 展示；默认 `Asia/Shanghai`。SQLite 启用 WAL、外键、10 秒 busy timeout 和事务。

Skill 包内不保存数据库、缓存、备份、密钥、渠道 ID 或用户聊天内容。测试通过 `--db` 或 `PERSONAL_SECRETARY_DB` 指向临时目录。

## 实体

### projects

保存多步骤事项的期望结果。活跃项目必须拥有 `current_next_action_item_id`；为空即僵尸项目。

核心字段：`id`、`title`、`desired_outcome`、`status`、`deadline_at`、`current_next_action_item_id`、`category`、`importance`。

### items

类型：`event`、`task`、`someday`、`idea`、`waiting`、`reference`。

状态：`draft`、`pending_schedule`、`active`、`scheduled`、`waiting`、`delegated`、`someday`、`completed`、`cancelled`、`archived`、`sync_error`。

核心字段：结果与动作、分类、重要性、截止/开始/结束/复查/start-by、预估与实际用时、地点/工具、精力、等待或委派对象、解锁与依赖数量、静默穿透、P1-P4 与评分解释。

### reminders

`pending_cron` 表示数据库已有计划但真实 cron 尚未完成核验与绑定；`active` 表示 job ID 及非敏感 `delivery_proof` 已通过 fail-closed 验证；`batched` 表示并入简报；`cancelled`/`sent` 表示终态。原始收件目标不进入 reminders 或 audit_log。

### drafts

草稿默认 24 小时过期。状态从 `draft` 到 `clarifying`，用户确认后为 `finalized`，超时为 `expired`。

### behavior_events / audit_log

前者记录完成、确认、延期、忽略、实际用时和同步错误等行为，用于回顾与校准；后者保存创建、修改、删除和规则变化的最小审计信息。

### settings / recommendations

`settings` 保存时区、静默时段、简报时间、工作时段、默认 1.25 缓冲和 8 样本阈值。`recommendations` 保存证据、建议值与 pending/accepted/rejected 状态；只有 `accept-rule` 会修改设置。

## JSON CLI 约定

```text
python3 scripts/secretary.py <command> [argument] --payload '<JSON object>' [--db PATH] [--now ISO]
```

成功：`{"ok":true,"result":...}`，退出码 0。

字段/状态错误：`{"ok":false,"error":{"type":"validation_error",...}}`，退出码 2。

数据库错误：`database_error`，退出码 3。

`--now` 仅用于确定性测试与故障复现，正常对话不得伪造当前时间。

## 命令责任

| 命令 | 作用 |
|---|---|
| `init`, `doctor` | 初始化、完整性与版本检查 |
| `draft`, `clarify`, `finalize` | 草稿、补充字段、确认写入 |
| `create-project`, `set-next-action` | 项目容器和当前下一步 |
| `get`, `list`, `update` | 查询和字段更新；`update action=bind-cron` 仅在 delivery proof 完整时回写 job ID |
| `cron-audit-plan` | 列出升级后必须用 `cron show` 核验或重建的未来 Cron |
| `complete`, `cancel`, `ack` | 完成、取消、收到并返回待删除 cron ID |
| `snooze`, `reschedule`, `mark-sync-error` | 稍后提醒、先建后删改期、显式同步故障 |
| `conflicts`, `plan-now` | 日程冲突和上下文推荐；`plan-now` 同时返回微信短报 |
| `agenda week|month` | 查询本周或本月剩余事项、逾期风险和复查节点，并返回微信短报 |
| `digest daily|weekly|monthly` | 结构化简报数据和 `wechat_text` |
| `record-actual`, `recommend-rules`, `accept-rule` | 实际用时与经确认的规则校准 |
| `backup`, `export` | 一致性备份与 JSON/CSV 导出 |

## 用户可见输出契约

`agenda`、`plan-now` 和三类 `digest` 返回：

- `wechat_text`：可直接发送的纯文本短报。
- `output_contract.format=wechat_plain_text_v1`。
- `output_contract.markdown_tables=false`。
- `output_contract.emoji=false`。

Agent 必须直接使用 `wechat_text`，不得把结构化字段重新渲染成表格。结构化数组保留给排序、验证和“展开全部”重查使用。

## 代码、数据与公开仓库边界

- 公开 GitHub 仓库和 SkillHub ZIP：只含 `SKILL.md`、`scripts/`、`references/` 及仓库级源码、测试和版本说明。
- 正式事项：默认只在运行 OpenClaw 的服务器 `~/.openclaw/data/personal-secretary-reminders/reminders.sqlite3`。
- 备份和导出：只写到用户明确选择的服务器路径，不进入 Skill 目录。
- Cron job：保存在 OpenClaw Gateway 的调度状态中；微信凭证和聊天路由保存在 OpenClaw 配置中。

打包或提交前必须拒绝 `.sqlite`、`.sqlite3`、`.db`、备份、导出、密钥、环境文件和本机绝对路径。升级 Skill 目录不会迁移、上传或删除 SQLite 数据。

## 迁移原则

`meta.schema_version` 是唯一版本来源。升级脚本前先运行 `backup`；每次迁移放在一个事务中，失败必须回滚并保持原版本。首版 schema version 为 1。
