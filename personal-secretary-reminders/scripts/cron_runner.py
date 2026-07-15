#!/usr/bin/env python3
"""Deterministic OpenClaw Cron entrypoint.

Prints only user-visible WeChat text or NO_REPLY. Errors go to stderr and return
non-zero so OpenClaw records a failed run instead of delivering invented text.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from secretary import SecretaryDB, SecretaryError, default_database_path, parse_dt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw Cron runner for personal-secretary-reminders")
    subparsers = parser.add_subparsers(dest="command", required=True)

    reminder = subparsers.add_parser("reminder")
    reminder.add_argument("--reminder-id", required=True)
    reminder.add_argument("--db", default=str(default_database_path()))
    reminder.add_argument("--now", help="deterministic ISO timestamp for testing")

    digest = subparsers.add_parser("digest")
    digest.add_argument("kind", choices=("daily", "weekly", "monthly"))
    digest.add_argument("--db", default=str(default_database_path()))
    digest.add_argument("--now", help="deterministic ISO timestamp for testing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = parse_dt(args.now, "Asia/Shanghai") if args.now else None
    db = None
    try:
        db = SecretaryDB(Path(args.db), now)
        db.initialize()
        if args.command == "reminder":
            result = db.fire_reminder({"reminder_id": args.reminder_id})
            print(result["wechat_text"] if result["deliver"] else "NO_REPLY")
        else:
            result = db.digest(args.kind, {})
            print(result.get("wechat_text") or "NO_REPLY")
        return 0
    except (SecretaryError, OSError, sqlite3.Error) as exc:
        print(f"personal-secretary-reminders cron error: {exc}", file=sys.stderr)
        return 2
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
