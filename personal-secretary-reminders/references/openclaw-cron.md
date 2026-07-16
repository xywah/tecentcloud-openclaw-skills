# OpenClaw Cron 一致性协议

## 核心边界

业务记录以 SQLite 为准，定时投递以 OpenClaw Cron 为准。Skill 代码、事项数据、Cron 状态和聊天凭证必须分离：

- Skill：`<workspace>/skills/personal-secretary-reminders/`
- 数据：`~/.openclaw/data/personal-secretary-reminders/reminders.sqlite3`
- Cron：OpenClaw Gateway 的持久化调度状态
- 微信凭证：OpenClaw 通道配置

升级 Skill 只能替换第一项，不得覆盖其余三项。`PERSONAL_SECRETARY_DB` 只用于明确的数据迁移、测试或定制部署。

## 调度不变量

1. 所有时间由 `secretary.py` 生成绝对 UTC `trigger_at`，Cron 不重新理解“明天”等相对时间。
2. 任务名使用 `psr:<reminder_id>`，确保可唯一反查。
3. 收件目标只能取自当前入站会话的 delivery context；禁止把微信账号、sender ID、OpenID 或其他个人标识写入 Skill 文件。
4. 先建立新 Cron，确认真实 job ID 并绑定数据库，再删除旧 Cron。
5. 只在所有 job 创建成功后声称提醒已生效；部分失败必须回滚本轮 job，并把事项标为 `sync_error`。
6. 同一 Cron 创建或删除最多重试一次，避免静默制造重复任务。
7. 每次创建后按唯一名称或 job ID 查询校验；不得从非结构化日志中猜 job ID。
8. 缺少明确投递目标、渠道为 `last`、目标与当前入站会话不一致，或任务会进入 isolated agent session 时，一律按失败处理。
9. SQLite 只保存 job ID 和非敏感核验结论；不得保存原始收件目标、OpenID、sender ID 或聊天凭证。

## 首次检查

首次真实调度前运行：

```bash
openclaw cron status
openclaw cron add --help
```

Gateway 不可达时停止。不要自动执行 `doctor --fix`、修改插件、重启服务或改防火墙；这些属于服务器运维，不属于保存一条待办的权限范围。

## 一次性提醒

使用 OpenClaw command job 直接运行确定性脚本，不启动隔离 Agent 或模型会话。优先调用 OpenClaw 原生 Cron 工具，因为它可以继承并结构化读取当前入站消息的 delivery context。只有工具不可用、且 CLI 能明确取得 channel 与精确目标时才调用 CLI；无法取得目标不是可降级情况。CLI 逻辑模板如下，具体选项以服务器当前 `openclaw cron add --help` 为准：

```bash
openclaw cron add \
  --name "psr:<reminder_id>" \
  --at "<trigger_at>" \
  --command-argv '["python3","<skill_dir>/scripts/cron_runner.py","reminder","--reminder-id","<reminder_id>"]' \
  --command-cwd "<skill_dir>" \
  --announce \
  --channel "<current_channel>" \
  --to "<current_recipient>"
```

在微信私聊中，`current_channel` 应来自入站上下文，通常为 `openclaw-weixin`；`current_recipient` 必须是该条入站消息的实际回复目标，不能使用插件账号 ID 代替。一次性任务成功后是否自动删除，以服务器当前帮助为准。

创建后从工具返回值直接取 job ID；若 CLI 没有返回结构化 ID，运行 `openclaw cron list --json`，按完整名称 `psr:<reminder_id>` 精确匹配。必须恰好匹配一条，否则回滚并报告同步错误。

如果入站 delivery context 明确包含非默认 account，创建时同时传对应 `--account`；不得猜测或把 account 写进 Skill 文件。

## 创建后强制核验

每个新 job 创建后，立即运行 `openclaw cron show <job_id>` 或原生工具的等价读取操作。只有以下条件全部成立，才允许调用 `bind-cron`：

