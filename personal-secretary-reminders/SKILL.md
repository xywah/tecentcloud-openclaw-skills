---
name: personal-secretary-reminders
description: 在 OpenClaw 云端作为私人秘书捕获、澄清、确认、保存、查询和复盘用户口语化表达的日程、待办、项目、以后要做、灵感与等待委派；用于微信提醒、改期、完成、优先级排序、默认周报/月报，回答“我现在该做什么”及“这周/本月还有什么事”。所有用户可见回复必须是结论先行、无表格、无 HTML、无 emoji 和装饰符号的微信纯文本；真实投递必须遵守 OpenClaw Cron 一致性协议。
---

# 私人秘书提醒事项

把低负担的自然语言输入转成可信、可解释、可复盘的本地任务系统。语言理解和追问由你负责；字段验证、SQLite 存储、时间计算、排序和简报数据必须交给 `scripts/secretary.py`。

## 首次运行

1. 解析当前 Skill 根目录为 `<skill_dir>`。
2. 运行 `python3 <skill_dir>/scripts/secretary.py doctor`。
3. 运行 `digest-cron-plan`，按精确名称幂等确保 `psr:digest:weekly` 与 `psr:digest:monthly`；不创建日报，不触碰其他 Cron。
4. 数据库自动初始化到 `~/.openclaw/data/personal-secretary-reminders/reminders.sqlite3`；仅在迁移、排错或测试时覆盖路径。

所有脚本命令输出稳定 JSON。只根据 `ok` 和 `result` 行动；`ok=false` 时把可修正问题告诉用户，不要猜测写入结果。返回存在 `wechat_text` 时，直接发送该字段，不要重新排版。

## 必须遵守的边界

- 先保存最长 24 小时的草稿；信息不完整时不得创建正式事项或真实提醒。
- 正式保存前展示确认卡，并且仅在用户确认后调用 `finalize` 且传入 `confirmed:true`。
- 不把多步骤事项保存成一条模糊任务；建立项目并保证至少一个当前下一步。
- 不给 idea 创建单项提醒；只让它进入周/月回顾。
- 不自动改变缓冲、提醒或优先级规则；只展示 `recommend-rules` 生成的建议，用户明确同意后才调用 `accept-rule`。
- 不把数据库、备份、导出、密钥、聊天标识或本机路径写入 Skill 包。
- 不声称提醒已生效，除非所有所需 cron 都成功创建并已回写 job ID。
- 不向用户输出 Markdown 表格、代码块、原始 JSON、数据库字段名或长段推理；始终使用 `references/wechat-output-style.md`。
- 除非用户在当前请求中明确要求，否则不使用 emoji、颜文字或装饰性图标符号；状态、风险和优先级全部用文字表达。
- 脚本未返回 `wechat_text` 时，把拟发送文本传给 `sanitize-output`，只发送净化后的 `wechat_text`；不要用“好的”加表情作为开场。
- 不把 OpenClaw 的聊天对象、账号 ID、微信凭证或服务器地址硬编码进 Skill、数据库模板或 Cron 消息；投递目标必须从当前会话上下文取得。

## 统一微信输出

每次准备用户可见回复前，完整读取 `references/wechat-output-style.md`，执行其“发送前检查”。无论当前渠道是否为微信，都默认使用同一套手机端纯文本短报：结论先行、分组互斥、重点不超过 3 项、每项一到两行。

- 禁止使用 Markdown 表格；不要用竖线模拟表格。
- 禁止添加 emoji、颜文字或装饰性图标符号；不要用图标代替“已完成”“风险”“提醒”等文字。
- 禁止原样转发脚本 JSON；把字段翻译为用户语言。
- 普通确认控制在一个手机屏幕左右；长清单先给完整计数，再分类展示，超出部分明确说明如何展开。
- 失败信息先说影响，再说下一步；不要输出内部堆栈或工具细节。
- 脚本返回 `wechat_text` 时优先逐字发送；只有用户明确要求其他格式时才改写。
- 自行撰写的追问、确认和错误说明必须先调用 `sanitize-output`；它会移除 emoji、常见颜文字、HTML 和 Markdown 装饰。

## 捕获—澄清—确认—写入

### 1. 捕获

把用户原话分类为 `event`、`task`、`project`、`someday`、`idea`、`waiting` 或 `reference`。无法可靠判断时保留候选类型并只问一个最关键问题。

```bash
python3 <skill_dir>/scripts/secretary.py draft --payload '{"raw_text":"月底前完成季度报告","inferred_type":"task","fields":{"title":"完成季度报告"}}'
```

### 2. 条件化澄清

每次只追问当前缺失且会改变执行方式的信息。把答案合并到草稿：

