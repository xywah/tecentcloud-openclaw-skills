#!/usr/bin/env bash
set -Eeuo pipefail

AGENT_ID="${OPENCLAW_AGENT_ID:-main}"

fail() {
  echo "PRECHECK_FAILED: $*" >&2
  exit 1
}

command -v openclaw >/dev/null 2>&1 || fail "openclaw command not found"
command -v python3 >/dev/null 2>&1 || fail "python3 command not found"

echo "[1/5] OpenClaw version"
openclaw --version

echo "[2/5] Gateway health"
openclaw health

echo "[3/5] Cron scheduler"
openclaw cron status

echo "[4/5] Required Cron options"
cron_help="$(openclaw cron add --help)"
for option in --name --at --command-argv --command-cwd --announce --channel --to; do
  grep -q -- "$option" <<<"$cron_help" || fail "openclaw cron add is missing $option"
done

echo "[5/5] Skill installer"
skill_help="$(openclaw skills install --help)"
for option in --as --agent --force; do
  grep -q -- "$option" <<<"$skill_help" || fail "openclaw skills install is missing $option"
done
openclaw skills check --agent "$AGENT_ID"

if [[ "$(id -u)" == "0" ]]; then
  echo "WARNING: OpenClaw is running as root. Keep the Gateway port private and restrict Tencent Cloud security-group access."
fi

echo "PRECHECK_OK: runtime supports the deployment contract for agent $AGENT_ID"