1. job 类型是 command，不是 agent turn 或 isolated session。
2. 名称严格等于 `psr:<reminder_id>`。
3. command 运行 `cron_runner.py`，且参数中的 reminder ID 与当前记录一致。
4. delivery mode 为 `announce`。
5. channel 是当前入站会话的明确渠道，不能是空值或 `last`。
6. 收件目标非空，并与当前入站会话的回复目标一致。

核验通过后，把结果规范化为下面的非敏感证明。`delivery_target_present` 和 `delivery_matches_current_context` 只保存布尔结论，禁止附带原始目标：

```json
{
  "reminder_id": "<reminder_id>",
  "cron_job_id": "<job_id>",
  "delivery_proof": {
    "job_kind": "command",
    "job_name": "psr:<reminder_id>",
    "command_runner": "cron_runner.py",
    "command_reminder_id": "<reminder_id>",
    "delivery_mode": "announce",
    "delivery_channel": "<current_channel>",
    "delivery_target_present": true,
    "delivery_matches_current_context": true,
    "isolated_session": false,
    "verified_via": "cron_show"
  }
}
```

`secretary.py update --payload '{"action":"bind-cron",...}'` 会再次执行 fail-closed 验证。旧式只传 `reminder_id` 和 `cron_job_id` 的绑定会被拒绝，这是 1.2.1 用来阻止“数据库显示成功但微信没有收到”的保护。

## 触发与去重

Command job 必须直接运行：

```bash
python3 <skill_dir>/scripts/cron_runner.py reminder --reminder-id <reminder_id>
```

- 首次有效触发：脚本只输出 `wechat_text`。
- 已触发、取消、暂停或终止：脚本只输出 `NO_REPLY`，由 OpenClaw 抑制投递。
- 命令失败：非零退出，让 Cron run 记录失败，不自行编造提醒正文。

不使用 `--command "..."` 拼接 shell 字符串；使用 `--command-argv` 固定参数数组，减少转义和注入风险。

## 周期简报

周期任务使用 `--cron`、`--tz Asia/Shanghai` 和同样的 `--command-argv` 投递方式，并保持相同的 channel、recipient 和必要 account：

- 每日简报：`0 9 * * *`
- 每周简报：`0 8 * * 1`
- 每月简报：`0 9 1 * *`

command argv 分别运行：

```text
python3 <skill_dir>/scripts/cron_runner.py digest daily
python3 <skill_dir>/scripts/cron_runner.py digest weekly
python3 <skill_dir>/scripts/cron_runner.py digest monthly
```

脚本只输出对应 `wechat_text` 或 `NO_REPLY`。不要让模型重新排版。月报与日报同时触发时只保留月报任务负责合并输出，避免同一分钟重复消息。

## 修改和删除

- 查看：`openclaw cron get <job_id>` 或 `openclaw cron show <job_id>`。
- 暂停/恢复：`openclaw cron disable <job_id>` / `openclaw cron enable <job_id>`。
- 删除：`openclaw cron rm <job_id>`。
- 诊断：`openclaw cron runs --id <job_id>`；只在用户要求排错时展示必要摘要。

命令实际参数以当前服务器帮助为准。删除一个已成功执行并自动清理的一次性 job 时，如果结构化结果明确表示“不存在”，可按幂等成功处理，不要把已完成事项改成同步异常。

## 旧端数据迁移

如需导入旧 SQLite 业务数据，可以迁移项目、事项、草稿和行为记录，但不能沿用旧运行时的 `cron_job_id`。迁移前在线备份数据库；把未来未触发提醒重新创建为 OpenClaw Cron，并用新 job ID 绑定。云端“两分钟后提醒”真实验收成功前，不关闭仍承担提醒职责的旧端。

## 1.2.1 升级审计

运行：

```bash
python3 <skill_dir>/scripts/secretary.py cron-audit-plan
```

它列出所有未来、非批处理的 active 或 pending Cron 记录及预期契约。逐个用 `cron show` 核验；旧 job 不满足强制核验时，先创建并验证新 job，绑定成功后才删除旧 job。若服务器无法读取旧 job 的结构化 delivery，视为不可验证，不能把它当作有效提醒。
