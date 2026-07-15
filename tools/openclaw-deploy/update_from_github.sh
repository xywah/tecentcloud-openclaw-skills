#!/usr/bin/env bash
set -Eeuo pipefail

REPO=""
REF=""
SKILL_PATH=""
DRY_RUN=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "Usage: $0 --repo <git-url> --ref <version-tag> --skill-path <folder> [--dry-run]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --ref) REF="${2:-}"; shift 2 ;;
    --skill-path) SKILL_PATH="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$REPO" && -n "$REF" && -n "$SKILL_PATH" ]] || { usage >&2; exit 2; }
[[ "$SKILL_PATH" != /* && "$SKILL_PATH" != *".."* ]] || {
  echo "--skill-path must be a repository-relative folder without '..'" >&2
  exit 2
}
command -v git >/dev/null 2>&1 || { echo "git command not found" >&2; exit 1; }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
CHECKOUT="$TMP_ROOT/repo"

git clone --quiet --depth 1 --branch "$REF" "$REPO" "$CHECKOUT"
SOURCE="$CHECKOUT/$SKILL_PATH"
[[ -f "$SOURCE/SKILL.md" ]] || {
  echo "Skill not found at repository path: $SKILL_PATH" >&2
  exit 2
}

args=(--source "$SOURCE")
if [[ "$DRY_RUN" == "1" ]]; then
  args+=(--dry-run)
fi
"$SCRIPT_DIR/deploy_openclaw.sh" "${args[@]}"
