# Tencent Cloud OpenClaw Skills

Public source monorepo for Skills developed locally with Codex and run by the Tencent Cloud OpenClaw `main` agent.

## Layout

```text
tecentcloud-openclaw-skills/
├── personal-secretary-reminders/   # One independently installable Skill
├── tests/                           # Repository tests; never copied into a Skill
└── tools/openclaw-deploy/           # Shared deployment and rollback tools
```

Every Skill lives in its own top-level folder. The folder name must match the `name` in its `SKILL.md`. Future Skills should be added as peer folders, not nested inside an existing Skill.

## Release and deployment model

The workflow is deliberately split:

- Codex edits and tests the source in this repository.
- GitHub stores only public source, tests, tags, and release notes.
- A clean ZIP is generated for each release.
- The user manually uploads that ZIP to SkillHub.
- Tencent Cloud OpenClaw runs the installed Skill and keeps all runtime state.

SkillHub is not assumed to auto-sync from GitHub. A GitHub push or tag does not update the production SkillHub version by itself.

## Tagged GitHub fallback

For recovery, explicit tag deployment, or diagnostics, one Skill can still be installed directly from this public repository:

```bash
tools/openclaw-deploy/update_from_github.sh \
  --repo https://github.com/xywah/tecentcloud-openclaw-skills.git \
  --ref personal-secretary-reminders-v1.2.2 \
  --skill-path personal-secretary-reminders
```

The deployment tool installs only the selected Skill folder. Tests, repository documentation, other Skills, and deployment tools are not copied into the OpenClaw workspace Skill directory.

## Data boundary

This repository is public. Never commit databases, exports, backups, credentials, chat identifiers, personal reminder content, or server configuration.

For `personal-secretary-reminders`, production items stay only in:

```text
~/.openclaw/data/personal-secretary-reminders/reminders.sqlite3
```

The Skill code lives separately under `~/.openclaw/workspace/skills/`. OpenClaw Cron state and WeChat configuration also remain on the cloud server. Releasing or upgrading source code must not upload, overwrite, or delete those runtime assets.

## Version policy

- `1.2.x`: bug fixes, reliability/security hardening, compatibility fixes, and documentation corrections without new user-facing capability.
- `1.x.0`: backward-compatible feature releases; after 1.2.x, the next functional release is 1.3.0.
- `2.0.0`: breaking changes to data, architecture, installation, or interaction contracts.