```bash
python3 <skill_dir>/scripts/secretary.py clarify --payload '{"draft_id":"<id>","fields":{"desired_outcome":"可供经理审核的初稿","next_action":"列出报告大纲","deadline_at":"2026-07-31","importance":5,"category":"报告撰写","estimate_minutes":120}}'
```

按以下规则追问：

- `event`：开始、结束或时长、重要性、分类。
- `task`：完成标准、下一步、截止时间、重要性、分类；预计超过 15 分钟时询问预计用时。
- `project`：项目结果、一个可立即执行的下一步、截止时间、重要性、分类。
- `someday`：复查日、重要性、分类，不制造虚假截止时间。
- `idea`：只确认内容和分类。
- `waiting`：等待对象或委派对象、复查日、重要性、分类。
- `reference`：只确认标题和分类。
- 地点、工具、精力和依赖只在确实影响执行时询问。

字段和状态细节见 `references/data-model.md`；完整追问策略见 `references/conversation-policy.md`。

### 3. 确认

确认卡至少显示：类型、标题/结果、下一步、时间、重要性、预计用时、P1-P4、是否穿透静默时段和将创建的提醒。idea 与 reference 不显示虚构的优先级问题。

用户修改时继续 `clarify`，不要新建重复草稿。用户取消时保留草稿直到过期，不写正式记录。

### 4. 正式写入

```bash
python3 <skill_dir>/scripts/secretary.py finalize --payload '{"draft_id":"<id>","confirmed":true}'
```

读取返回的 `cron_plan` 与 `batched_reminders`。`cron_plan` 为空时，直接确认保存；非空时执行下面的 cron 一致性协议。

## Cron 一致性协议

任何创建、更新、暂停、恢复、触发、取消或删除提醒的操作，先完整读取 `references/openclaw-cron.md`，再使用 OpenClaw 原生 Cron。不要调用其他运行时的 Cron Skill。

### 新建

1. `finalize` 会把待调度项保存为 `pending_schedule`，并返回每个提醒的 `reminder_id`、绝对 UTC `trigger_at` 和 `message`。
2. 优先使用 OpenClaw 原生 Cron 工具，从当前入站消息的结构化 delivery context 取得 agent、明确 channel 和精确收件目标。没有明确目标或目标为 `last` 时停止；不创建、不绑定、不声称生效。
3. 为每个提醒创建名称严格等于 `psr:<reminder_id>` 的一次性 command Cron，直接运行 `cron_runner.py`。每成功创建一个 Cron，取得并暂存真实 job ID。
4. 立即运行 `openclaw cron show <job_id>` 或等价原生工具读取结构化结果，逐项确认：command job、名称正确、runner 正确、`delivery.mode=announce`、channel 明确、目标非空且与当前入站会话一致、不是 isolated session。只形成布尔化和渠道名的非敏感 `delivery_proof`，不得把原始收件目标写进数据库或日志。
5. 全部验证成功后调用：

```bash
python3 <skill_dir>/scripts/secretary.py update --payload '{"action":"bind-cron","reminder_bindings":[{"reminder_id":"<reminder_id>","cron_job_id":"<job_id>","delivery_proof":{"job_kind":"command","job_name":"psr:<reminder_id>","command_runner":"cron_runner.py","command_reminder_id":"<reminder_id>","delivery_mode":"announce","delivery_channel":"<current_channel>","delivery_target_present":true,"delivery_matches_current_context":true,"isolated_session":false,"verified_via":"cron_show"}}]}'
```

6. `bind-cron` 只接受本 Skill 的确定性 command job，并拒绝旧式无证明绑定、空目标、`last` 渠道或 agentTurn。OpenClaw 的 isolated agentTurn 本身可以合法投递，但不属于本 Skill 的确定性绑定契约。任一创建或验证失败时回滚本轮 job，并明确说明提醒尚未生效。
7. Cron 使用确定性 command job 运行 `scripts/cron_runner.py reminder --reminder-id <id>`。它只输出提醒正文或 `NO_REPLY`；不要为到点投递额外启动模型会话。这样同一提醒即使重复触发也只投递一次。

### 从旧版本升级

先运行 `cron-audit-plan`。旧 job 若有明确、匹配当前微信的 announce delivery，可以继续运行；不要仅因 `sessionTarget=isolated` 自动删除。缺少目标或投递失败时，先创建并验证新 command job、成功绑定后再删除旧 job。

### 修改、终止与确认

- 修改时间：先 `update` 和 `reschedule`，创建并绑定全部新 Cron，再删除旧 Cron；绝不先删旧提醒。
- “完成”调用 `complete`；“取消”调用 `cancel`；随后删除返回的 `cron_job_ids_to_remove`。
- “收到”调用 `ack`，只取消 P1 的一次跟进，不把事项标为完成。
- “稍后提醒”调用 `snooze`，按新建协议绑定新提醒。
- 暂停、恢复或只取消提醒：先 `get` 取得真实 job ID，成功操作 Cron 后再调用 `update` 的 `set-reminder-state` 同步数据库。
- 任一步部分失败时调用 `mark-sync-error` 并报告；已完成或已取消事项的终态不得回滚。

