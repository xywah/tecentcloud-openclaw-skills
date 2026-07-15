# Tencent Cloud OpenClaw Skills

Private monorepo for skills developed locally with Codex and deployed to the Tencent Cloud OpenClaw `main` agent.

## Layout

```text
tecentcloud-openclaw-skills/
├── personal-secretary-reminders/   # One independently installable Skill
├── tests/                           # Repository tests; never copied into a Skill
└── tools/openclaw-deploy/           # Shared deployment and rollback tools
```

Every Skill lives in its own top-level folder. The folder name must match the `name` in its `SKILL.md`. Future Skills should be added as peer folders, not nested inside an existing Skill.

## Deploy one Skill

```bash
tools/openclaw-deploy/update_from_private_github.sh \
  --repo git@github.com:xywah/tecentcloud-openclaw-skills.git \
  --ref personal-secretary-reminders-v1.2.0 \
  --skill-path personal-secretary-reminders
```

The deployment tool installs only the selected Skill folder. Tests, repository documentation, other Skills, and deployment tools are not copied into the OpenClaw workspace Skill directory.

## Data boundary

Never commit databases, exports, backups, credentials, chat identifiers, or server configuration. Stateful Skill data stays under `~/.openclaw/data/<skill-name>/` on the cloud server.
