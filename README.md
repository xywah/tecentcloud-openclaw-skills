# Tencent Cloud OpenClaw Skills

Public source monorepo for Skills developed locally with Codex, published through SkillHub/ClawHub, and run by the Tencent Cloud OpenClaw `main` agent.

## Layout

```text
tecentcloud-openclaw-skills/
├── personal-secretary-reminders/   # One independently installable Skill
├── tests/                           # Repository tests; never copied into a Skill
└── tools/openclaw-deploy/           # Shared deployment and rollback tools
```

Every Skill lives in its own top-level folder. The folder name must match the `name` in its `SKILL.md`. Future Skills should be added as peer folders, not nested inside an existing Skill.

## Recommended install and update

The preferred distribution path is the official OpenClaw SkillHub/ClawHub listing. On the Tencent Cloud server, use the exact owner/slug shown on that listing:

```bash
openclaw skills install <owner/slug> --agent main
openclaw skills update <owner/slug> --agent main
```

This repository remains the public source of truth. Codex changes and tests the source here; SkillHub/ClawHub publishes installable versions; the Tencent Cloud server keeps runtime data and WeChat configuration.

## Tagged GitHub fallback

For recovery, explicit tag deployment, or diagnostics, one Skill can still be installed directly from this public repository:

```bash
tools/openclaw-deploy/update_from_github.sh \
  --repo https://github.com/xywah/tecentcloud-openclaw-skills.git \
  --ref personal-secretary-reminders-v1.2.0 \
  --skill-path personal-secretary-reminders
```

The deployment tool installs only the selected Skill folder. Tests, repository documentation, other Skills, and deployment tools are not copied into the OpenClaw workspace Skill directory.

## Data boundary

This repository is public. Never commit databases, exports, backups, credentials, chat identifiers, personal reminder content, or server configuration. Stateful Skill data stays under `~/.openclaw/data/<skill-name>/` on the cloud server.