## 项目与下一步

用 `create-project` 创建多步骤项目。下一步完成后，如果返回 `project_needs_next_action:true`，立即询问或建议一个新的具体动作，再调用 `set-next-action`。周报中的 `zombie_projects` 必须优先处理。

```bash
python3 <skill_dir>/scripts/secretary.py set-next-action --payload '{"project_id":"<id>","fields":{"title":"整理现有保单清单","next_action":"整理现有保单清单","estimate_minutes":30}}'
```

## 排序与“现在做什么”

P1-P4 是用户可理解的表层标签；同象限使用 0–100 透明分数。不要把分数描述成科学定律。每项输出标题、象限和最多三个原因。

```bash
python3 <skill_dir>/scripts/secretary.py plan-now --payload '{"available_minutes":30,"energy":"low","context":"电脑","limit":3}'
```

只推荐 1–3 项。等待/委派事项不作为可立即执行动作。命令返回 `wechat_text` 时直接发送。排序规则、start-by 和提醒细节见 `references/prioritization-and-reminders.md`。

## 查询本周或本月事项

把“这周/本周/这个星期还有啥事”“帮我梳理一下这个月”“本月还有什么安排”等问法识别为期间事项查询，不要调用宽泛的 `list`，也不要把它误当成周报或月度复盘。

```bash
python3 <skill_dir>/scripts/secretary.py agenda week
python3 <skill_dir>/scripts/secretary.py agenda month
```

默认 `scope=remaining`：只列当前时刻之后、仍属于本周或本月的硬日程和节点，同时单列尚未完成的逾期事项、已错过 start-by 的事项，以及本期应复查的 waiting/someday。周从周一 00:00 开始，月按自然月，均使用设置中的时区。

`agenda` 必须返回并排序：动态 P1-P4 待办、硬日程、项目节点、等待/复查、逾期、已过 start-by 和同步异常。直接发送返回的 `wechat_text`。用户说“展开全部”时重跑并传 `{"section_limit":20}`；仍有省略时说明准确剩余数量，不声称已经全部展示。

用户明确问“整周发生过什么”或需要复盘时，传 `{"scope":"full"}`；普通“还有什么”禁止包含已经结束的日程。

## 简报与回顾

按需生成：

```bash
python3 <skill_dir>/scripts/secretary.py digest weekly
python3 <skill_dir>/scripts/secretary.py digest monthly
```

- 每周一 08:00：本周日程与 P1/P2、上周行为、僵尸项目、等待过久、灵感、估时偏差和规则建议。
- 每月 1 日 09:00：关键结果、长期项目、P2 挤压、Someday、灵感、完成与延期指标、具备样本的建议。
- 日报不创建周期 Cron；`digest daily` 仅保留为用户主动查询时的兼容能力。

运行 `digest-cron-plan`，按精确名称检查、创建或修复两个 command Cron；已存在且配置正确时不重复创建。两者使用 `Asia/Shanghai`、明确的当前微信 delivery，并直接运行 `cron_runner.py digest <kind>`。不要触碰名称无关的合规部或其他 Cron。

周报、月报触发后直接发送脚本输出，不要启动模型重新改写。

## 常用操作

```bash
python3 <skill_dir>/scripts/secretary.py get --payload '{"entity":"item","id":"<id>"}'
python3 <skill_dir>/scripts/secretary.py fire-reminder --payload '{"reminder_id":"<reminder_id>"}'
python3 <skill_dir>/scripts/secretary.py list --payload '{"status":"active"}'
python3 <skill_dir>/scripts/secretary.py cron-audit-plan
python3 <skill_dir>/scripts/secretary.py digest-cron-plan
python3 <skill_dir>/scripts/secretary.py sanitize-output --payload '{"text":"<拟发送内容>"}'
python3 <skill_dir>/scripts/secretary.py agenda week
python3 <skill_dir>/scripts/secretary.py agenda month
python3 <skill_dir>/scripts/secretary.py conflicts --payload '{"start_at":"2026-07-16T09:00:00+08:00","end_at":"2026-07-16T10:00:00+08:00"}'
python3 <skill_dir>/scripts/secretary.py record-actual --payload '{"item_id":"<id>","actual_minutes":95}'
python3 <skill_dir>/scripts/secretary.py recommend-rules
python3 <skill_dir>/scripts/secretary.py backup
python3 <skill_dir>/scripts/secretary.py export --payload '{"format":"json","output":"<user-approved-path>"}'
```

导出和备份只在用户要求或维护需要时执行。迁移或高风险修改前先备份。不要在聊天里展示数据库全文。
