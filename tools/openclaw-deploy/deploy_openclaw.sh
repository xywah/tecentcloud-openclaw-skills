#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE=""
AGENT_ID="${OPENCLAW_AGENT_ID:-main}"
WORKSPACE="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
DATA_DIR_OVERRIDE=""
DRY_RUN=0

usage() {
  echo "Usage: $0 --source <skill-folder> [--agent main] [--workspace <path>] [--data-dir <path>] [--dry-run]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="${2:-}"; shift 2 ;;
    --agent) AGENT_ID="${2:-}"; shift 2 ;;
    --workspace) WORKSPACE="${2:-}"; shift 2 ;;
    --data-dir) DATA_DIR_OVERRIDE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$SOURCE" ]] || { usage >&2; exit 2; }
SOURCE="$(cd "$SOURCE" && pwd)"
[[ -f "$SOURCE/SKILL.md" ]] || { echo "SKILL.md not found at source root" >&2; exit 2; }

SKILL_NAME="$(sed -n 's/^name:[[:space:]]*//p' "$SOURCE/SKILL.md" | head -n 1 | tr -d '\r')"
[[ "$SKILL_NAME" =~ ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$ ]] || {
  echo "Invalid or missing Skill name in SKILL.md" >&2
  exit 2
}
[[ "$(basename "$SOURCE")" == "$SKILL_NAME" ]] || {
  echo "Skill folder name must match SKILL.md name: $SKILL_NAME" >&2
  exit 2
}

TARGET="$WORKSPACE/skills/$SKILL_NAME"
BACKUP_ROOT="${OPENCLAW_SKILL_BACKUP_DIR:-$HOME/.openclaw/backups/$SKILL_NAME}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
DB_PATH=""

if [[ "$SKILL_NAME" == "personal-secretary-reminders" ]]; then
  [[ -f "$SOURCE/scripts/secretary.py" ]] || { echo "secretary.py not found" >&2; exit 2; }
  [[ -f "$SOURCE/scripts/cron_runner.py" ]] || { echo "cron_runner.py not found" >&2; exit 2; }
  python3 "$SOURCE/scripts/secretary.py" --help >/dev/null
  DATA_DIR="${DATA_DIR_OVERRIDE:-$HOME/.openclaw/data/$SKILL_NAME}"
  DB_PATH="$DATA_DIR/reminders.sqlite3"
elif [[ -n "$DATA_DIR_OVERRIDE" ]]; then
  echo "--data-dir is only supported for a Skill with an explicit deployment adapter" >&2
  exit 2
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN_OK"
  echo "skill=$SKILL_NAME"
  echo "source=$SOURCE"
  echo "target=$TARGET"
  echo "database=${DB_PATH:-not-managed}"
  echo "backup=$BACKUP_DIR"
  echo "agent=$AGENT_ID"
  exit 0
fi

command -v openclaw >/dev/null 2>&1 || { echo "openclaw command not found" >&2; exit 1; }
openclaw health >/dev/null
mkdir -p "$BACKUP_DIR" "$(dirname "$TARGET")"

HAD_PREVIOUS=0
if [[ -d "$TARGET" ]]; then
  HAD_PREVIOUS=1
  cp -a "$TARGET" "$BACKUP_DIR/skill"
fi

if [[ -n "$DB_PATH" ]]; then
  mkdir -p "$(dirname "$DB_PATH")"
  if [[ -f "$DB_PATH" ]]; then
    python3 "$SOURCE/scripts/secretary.py" backup \
      --db "$DB_PATH" \
      --payload "{\"output\":\"$BACKUP_DIR/reminders.sqlite3\"}" >/dev/null
  fi
fi

rollback_code() {
  status=$?
  trap - ERR
  echo "Deployment failed; restoring the previous Skill code." >&2
  rm -rf "$TARGET"
  if [[ "$HAD_PREVIOUS" == "1" ]]; then
    cp -a "$BACKUP_DIR/skill" "$TARGET"
  fi
  echo "Persistent data was not overwritten. Backup directory: $BACKUP_DIR" >&2
  exit "$status"
}
trap rollback_code ERR

openclaw skills install "$SOURCE" --as "$SKILL_NAME" --agent "$AGENT_ID" --force
[[ -f "$TARGET/SKILL.md" ]] || { echo "Installed Skill not found at $TARGET" >&2; false; }
openclaw skills check --agent "$AGENT_ID"
if [[ -n "$DB_PATH" ]]; then
  python3 "$TARGET/scripts/secretary.py" doctor --db "$DB_PATH" >/dev/null
fi
openclaw health >/dev/null

trap - ERR
echo "DEPLOY_OK"
echo "skill=$SKILL_NAME"
echo "target=$TARGET"
echo "database=${DB_PATH:-not-managed}"
echo "backup=$BACKUP_DIR"
echo "agent=$AGENT_ID"
