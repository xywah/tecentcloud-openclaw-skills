#!/usr/bin/env python3
"""Deterministic local storage and planning engine for personal-secretary-reminders.

The surrounding OpenClaw agent is responsible for natural-language understanding
and for calling OpenClaw Cron. This script owns validation,
SQLite transactions, time calculations, transparent priority scoring, and
structured digest data. It uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import statistics
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


APP_NAME = "personal-secretary-reminders"
SCHEMA_VERSION = 1
ACTIVE_STATES = ("active", "scheduled", "pending_schedule", "sync_error")
VALID_STATES = {
    "draft", "pending_schedule", "active", "scheduled", "waiting",
    "delegated", "someday", "completed", "cancelled", "archived", "sync_error",
}
VALID_TYPES = {"event", "task", "someday", "idea", "waiting", "reference"}
ENERGY_RANK = {"low": 1, "medium": 2, "high": 3}


def default_database_path(
    *,
    home: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    """Keep persistent data outside the OpenClaw workspace Skill directory."""
    env = os.environ if environ is None else environ
    explicit = env.get("PERSONAL_SECRETARY_DB")
    if explicit:
        return Path(explicit).expanduser()
    base = (home or Path.home()).expanduser()
    return base / ".openclaw" / "data" / APP_NAME / "reminders.sqlite3"


class SecretaryError(Exception):
    """A user-correctable validation or state error."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: Any, tz_name: str = "Asia/Shanghai", *, end_of_day: bool = False) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time(23, 59) if end_of_day else time.min)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            if len(text) == 10:
                parsed = datetime.combine(date.fromisoformat(text), time(23, 59) if end_of_day else time.min)
            else:
                parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SecretaryError(f"Invalid ISO date/time: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class SecretaryDB:
    def __init__(self, db_path: Path, now: datetime | None = None):
        self.path = db_path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.now = (now or utc_now()).astimezone(timezone.utc).replace(microsecond=0)

    def close(self) -> None:
        self.conn.close()

    def initialize(self) -> dict[str, Any]:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                desired_outcome TEXT NOT NULL,
                status TEXT NOT NULL,
                deadline_at TEXT,
                current_next_action_item_id TEXT,
                category TEXT NOT NULL,
                importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 5),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                details TEXT,
                status TEXT NOT NULL,
                desired_outcome TEXT,
                next_action TEXT,
                category TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 5),
                urgency_override INTEGER,
                deadline_at TEXT,
                start_at TEXT,
                end_at TEXT,
                review_at TEXT,
                start_by_at TEXT,
                estimate_minutes INTEGER,
                actual_minutes INTEGER,
                context TEXT,
                energy TEXT,
                waiting_for TEXT,
                delegated_to TEXT,
                unlocks_count INTEGER NOT NULL DEFAULT 0,
                dependency_count INTEGER NOT NULL DEFAULT 0,
                quiet_bypass INTEGER NOT NULL DEFAULT 0,
                priority_label TEXT,
                priority_score REAL,
                score_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                archived_at TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                trigger_at TEXT NOT NULL,
                status TEXT NOT NULL,
                cron_job_id TEXT,
                follow_up INTEGER NOT NULL DEFAULT 0,
                batched INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                raw_text TEXT NOT NULL,
                inferred_type TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS behavior_events (
                id TEXT PRIMARY KEY,
                item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
                event_type TEXT NOT NULL,
                data_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recommendations (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                kind TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                suggested_value REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_items_status_deadline ON items(status, deadline_at);
            CREATE INDEX IF NOT EXISTS idx_items_project ON items(project_id);
            CREATE INDEX IF NOT EXISTS idx_reminders_status_trigger ON reminders(status, trigger_at);
            CREATE INDEX IF NOT EXISTS idx_behavior_item_type ON behavior_events(item_id, event_type);
            CREATE INDEX IF NOT EXISTS idx_drafts_status_expiry ON drafts(status, expires_at);
            """
        )
        now = iso_utc(self.now)
        defaults = {
            "timezone": "Asia/Shanghai",
            "quiet_start": "22:00",
            "quiet_end": "07:30",
            "daily_digest": "09:00",
            "weekly_digest": "monday 08:00",
            "monthly_digest": "day 1 09:00",
            "buffer_multiplier": 1.25,
            "learning_min_samples": 8,
            "workday_start": "09:00",
            "workday_end": "18:00",
            "workdays": [0, 1, 2, 3, 4],
            "daily_focus_limit": 3,
            "wechat_section_limit": 5,
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            for key, value in defaults.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                    (key, json_dumps(value), now),
                )
        return {"database": str(self.path), "schema_version": SCHEMA_VERSION, "journal_mode": "wal"}

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json_loads(row[0], default) if row else default

    @property
    def tz_name(self) -> str:
        return self.setting("timezone", "Asia/Shanghai")

    def audit(self, entity_type: str, entity_id: str, action: str,
              before: Any = None, after: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log VALUES(?,?,?,?,?,?,?)",
            (new_id("aud"), entity_type, entity_id, action,
             json_dumps(before) if before is not None else None,
             json_dumps(after) if after is not None else None, iso_utc(self.now)),
        )

    def record_behavior(self, item_id: str | None, event_type: str, data: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO behavior_events VALUES(?,?,?,?,?)",
            (new_id("beh"), item_id, event_type, json_dumps(data or {}), iso_utc(self.now)),
        )

    def expire_drafts(self) -> int:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE drafts SET status='expired',updated_at=? "
                "WHERE status IN ('draft','clarifying') AND expires_at<=?",
                (iso_utc(self.now), iso_utc(self.now)),
            )
        return cur.rowcount

    def doctor(self) -> dict[str, Any]:
        self.initialize()
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = {}
        for table in ("projects", "items", "reminders", "drafts", "behavior_events", "recommendations"):
            counts[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {
            "ok": integrity == "ok" and not foreign_keys,
            "database": str(self.path),
            "integrity": integrity,
            "foreign_key_errors": len(foreign_keys),
            "schema_version": int(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]),
            "counts": counts,
            "timezone": self.tz_name,
        }

    def create_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_text = str(payload.get("raw_text", "")).strip()
        if not raw_text:
            raise SecretaryError("draft requires raw_text")
        inferred = payload.get("inferred_type")
        if inferred and inferred not in VALID_TYPES | {"project"}:
            raise SecretaryError(f"unsupported inferred_type: {inferred}")
        draft_id = new_id("drf")
        expires = self.now + timedelta(hours=24)
        body = dict(payload.get("fields") or {})
        with self.conn:
            self.conn.execute(
                "INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?)",
                (draft_id, raw_text, inferred, json_dumps(body), "draft", iso_utc(expires),
                 iso_utc(self.now), iso_utc(self.now)),
            )
            self.audit("draft", draft_id, "create", after=payload)
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        self.expire_drafts()
        row = self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise SecretaryError(f"draft not found: {draft_id}")
        result = dict(row)
        result["payload"] = json_loads(result.pop("payload_json"), {})
        result["missing_fields"] = self.missing_fields(result.get("inferred_type"), result["payload"])
        return result

    def missing_fields(self, item_type: str | None, fields: dict[str, Any]) -> list[str]:
        if not item_type:
            return ["type"]
        required: dict[str, list[str]] = {
            "event": ["title", "start_at", "importance", "category"],
            "task": ["title", "desired_outcome", "next_action", "deadline_at", "importance", "category"],
            "project": ["title", "desired_outcome", "next_action", "deadline_at", "importance", "category"],
            "someday": ["title", "review_at", "importance", "category"],
            "idea": ["title", "category"],
            "waiting": ["title", "review_at", "importance", "category"],
            "reference": ["title", "category"],
        }
        missing = [key for key in required.get(item_type, []) if fields.get(key) in (None, "", [])]
        if item_type == "event" and not fields.get("end_at") and not fields.get("duration_minutes"):
            missing.append("end_at_or_duration_minutes")
        if item_type == "waiting" and not (fields.get("waiting_for") or fields.get("delegated_to")):
            missing.append("waiting_for_or_delegated_to")
        return missing

    def clarify(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft_id = payload.get("draft_id")
        if not draft_id:
            raise SecretaryError("clarify requires draft_id")
        current = self.get_draft(draft_id)
        if current["status"] == "expired":
            raise SecretaryError("draft expired; capture it again")
        inferred = payload.get("inferred_type", current.get("inferred_type"))
        if inferred and inferred not in VALID_TYPES | {"project"}:
            raise SecretaryError(f"unsupported inferred_type: {inferred}")
        fields = current["payload"]
        fields.update(payload.get("fields") or {})
        with self.conn:
            self.conn.execute(
                "UPDATE drafts SET inferred_type=?,payload_json=?,status='clarifying',updated_at=? WHERE id=?",
                (inferred, json_dumps(fields), iso_utc(self.now), draft_id),
            )
            self.audit("draft", draft_id, "clarify", current["payload"], fields)
        return self.get_draft(draft_id)

    def _work_window(self) -> tuple[time, time, set[int]]:
        start = time.fromisoformat(self.setting("workday_start", "09:00"))
        end = time.fromisoformat(self.setting("workday_end", "18:00"))
        days = set(self.setting("workdays", [0, 1, 2, 3, 4]))
        return start, end, days

    def subtract_work_minutes(self, deadline: datetime, minutes: int) -> datetime:
        """Subtract minutes through configured work windows, skipping non-workdays."""
        tz = ZoneInfo(self.tz_name)
        local = deadline.astimezone(tz)
        work_start, work_end, workdays = self._work_window()
        remaining = max(0, int(minutes))
        cursor = local
        while remaining > 0:
            if cursor.weekday() not in workdays:
                cursor = datetime.combine(cursor.date() - timedelta(days=1), work_end, tzinfo=tz)
                continue
            day_start = datetime.combine(cursor.date(), work_start, tzinfo=tz)
            day_end = datetime.combine(cursor.date(), work_end, tzinfo=tz)
            if cursor > day_end:
                cursor = day_end
            if cursor <= day_start:
                cursor = datetime.combine(cursor.date() - timedelta(days=1), work_end, tzinfo=tz)
                continue
            available = int((cursor - day_start).total_seconds() // 60)
            used = min(remaining, available)
            cursor -= timedelta(minutes=used)
            remaining -= used
            if remaining > 0:
                cursor = datetime.combine(cursor.date() - timedelta(days=1), work_end, tzinfo=tz)
        return cursor.astimezone(timezone.utc)

    def calibrated_minutes(self, category: str, estimate: int | None) -> int | None:
        if not estimate:
            return None
        if estimate <= 15:
            return estimate
        multiplier = self.setting(f"buffer_multiplier:{category}", self.setting("buffer_multiplier", 1.25))
        return max(estimate, round(estimate * float(multiplier)))

    def compute_start_by(self, fields: dict[str, Any]) -> datetime | None:
        deadline = parse_dt(fields.get("deadline_at"), self.tz_name, end_of_day=True)
        estimate = fields.get("estimate_minutes")
        # start-by is deliberately reserved for non-trivial work. Short tasks
        # use the date/time reminder matrix without creating another planning
        # timestamp for the user to manage.
        if not deadline or not estimate or int(estimate) <= 15:
            return None
        return self.subtract_work_minutes(deadline, self.calibrated_minutes(fields.get("category", "general"), int(estimate)) or 0)

    def priority(self, item: dict[str, Any], *, context: str | None = None,
                 energy: str | None = None, available_minutes: int | None = None) -> dict[str, Any]:
        now = self.now
        importance = int(item.get("importance") or 1)
        deadline = parse_dt(item.get("deadline_at"), self.tz_name)
        start_by = parse_dt(item.get("start_by_at"), self.tz_name)
        risk_point = start_by or deadline
        hours = (risk_point - now).total_seconds() / 3600 if risk_point else None
        if hours is None:
            deadline_score = 0
            urgent = False
        elif hours <= 0:
            deadline_score, urgent = 30, True
        elif hours <= 24:
            deadline_score, urgent = 27, True
        elif hours <= 72:
            deadline_score, urgent = 22, False
        elif hours <= 168:
            deadline_score, urgent = 15, False
        elif hours <= 720:
            deadline_score, urgent = 8, False
        else:
            deadline_score, urgent = 3, False
        override = item.get("urgency_override")
        if override is not None:
            urgent = int(override) >= 4
        important = importance >= 4
        label = "P1" if important and urgent else "P2" if important else "P3" if urgent else "P4"
        impact_score = min(25, importance * 5)
        unlock_score = min(15, max(0, int(item.get("unlocks_count") or 0)) * 5)
        blocked = item.get("status") in ("waiting", "delegated")
        dependency_score = 0 if blocked else min(10, (4 if item.get("project_id") else 0) + int(item.get("dependency_count") or 0) * 2)
        created = parse_dt(item.get("created_at"), self.tz_name) or now
        age_score = min(10, max(0, int((now - created).total_seconds() // 86400)) / 3)
        fit_score = 5.0
        if context is not None:
            fit_score += 3 if not item.get("context") or item.get("context") == context else -3
        if energy is not None:
            needed = ENERGY_RANK.get(str(item.get("energy") or "medium"), 2)
            current = ENERGY_RANK.get(energy, 2)
            fit_score += 2 if current >= needed else -3
        estimate = item.get("estimate_minutes")
        if available_minutes is not None and estimate:
            fit_score += 2 if int(estimate) <= available_minutes else -5
        fit_score = max(0, min(10, fit_score))
        components = {
            "deadline_start_by_risk": round(deadline_score, 1),
            "impact_consequence": round(impact_score, 1),
            "unlock_effect": round(unlock_score, 1),
            "project_dependencies": round(dependency_score, 1),
            "age_waiting": round(age_score, 1),
            "context_fit": round(fit_score, 1),
        }
        score = round(sum(components.values()), 1)
        reasons = []
        if deadline and deadline <= now:
            reasons.append("已逾期")
        elif start_by and start_by <= now:
            reasons.append("已到最迟开始时间")
        elif hours is not None and hours <= 24:
            reasons.append("24小时内进入时间风险")
        if importance >= 4:
            reasons.append("重要性高")
        if unlock_score:
            reasons.append("可解锁后续事项")
        if item.get("project_id"):
            reasons.append("属于活跃项目")
        if not reasons:
            reasons.append("当前风险与影响综合排序")
        return {"label": label, "score": score, "components": components, "reasons": reasons[:3]}

    def _is_quiet(self, when: datetime) -> bool:
        tz = ZoneInfo(self.tz_name)
        local_time = when.astimezone(tz).time()
        start = time.fromisoformat(self.setting("quiet_start", "22:00"))
        end = time.fromisoformat(self.setting("quiet_end", "07:30"))
        return local_time >= start or local_time < end if start > end else start <= local_time < end

    def _nine_am(self, target: date) -> datetime:
        return datetime.combine(target, time(9, 0), tzinfo=ZoneInfo(self.tz_name)).astimezone(timezone.utc)

    def reminder_specs(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        item_type = item["type"]
        if item_type in ("idea", "reference"):
            return []
        now = self.now
        specs: list[tuple[str, datetime, bool]] = []
        start = parse_dt(item.get("start_at"), self.tz_name)
        deadline = parse_dt(item.get("deadline_at"), self.tz_name)
        start_by = parse_dt(item.get("start_by_at"), self.tz_name)
        review = parse_dt(item.get("review_at"), self.tz_name)
        if item_type == "event" and start:
            specs.extend([
                ("event_day_before", start - timedelta(days=1), False),
                ("event_hour_before", start - timedelta(hours=1), False),
                ("event_ten_minutes", start - timedelta(minutes=10), False),
            ])
        elif item_type == "task" and deadline:
            estimate = int(item.get("estimate_minutes") or 0)
            exact_time = deadline.astimezone(ZoneInfo(self.tz_name)).time() not in (time(23, 59), time.min)
            if estimate > 15:
                if start_by:
                    specs.append(("start_by", start_by, False))
                specs.append(("progress_day_before", deadline - timedelta(days=1), False))
                specs.append(("deadline", deadline, False))
            else:
                local_date = deadline.astimezone(ZoneInfo(self.tz_name)).date()
                specs.extend([
                    ("task_day_before", self._nine_am(local_date - timedelta(days=1)), False),
                    ("task_due_day", self._nine_am(local_date), False),
                ])
            if exact_time:
                specs.append(("task_ten_minutes", deadline - timedelta(minutes=10), False))
        elif item_type in ("someday", "waiting") and review:
            specs.append(("review", review, False))
        specs = [(kind, when, follow) for kind, when, follow in specs if when > now]
        if item.get("priority_label") == "P1" and specs:
            last = max(when for _, when, _ in specs)
            specs.append(("p1_follow_up", last + timedelta(minutes=15), True))
        output = []
        for kind, when, follow_up in sorted(specs, key=lambda x: x[1]):
            user_exact = kind in {"event_day_before", "event_hour_before", "event_ten_minutes", "task_ten_minutes"}
            hard_event = item_type == "event"
            bypass = bool(item.get("quiet_bypass")) or hard_event or user_exact
            system_generated = kind in {"progress_day_before", "deadline", "task_day_before", "task_due_day", "review"}
            batched = bool(system_generated and item.get("priority_label") != "P1" and (self._is_quiet(when) or item.get("priority_label") in {"P2", "P3", "P4"}))
            if bypass:
                batched = False
            output.append({
                "kind": kind,
                "trigger_at": iso_utc(when),
                "follow_up": follow_up,
                "batched": batched,
                "needs_cron": not batched,
                "message": self._reminder_message(item, kind),
            })
        return output

    @staticmethod
    def _reminder_message(item: dict[str, Any], kind: str) -> str:
        labels = {
            "start_by": "该开始了", "progress_day_before": "请检查进展", "deadline": "今天到期",
            "event_day_before": "明天有安排", "event_hour_before": "1小时后开始", "event_ten_minutes": "10分钟后开始",
            "task_day_before": "明天到期", "task_due_day": "今天待办", "task_ten_minutes": "还有10分钟到期",
            "review": "请重新评估", "p1_follow_up": "这是唯一一次跟进：是否已收到、完成或需要延期？",
        }
        return f"{labels.get(kind, '提醒')}：{item['title']}"

    def _insert_item(self, fields: dict[str, Any], project_id: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        item_type = fields["type"]
        if item_type not in VALID_TYPES:
            raise SecretaryError(f"unsupported type: {item_type}")
        importance = int(fields.get("importance", 1))
        if not 1 <= importance <= 5:
            raise SecretaryError("importance must be 1-5")
        item_id = new_id("itm")
        start_at = parse_dt(fields.get("start_at"), self.tz_name)
        end_at = parse_dt(fields.get("end_at"), self.tz_name)
        if item_type == "event" and start_at and not end_at and fields.get("duration_minutes"):
            end_at = start_at + timedelta(minutes=int(fields["duration_minutes"]))
        deadline = parse_dt(fields.get("deadline_at"), self.tz_name, end_of_day=True)
        review_at = parse_dt(fields.get("review_at"), self.tz_name)
        start_by = self.compute_start_by(fields)
        status = fields.get("status") or ("waiting" if item_type == "waiting" else "someday" if item_type == "someday" else "active")
        if status not in VALID_STATES:
            raise SecretaryError(f"invalid status: {status}")
        base = {
            "id": item_id, "project_id": project_id or fields.get("project_id"), "type": item_type,
            "title": str(fields["title"]).strip(), "details": fields.get("details"), "status": status,
            "desired_outcome": fields.get("desired_outcome"), "next_action": fields.get("next_action"),
            "category": fields.get("category", "general"), "tags_json": json_dumps(fields.get("tags", [])),
            "importance": importance, "urgency_override": fields.get("urgency_override"),
            "deadline_at": iso_utc(deadline), "start_at": iso_utc(start_at), "end_at": iso_utc(end_at),
            "review_at": iso_utc(review_at), "start_by_at": iso_utc(start_by),
            "estimate_minutes": fields.get("estimate_minutes"), "actual_minutes": None,
            "context": fields.get("context"), "energy": fields.get("energy"),
            "waiting_for": fields.get("waiting_for"), "delegated_to": fields.get("delegated_to"),
            "unlocks_count": int(fields.get("unlocks_count", 0)), "dependency_count": int(fields.get("dependency_count", 0)),
            "quiet_bypass": int(bool(fields.get("quiet_bypass", False))),
            "created_at": iso_utc(self.now), "updated_at": iso_utc(self.now),
        }
        ranking = self.priority(base)
        base.update({"priority_label": ranking["label"], "priority_score": ranking["score"], "score_json": json_dumps(ranking)})
        specs = self.reminder_specs(base)
        if any(spec["needs_cron"] for spec in specs):
            base["status"] = "pending_schedule" if status not in ("waiting", "someday") else status
        columns = list(base)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO items({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(base[col] for col in columns),
            )
            saved_specs = []
            for spec in specs:
                reminder_id = new_id("rem")
                reminder_status = "batched" if spec["batched"] else "pending_cron"
                self.conn.execute(
                    "INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (reminder_id, item_id, spec["kind"], spec["trigger_at"], reminder_status, None,
                     int(spec["follow_up"]), int(spec["batched"]), spec["message"], iso_utc(self.now), iso_utc(self.now)),
                )
                saved_specs.append({"reminder_id": reminder_id, **spec})
            self.audit("item", item_id, "create", after=base)
        return self.get_item(item_id), saved_specs

    def finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise SecretaryError("finalize requires confirmed=true after showing a confirmation card")
        draft_id = payload.get("draft_id")
        fields = dict(payload.get("fields") or {})
        inferred = fields.get("type")
        if draft_id:
            draft = self.get_draft(draft_id)
            if draft["status"] == "expired":
                raise SecretaryError("draft expired; capture it again")
            merged = dict(draft["payload"])
            merged.update(fields)
            fields = merged
            inferred = fields.get("type") or draft.get("inferred_type")
        fields["type"] = inferred
        missing = self.missing_fields(inferred, fields)
        if missing:
            raise SecretaryError("missing required fields: " + ", ".join(missing))
        if inferred == "project":
            result = self.create_project(fields)
        else:
            item, reminders = self._insert_item(fields)
            result = {"item": item, "cron_plan": [r for r in reminders if r["needs_cron"]],
                      "batched_reminders": [r for r in reminders if r["batched"]]}
        if draft_id:
            with self.conn:
                self.conn.execute("UPDATE drafts SET status='finalized',updated_at=? WHERE id=?", (iso_utc(self.now), draft_id))
                self.audit("draft", draft_id, "finalize", after={"result": result})
        return result

    def create_project(self, fields: dict[str, Any]) -> dict[str, Any]:
        for required in ("title", "desired_outcome", "next_action", "category", "importance"):
            if fields.get(required) in (None, ""):
                raise SecretaryError(f"create-project requires {required}")
        project_id = new_id("prj")
        deadline = parse_dt(fields.get("deadline_at"), self.tz_name, end_of_day=True)
        now = iso_utc(self.now)
        with self.conn:
            self.conn.execute(
                "INSERT INTO projects(id,title,desired_outcome,status,deadline_at,current_next_action_item_id,category,importance,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project_id, fields["title"], fields["desired_outcome"], "active", iso_utc(deadline), None,
                 fields["category"], int(fields["importance"]), now, now),
            )
            self.audit("project", project_id, "create", after=fields)
        action_fields = {
            "type": "task", "title": fields["next_action"], "desired_outcome": fields["desired_outcome"],
            "next_action": fields["next_action"], "category": fields["category"], "importance": fields["importance"],
            "deadline_at": fields.get("next_action_deadline_at") or fields.get("deadline_at"),
            "estimate_minutes": fields.get("estimate_minutes"), "context": fields.get("context"),
            "energy": fields.get("energy"), "project_id": project_id,
        }
        try:
            item, reminders = self._insert_item(action_fields, project_id)
        except Exception:
            # Never leave an active project without its mandatory first action.
            with self.conn:
                self.conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            raise
        with self.conn:
            self.conn.execute("UPDATE projects SET current_next_action_item_id=?,updated_at=? WHERE id=?",
                              (item["id"], now, project_id))
        return {"project": self.get_project(project_id), "next_action": item,
                "cron_plan": [r for r in reminders if r["needs_cron"]],
                "batched_reminders": [r for r in reminders if r["batched"]]}

    def set_next_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = payload.get("project_id")
        project = self.get_project(project_id)
        if project["status"] != "active":
            raise SecretaryError("next action can only be set for an active project")
        fields = dict(payload.get("fields") or {})
        fields.setdefault("type", "task")
        fields.setdefault("desired_outcome", project["desired_outcome"])
        fields.setdefault("category", project["category"])
        fields.setdefault("importance", project["importance"])
        fields.setdefault("deadline_at", project["deadline_at"])
        fields.setdefault("next_action", fields.get("title"))
        for key in ("title", "next_action", "deadline_at"):
            if fields.get(key) in (None, ""):
                raise SecretaryError(f"set-next-action requires {key}")
        item, reminders = self._insert_item(fields, project_id)
        with self.conn:
            self.conn.execute("UPDATE projects SET current_next_action_item_id=?,updated_at=? WHERE id=?",
                              (item["id"], iso_utc(self.now), project_id))
            self.audit("project", project_id, "set_next_action", after={"item_id": item["id"]})
        return {"project": self.get_project(project_id), "next_action": item,
                "cron_plan": [r for r in reminders if r["needs_cron"]]}

    def get_project(self, project_id: str | None) -> dict[str, Any]:
        if not project_id:
            raise SecretaryError("project_id is required")
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise SecretaryError(f"project not found: {project_id}")
        result = dict(row)
        result["zombie"] = result["status"] == "active" and not result["current_next_action_item_id"]
        return result

    def get_item(self, item_id: str | None) -> dict[str, Any]:
        if not item_id:
            raise SecretaryError("item_id is required")
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise SecretaryError(f"item not found: {item_id}")
        result = dict(row)
        result["tags"] = json_loads(result.pop("tags_json"), [])
        result["score"] = json_loads(result.pop("score_json"), {})
        return result

    def get(self, payload: dict[str, Any]) -> dict[str, Any]:
        entity = payload.get("entity", "item")
        entity_id = payload.get("id") or payload.get(f"{entity}_id")
        if entity == "item":
            item = self.get_item(entity_id)
            reminders = [dict(r) for r in self.conn.execute("SELECT * FROM reminders WHERE item_id=? ORDER BY trigger_at", (entity_id,))]
            return {"item": item, "reminders": reminders}
        if entity == "project":
            project = self.get_project(entity_id)
            items = [self.get_item(r[0]) for r in self.conn.execute("SELECT id FROM items WHERE project_id=? ORDER BY created_at", (entity_id,))]
            return {"project": project, "items": items}
        if entity == "draft":
            return {"draft": self.get_draft(entity_id)}
        raise SecretaryError(f"unsupported entity: {entity}")

    def bind_cron(self, payload: dict[str, Any]) -> dict[str, Any]:
        bindings = payload.get("reminder_bindings") or []
        if not bindings:
            raise SecretaryError("update with action=bind-cron requires reminder_bindings")
        item_ids = set()
        retired_ids = payload.get("retire_reminder_ids") or []
        old_jobs_to_remove: list[str] = []
        with self.conn:
            for binding in bindings:
                row = self.conn.execute("SELECT item_id,status FROM reminders WHERE id=?", (binding.get("reminder_id"),)).fetchone()
                if not row:
                    raise SecretaryError(f"reminder not found: {binding.get('reminder_id')}")
                self.conn.execute(
                    "UPDATE reminders SET cron_job_id=?,status='active',updated_at=? WHERE id=?",
                    (binding.get("cron_job_id"), iso_utc(self.now), binding["reminder_id"]),
                )
                item_ids.add(row["item_id"])
            if retired_ids:
                placeholders = ",".join("?" for _ in retired_ids)
                retired = self.conn.execute(
                    f"SELECT item_id,cron_job_id FROM reminders WHERE id IN ({placeholders})", retired_ids
                ).fetchall()
                old_jobs_to_remove = [r["cron_job_id"] for r in retired if r["cron_job_id"]]
                item_ids.update(r["item_id"] for r in retired)
                self.conn.execute(
                    f"UPDATE reminders SET status='replaced',updated_at=? WHERE id IN ({placeholders})",
                    (iso_utc(self.now), *retired_ids),
                )
            for item_id in item_ids:
                pending = self.conn.execute(
                    "SELECT COUNT(*) FROM reminders WHERE item_id=? AND status='pending_cron'", (item_id,)
                ).fetchone()[0]
                if pending == 0:
                    self.conn.execute("UPDATE items SET status='active',updated_at=? WHERE id=? AND status='pending_schedule'",
                                      (iso_utc(self.now), item_id))
                self.audit("item", item_id, "bind_cron", after=bindings)
        return {"bound": len(bindings), "retired": len(retired_ids),
                "cron_job_ids_to_remove": old_jobs_to_remove,
                "items": [self.get_item(i) for i in sorted(item_ids)]}

    def update_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "bind-cron":
            return self.bind_cron(payload)
        if payload.get("action") == "set-reminder-state":
            return self.set_reminder_state(payload)
        item_id = payload.get("item_id") or payload.get("id")
        before = self.get_item(item_id)
        changes = dict(payload.get("fields") or {})
        allowed = {"title", "details", "desired_outcome", "next_action", "category", "importance",
                   "urgency_override", "deadline_at", "start_at", "end_at", "review_at", "estimate_minutes",
                   "context", "energy", "waiting_for", "delegated_to", "unlocks_count", "dependency_count", "quiet_bypass"}
        unknown = set(changes) - allowed
        if unknown:
            raise SecretaryError("unsupported update fields: " + ", ".join(sorted(unknown)))
        for key in ("deadline_at", "start_at", "end_at", "review_at"):
            if key in changes:
                changes[key] = iso_utc(parse_dt(changes[key], self.tz_name, end_of_day=(key == "deadline_at")))
        merged = dict(before)
        merged.update(changes)
        if "deadline_at" in changes or "estimate_minutes" in changes or "category" in changes:
            merged["start_by_at"] = iso_utc(self.compute_start_by(merged))
            changes["start_by_at"] = merged["start_by_at"]
        ranking = self.priority(merged)
        changes.update({"priority_label": ranking["label"], "priority_score": ranking["score"], "score_json": json_dumps(ranking),
                        "updated_at": iso_utc(self.now)})
        assignments = ",".join(f"{key}=?" for key in changes)
        with self.conn:
            self.conn.execute(f"UPDATE items SET {assignments} WHERE id=?", (*changes.values(), item_id))
            self.audit("item", item_id, "update", before, changes)
        return {"item": self.get_item(item_id), "requires_reschedule": bool(set(changes) & {"deadline_at", "start_at", "end_at", "review_at", "start_by_at"})}

    def set_reminder_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = payload.get("state")
        if state not in {"active", "paused", "cancelled"}:
            raise SecretaryError("reminder state must be active, paused, or cancelled")
        reminder_ids = list(payload.get("reminder_ids") or [])
        item_id = payload.get("item_id") or payload.get("id")
        if not reminder_ids and item_id:
            reminder_ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM reminders WHERE item_id=? AND status NOT IN ('sent','cancelled','replaced')", (item_id,)
            )]
        if not reminder_ids:
            raise SecretaryError("set-reminder-state requires reminder_ids or an item_id with live reminders")
        placeholders = ",".join("?" for _ in reminder_ids)
        rows = self.conn.execute(
            f"SELECT id,item_id,cron_job_id,status FROM reminders WHERE id IN ({placeholders})", reminder_ids
        ).fetchall()
        if len(rows) != len(set(reminder_ids)):
            raise SecretaryError("one or more reminders were not found")
        with self.conn:
            self.conn.execute(
                f"UPDATE reminders SET status=?,updated_at=? WHERE id IN ({placeholders})",
                (state, iso_utc(self.now), *reminder_ids),
            )
            for affected_item in {r["item_id"] for r in rows}:
                self.audit("item", affected_item, f"reminders_{state}", after={"reminder_ids": reminder_ids})
        return {"state": state, "reminder_ids": reminder_ids,
                "cron_jobs": [{"reminder_id": r["id"], "cron_job_id": r["cron_job_id"]} for r in rows]}

    def fire_reminder(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Atomically claim one scheduled reminder and return its exact text."""
        reminder_id = payload.get("reminder_id") or payload.get("id")
        if not reminder_id:
            raise SecretaryError("fire-reminder requires reminder_id")
        row = self.conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        if not row:
            raise SecretaryError(f"reminder not found: {reminder_id}")
        reminder = dict(row)
        if reminder["status"] in {"sent", "cancelled", "replaced", "paused"}:
            return {
                "deliver": False,
                "reason": f"reminder is {reminder['status']}",
                "reminder_id": reminder_id,
                "item_id": reminder["item_id"],
            }
        item = self.get_item(reminder["item_id"])
        if item["status"] in {"completed", "cancelled", "archived"}:
            with self.conn:
                self.conn.execute(
                    "UPDATE reminders SET status='cancelled',updated_at=? WHERE id=?",
                    (iso_utc(self.now), reminder_id),
                )
            return {
                "deliver": False,
                "reason": f"item is {item['status']}",
                "reminder_id": reminder_id,
                "item_id": item["id"],
            }
        with self.conn:
            self.conn.execute(
                "UPDATE reminders SET status='sent',updated_at=? WHERE id=?",
                (iso_utc(self.now), reminder_id),
            )
            self.record_behavior(item["id"], "reminder_fired", {
                "reminder_id": reminder_id,
                "kind": reminder["kind"],
                "cron_job_id": reminder["cron_job_id"],
            })
        return {
            "deliver": True,
            "reminder_id": reminder_id,
            "item_id": item["id"],
            "kind": reminder["kind"],
            "message": reminder["message"],
            "wechat_text": reminder["message"],
        }

    def _terminal(self, item_id: str, status: str, event: str) -> dict[str, Any]:
        before = self.get_item(item_id)
        if before["status"] in ("completed", "cancelled", "archived"):
            return {"item": before, "cron_job_ids_to_remove": []}
        jobs = [r[0] for r in self.conn.execute(
            "SELECT cron_job_id FROM reminders WHERE item_id=? AND cron_job_id IS NOT NULL AND status='active'", (item_id,)
        )]
        with self.conn:
            self.conn.execute(
                "UPDATE items SET status=?,completed_at=?,updated_at=? WHERE id=?",
                (status, iso_utc(self.now) if status == "completed" else None, iso_utc(self.now), item_id),
            )
            self.conn.execute("UPDATE reminders SET status='cancelled',updated_at=? WHERE item_id=? AND status NOT IN ('sent','cancelled')",
                              (iso_utc(self.now), item_id))
            self.record_behavior(item_id, event)
            self.audit("item", item_id, event, before, {"status": status})
            if before.get("project_id"):
                project = self.get_project(before["project_id"])
                if project.get("current_next_action_item_id") == item_id:
                    self.conn.execute("UPDATE projects SET current_next_action_item_id=NULL,updated_at=? WHERE id=?",
                                      (iso_utc(self.now), before["project_id"]))
        result = {"item": self.get_item(item_id), "cron_job_ids_to_remove": jobs}
        if before.get("project_id"):
            result["project_needs_next_action"] = True
            result["project_id"] = before["project_id"]
        return result

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._terminal(payload.get("item_id") or payload.get("id"), "completed", "complete")
        if payload.get("actual_minutes") is not None:
            self.record_actual({"item_id": result["item"]["id"], "actual_minutes": payload["actual_minutes"]})
            result["item"] = self.get_item(result["item"]["id"])
        return result

    def cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._terminal(payload.get("item_id") or payload.get("id"), "cancelled", "cancel")

    def ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("item_id") or payload.get("id")
        self.get_item(item_id)
        rows = self.conn.execute(
            "SELECT id,cron_job_id FROM reminders WHERE item_id=? AND follow_up=1 AND status IN ('active','pending_cron')", (item_id,)
        ).fetchall()
        with self.conn:
            self.conn.execute(
                "UPDATE reminders SET status='cancelled',updated_at=? WHERE item_id=? AND follow_up=1 AND status IN ('active','pending_cron')",
                (iso_utc(self.now), item_id),
            )
            self.record_behavior(item_id, "acknowledge")
        return {"item": self.get_item(item_id), "cancelled_follow_up_count": len(rows),
                "cron_job_ids_to_remove": [r["cron_job_id"] for r in rows if r["cron_job_id"]]}

    def snooze(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = self.get_item(payload.get("item_id") or payload.get("id"))
        if payload.get("until"):
            trigger = parse_dt(payload["until"], self.tz_name)
        else:
            trigger = self.now + timedelta(minutes=int(payload.get("minutes", 15)))
        if not trigger or trigger <= self.now:
            raise SecretaryError("snooze target must be in the future")
        reminder_id = new_id("rem")
        message = f"稍后提醒：{item['title']}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (reminder_id, item["id"], "snooze", iso_utc(trigger), "pending_cron", None, 0, 0,
                 message, iso_utc(self.now), iso_utc(self.now)),
            )
            self.record_behavior(item["id"], "snooze", {"until": iso_utc(trigger)})
        return {"item": item, "cron_plan": [{"reminder_id": reminder_id, "kind": "snooze",
                 "trigger_at": iso_utc(trigger), "follow_up": False, "batched": False,
                 "needs_cron": True, "message": message}]}

    def reschedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("item_id") or payload.get("id")
        if payload.get("deadline_at"):
            self.update_item({"item_id": item_id, "fields": {"deadline_at": payload["deadline_at"]}})
        item = self.get_item(item_id)
        old = [dict(r) for r in self.conn.execute(
            "SELECT * FROM reminders WHERE item_id=? AND status IN ('active','pending_cron','batched')", (item_id,)
        )]
        specs = self.reminder_specs(item)
        new_plan = []
        with self.conn:
            for spec in specs:
                reminder_id = new_id("rem")
                status = "batched" if spec["batched"] else "pending_cron"
                self.conn.execute(
                    "INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (reminder_id, item_id, spec["kind"], spec["trigger_at"], status, None,
                     int(spec["follow_up"]), int(spec["batched"]), spec["message"], iso_utc(self.now), iso_utc(self.now)),
                )
                if spec["needs_cron"]:
                    new_plan.append({"reminder_id": reminder_id, **spec})
            self.conn.execute("UPDATE items SET status='pending_schedule',updated_at=? WHERE id=?",
                              (iso_utc(self.now), item_id))
            self.record_behavior(item_id, "reschedule_requested", {"new_count": len(specs)})
        return {"item": self.get_item(item_id), "cron_plan": new_plan,
                "remove_after_success": [r["cron_job_id"] for r in old if r["cron_job_id"]],
                "old_reminder_ids": [r["id"] for r in old],
                "protocol": "create new cron jobs; bind them; then remove old jobs; on failure mark sync_error"}

    def mark_sync_error(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("item_id") or payload.get("id")
        before = self.get_item(item_id)
        with self.conn:
            if before["status"] not in ("completed", "cancelled", "archived"):
                self.conn.execute("UPDATE items SET status='sync_error',updated_at=? WHERE id=?", (iso_utc(self.now), item_id))
            self.record_behavior(item_id, "sync_error", payload.get("error") or {})
        return {"item": self.get_item(item_id), "terminal_status_preserved": before["status"] in ("completed", "cancelled", "archived")}

    def conflicts(self, payload: dict[str, Any]) -> dict[str, Any]:
        start = parse_dt(payload.get("start_at"), self.tz_name)
        end = parse_dt(payload.get("end_at"), self.tz_name)
        if not start or not end or end <= start:
            raise SecretaryError("conflicts requires start_at and end_at with end after start")
        exclude = payload.get("exclude_item_id")
        rows = self.conn.execute(
            "SELECT id FROM items WHERE type='event' AND status IN ('active','scheduled','pending_schedule') "
            "AND start_at<? AND end_at>? AND (? IS NULL OR id<>?) ORDER BY start_at",
            (iso_utc(end), iso_utc(start), exclude, exclude),
        ).fetchall()
        return {"conflicts": [self.get_item(r["id"]) for r in rows], "count": len(rows)}

    def list_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        clauses, values = [], []
        for field in ("status", "type", "category", "project_id", "priority_label"):
            if payload.get(field):
                clauses.append(f"{field}=?")
                values.append(payload[field])
        if not clauses:
            clauses.append("status NOT IN ('archived','cancelled')")
        rows = self.conn.execute(
            "SELECT id FROM items WHERE " + " AND ".join(clauses) + " ORDER BY priority_score DESC,deadline_at,created_at",
            values,
        ).fetchall()
        items = [self.get_item(r["id"]) for r in rows]
        return {"items": items, "count": len(items)}

    def _period_bounds(self, period: str) -> tuple[datetime, datetime, str]:
        """Return current calendar-period bounds in UTC plus a user-facing name."""
        tz = ZoneInfo(self.tz_name)
        local_now = self.now.astimezone(tz)
        if period == "week":
            start_date = local_now.date() - timedelta(days=local_now.weekday())
            end_date = start_date + timedelta(days=7)
            name = "本周"
        elif period == "month":
            start_date = local_now.date().replace(day=1)
            if start_date.month == 12:
                end_date = date(start_date.year + 1, 1, 1)
            else:
                end_date = date(start_date.year, start_date.month + 1, 1)
            name = "本月"
        else:
            raise SecretaryError("agenda period must be week or month")
        start = datetime.combine(start_date, time.min, tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(end_date, time.min, tzinfo=tz).astimezone(timezone.utc)
        return start, end, name

    def _date_label(self, value: str | datetime | None, *, include_time: bool = True) -> str:
        parsed = parse_dt(value, self.tz_name)
        if not parsed:
            return "时间未定"
        local = parsed.astimezone(ZoneInfo(self.tz_name))
        label = f"{local.month}月{local.day}日"
        if include_time and (local.hour, local.minute) not in {(0, 0), (23, 59)}:
            label += f" {local.hour:02d}:{local.minute:02d}"
        return label

    def _current_priority_view(self, item: dict[str, Any]) -> dict[str, Any]:
        view = dict(item)
        ranking = self.priority(view)
        view["priority_label"] = ranking["label"]
        view["priority_score"] = ranking["score"]
        view["priority_reasons"] = ranking["reasons"]
        view["priority_components"] = ranking["components"]
        return view

    @staticmethod
    def _unique_items(groups: Iterable[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for group in groups:
            for item in group:
                unique[item["id"]] = item
        return list(unique.values())

    def _render_agenda(self, result: dict[str, Any], section_limit: int) -> str:
        period_name = result["period_name"]
        start_label = self._date_label(result["period_start"], include_time=False)
        end_label = self._date_label(
            parse_dt(result["period_end"], self.tz_name) - timedelta(days=1), include_time=False
        )
        counts = result["counts"]
        tasks = result["tasks"]
        events = result["events"]
        projects = result["projects"]
        reviews = result["reviews"]
        risk_count = counts["risk_total"]

        lines = [f"【{period_name}事项｜{start_label}—{end_label}】"]
        if counts["total"] == 0:
            lines.append(f"结论：截至当前，{period_name}没有已记录的未完成事项。")
            return "\n".join(lines)

        conclusion = (
            f"结论：还剩{counts['total']}项；待办{counts['tasks']}项，"
            f"硬日程{counts['events']}项，项目节点{counts['projects']}项，复查{counts['reviews']}项。"
        )
        if risk_count:
            conclusion += f"其中{risk_count}项存在逾期、最迟开始点或提醒同步风险。"
        if tasks:
            conclusion += f"建议优先处理“{tasks[0]['title']}”。"
        elif events:
            conclusion += f"最近安排是“{events[0]['title']}”。"
        lines.append(conclusion)

        overdue_ids = {item["id"] for item in result["overdue"]}
        missed_ids = {item["id"] for item in result["start_by_missed"]}

        if tasks:
            lines.extend(["", "【优先待办】"])
            for index, item in enumerate(tasks[:section_limit], 1):
                meta = [item.get("priority_label") or "P4"]
                if item["id"] in overdue_ids:
                    meta.append(f"已逾期；原截止{self._date_label(item.get('deadline_at'))}")
                elif item["id"] in missed_ids:
                    meta.append(f"已过最迟开始点{self._date_label(item.get('start_by_at'))}")
                    if item.get("deadline_at"):
                        meta.append(f"截止{self._date_label(item['deadline_at'])}")
                elif item.get("deadline_at"):
                    meta.append(f"截止{self._date_label(item['deadline_at'])}")
                elif item.get("start_by_at"):
                    meta.append(f"最迟开始{self._date_label(item['start_by_at'])}")
                if item.get("status") == "sync_error":
                    meta.append("提醒同步待修复")
                lines.append(f"{index}. {item['title']}；" + "；".join(meta))
                if index <= 3 and item.get("priority_reasons"):
                    lines.append("   排序依据：" + "；".join(item["priority_reasons"][:2]))
            if len(tasks) > section_limit:
                lines.append(f"其余{len(tasks) - section_limit}项待办未展开。")

        if events:
            lines.extend(["", "【硬日程】"])
            for index, item in enumerate(events[:section_limit], 1):
                end = f"—{self._date_label(item.get('end_at'))}" if item.get("end_at") else ""
                lines.append(f"{index}. {self._date_label(item.get('start_at'))}{end}；{item['title']}")
            if len(events) > section_limit:
                lines.append(f"其余{len(events) - section_limit}项日程未展开。")

        if projects:
            lines.extend(["", "【项目节点】"])
            for index, project in enumerate(projects[:section_limit], 1):
                status = "已逾期" if parse_dt(project.get("deadline_at"), self.tz_name) < self.now else "截止"
                lines.append(f"{index}. {project['title']}；{status}{self._date_label(project.get('deadline_at'))}")
            if len(projects) > section_limit:
                lines.append(f"其余{len(projects) - section_limit}个项目节点未展开。")

        if reviews:
            lines.extend(["", "【等待与复查】"])
            for index, item in enumerate(reviews[:section_limit], 1):
                owner = item.get("waiting_for") or item.get("delegated_to")
                suffix = f"；对象：{owner}" if owner else ""
                lines.append(f"{index}. {item['title']}；{self._date_label(item.get('review_at'))}复查{suffix}")
            if len(reviews) > section_limit:
                lines.append(f"其余{len(reviews) - section_limit}项复查未展开。")

        omitted = sum(max(0, len(group) - section_limit) for group in (tasks, events, projects, reviews))
        if omitted:
            lines.extend(["", f"回复“展开{period_name}全部事项”，可查看剩余{omitted}项。"])
        return "\n".join(lines)

    def agenda(self, period: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Return an exact, remaining-this-period agenda for conversational queries."""
        start, end, period_name = self._period_bounds(period)
        scope = payload.get("scope", "remaining")
        if scope not in {"remaining", "full"}:
            raise SecretaryError("agenda scope must be remaining or full")
        window_start = max(start, self.now) if scope == "remaining" else start
        section_limit = max(1, min(20, int(payload.get(
            "section_limit", self.setting("wechat_section_limit", 5)
        ))))

        rows = self.conn.execute(
            "SELECT id FROM items WHERE status IN "
            "('active','scheduled','pending_schedule','sync_error','waiting','delegated','someday')"
        ).fetchall()
        items = [self.get_item(row["id"]) for row in rows]
        task_views = [self._current_priority_view(item) for item in items
                      if item["type"] == "task" and item["status"] in ACTIVE_STATES]

        def in_window(value: str | None) -> bool:
            parsed = parse_dt(value, self.tz_name)
            return bool(parsed and window_start <= parsed < end)

        overdue = [item for item in task_views
                   if item.get("deadline_at") and parse_dt(item["deadline_at"], self.tz_name) < self.now]
        start_by_missed = [item for item in task_views
                           if item.get("start_by_at")
                           and parse_dt(item["start_by_at"], self.tz_name) < self.now
                           and (not item.get("deadline_at")
                                or parse_dt(item["deadline_at"], self.tz_name) >= self.now)]
        due_in_period = [item for item in task_views if in_window(item.get("deadline_at"))]
        start_in_period = [item for item in task_views if in_window(item.get("start_by_at"))]
        undated_priority = [item for item in task_views
                            if not item.get("deadline_at") and not item.get("start_by_at")
                            and item.get("priority_label") in {"P1", "P2"}]
        tasks = self._unique_items([overdue, start_by_missed, due_in_period, start_in_period, undated_priority])
        tasks.sort(key=lambda item: (
            {"P1": 4, "P2": 3, "P3": 2, "P4": 1}.get(item.get("priority_label"), 0),
            item.get("priority_score") or 0,
        ), reverse=True)

        events = [item for item in items if item["type"] == "event" and in_window(item.get("start_at"))]
        events.sort(key=lambda item: parse_dt(item.get("start_at"), self.tz_name))

        reviews = [item for item in items if item["type"] in {"waiting", "someday"}
                   and in_window(item.get("review_at"))]
        reviews.sort(key=lambda item: parse_dt(item.get("review_at"), self.tz_name))

        project_rows = self.conn.execute(
            "SELECT * FROM projects WHERE status='active' ORDER BY deadline_at"
        ).fetchall()
        projects = [dict(row) for row in project_rows if row["deadline_at"] and (
            in_window(row["deadline_at"]) or parse_dt(row["deadline_at"], self.tz_name) < self.now
        )]

        sync_errors = [item for item in tasks + events + reviews if item.get("status") == "sync_error"]
        risk_ids = {item["id"] for item in overdue + start_by_missed + sync_errors}
        total = len(tasks) + len(events) + len(projects) + len(reviews)
        result = {
            "kind": "agenda",
            "period": period,
            "period_name": period_name,
            "scope": scope,
            "as_of": iso_utc(self.now),
            "period_start": iso_utc(start),
            "period_end": iso_utc(end),
            "remaining_from": iso_utc(window_start),
            "tasks": tasks,
            "events": events,
            "projects": projects,
            "reviews": reviews,
            "overdue": overdue,
            "start_by_missed": start_by_missed,
            "due_in_period": due_in_period,
            "start_in_period": start_in_period,
            "sync_errors": sync_errors,
            "counts": {
                "total": total,
                "tasks": len(tasks),
                "events": len(events),
                "projects": len(projects),
                "reviews": len(reviews),
                "overdue": len(overdue),
                "start_by_missed": len(start_by_missed),
                "sync_error": len(sync_errors),
                "risk_total": len(risk_ids),
                "p1": sum(1 for item in tasks if item.get("priority_label") == "P1"),
            },
            "output_contract": {
                "format": "wechat_plain_text_v1",
                "conclusion_first": True,
                "markdown_tables": False,
                "raw_json": False,
            },
        }
        result["wechat_text"] = self._render_agenda(result, section_limit)
        return result

    @staticmethod
    def _render_plan_now(recommendations: list[dict[str, Any]]) -> str:
        lines = ["【当前行动建议】"]
        if not recommendations:
            lines.append("结论：当前条件下没有匹配的可执行待办。可以增加可用时间，或调整精力、地点条件后再看。")
            return "\n".join(lines)
        first = recommendations[0]["item"]["title"]
        lines.append(f"结论：先做“{first}”；以下最多保留3个选择。")
        for index, recommendation in enumerate(recommendations, 1):
            item = recommendation["item"]
            estimate = f"；预计{item['estimate_minutes']}分钟" if item.get("estimate_minutes") else ""
            reasons = "；".join(recommendation.get("reasons", [])[:2])
            lines.append(f"{index}. {item['title']}；{recommendation['priority_label']}{estimate}")
            if reasons:
                lines.append(f"   依据：{reasons}")
        return "\n".join(lines)

    def plan_now(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(3, int(payload.get("limit", 3))))
        available = int(payload["available_minutes"]) if payload.get("available_minutes") is not None else None
        energy = payload.get("energy")
        context = payload.get("context")
        rows = self.conn.execute(
            "SELECT id FROM items WHERE type='task' AND status IN ('active','scheduled','pending_schedule','sync_error')"
        ).fetchall()
        ranked = []
        for row in rows:
            item = self.get_item(row["id"])
            if available is not None and item.get("estimate_minutes") and int(item["estimate_minutes"]) > available:
                continue
            ranking = self.priority(item, context=context, energy=energy, available_minutes=available)
            ranked.append({"item": item, "recommendation_score": ranking["score"],
                           "priority_label": ranking["label"], "reasons": ranking["reasons"],
                           "components": ranking["components"]})
        ranked.sort(key=lambda x: ({"P1": 4, "P2": 3, "P3": 2, "P4": 1}[x["priority_label"]], x["recommendation_score"]), reverse=True)
        recommendations = ranked[:limit]
        return {"recommendations": recommendations, "considered": len(ranked),
                "constraints": {"available_minutes": available, "energy": energy, "context": context},
                "wechat_text": self._render_plan_now(recommendations),
                "output_contract": {"format": "wechat_plain_text_v1", "markdown_tables": False}}

    def _render_daily_digest(self, result: dict[str, Any]) -> str:
        lines = [f"【今日简报｜{result['date']}】"]
        focus = result["focus"]
        events = result["events"]
        overdue = result["overdue"]
        waiting = result["waiting"]
        lines.append(
            f"结论：今日重点{len(focus)}项，硬日程{len(events)}项，逾期{len(overdue)}项，等待{len(waiting)}项。"
            + (f"建议先处理“{focus[0]['title']}”。" if focus else "")
        )
        if focus:
            lines.extend(["", "【今日重点】"])
            for index, item in enumerate(focus, 1):
                deadline = f"；截止{self._date_label(item.get('deadline_at'))}" if item.get("deadline_at") else ""
                lines.append(f"{index}. {item['title']}；{item.get('priority_label') or 'P4'}{deadline}")
        if events:
            lines.extend(["", "【硬日程】"])
            for index, item in enumerate(events, 1):
                lines.append(f"{index}. {self._date_label(item.get('start_at'))}；{item['title']}")
        if overdue:
            lines.extend(["", "【风险提示】"])
            for index, item in enumerate(overdue[:5], 1):
                lines.append(f"{index}. {item['title']}；原截止{self._date_label(item.get('deadline_at'))}")
            if len(overdue) > 5:
                lines.append(f"另有{len(overdue) - 5}项逾期事项未展开。")
        if waiting:
            lines.extend(["", f"【等待事项】共{len(waiting)}项；优先检查最久未更新的事项。"])
        return "\n".join(lines)

    @staticmethod
    def _append_management_section(base: str, title: str, notes: list[str]) -> str:
        if not notes:
            return base
        lines = [base, "", f"【{title}】"]
        lines.extend(f"{index}. {note}" for index, note in enumerate(notes, 1))
        return "\n".join(lines)

    def digest(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        tz = ZoneInfo(self.tz_name)
        local_now = self.now.astimezone(tz)
        today_start = datetime.combine(local_now.date(), time.min, tzinfo=tz).astimezone(timezone.utc)
        today_end = today_start + timedelta(days=1)
        active_rows = self.conn.execute(
            "SELECT id FROM items WHERE status IN ('active','scheduled','pending_schedule','sync_error','waiting','delegated','someday')"
        ).fetchall()
        items = [self.get_item(r["id"]) for r in active_rows]
        actionable = [self._current_priority_view(i) for i in items
                      if i["type"] == "task" and i["status"] in ACTIVE_STATES]
        actionable.sort(key=lambda i: ({"P1": 4, "P2": 3, "P3": 2, "P4": 1}.get(i["priority_label"], 0), i["priority_score"] or 0), reverse=True)
        if kind == "daily":
            events = [i for i in items if i["type"] == "event" and i.get("start_at") and today_start <= parse_dt(i["start_at"], self.tz_name) < today_end]
            events.sort(key=lambda item: parse_dt(item.get("start_at"), self.tz_name))
            overdue = [i for i in actionable if i.get("deadline_at") and parse_dt(i["deadline_at"], self.tz_name) < self.now]
            start_by_missed = [i for i in actionable if i.get("start_by_at") and parse_dt(i["start_by_at"], self.tz_name) < self.now]
            result = {"kind": kind, "date": str(local_now.date()), "focus": actionable[:3], "events": events,
                      "start_by_missed": start_by_missed, "overdue": overdue, "secondary": actionable[3:],
                      "waiting": [i for i in items if i["status"] in ("waiting", "delegated")],
                      "output_contract": {"format": "wechat_plain_text_v1", "markdown_tables": False}}
            result["wechat_text"] = self._render_daily_digest(result)
            return result
        if kind == "weekly":
            agenda = self.agenda("week", {"scope": "remaining", "section_limit": payload.get("section_limit", 5)})
            since = self.now - timedelta(days=7)
            stats = self._behavior_stats(since)
            zombies = [dict(r) for r in self.conn.execute(
                "SELECT * FROM projects WHERE status='active' AND current_next_action_item_id IS NULL"
            )]
            ideas = [i for i in items if i["type"] == "idea"]
            recs = [dict(r) for r in self.conn.execute("SELECT * FROM recommendations WHERE status='pending' ORDER BY created_at")]
            waiting = [i for i in items if i["status"] in ("waiting", "delegated")]
            behavior = stats["events"]
            notes = [
                f"过去7天完成{behavior.get('complete', 0)}项，延期请求{behavior.get('reschedule_requested', 0)}项，取消{behavior.get('cancel', 0)}项。",
                f"没有下一步的活跃项目{len(zombies)}个；等待或委派事项{len(waiting)}项。",
                f"待回顾灵感{len(ideas)}条；待确认规则建议{len(recs)}条。",
            ]
            result = {"kind": kind, "week_start": str((local_now - timedelta(days=local_now.weekday())).date()),
                      "week_end": agenda["period_end"], "agenda": agenda,
                      "events": agenda["events"], "priorities": agenda["tasks"],
                      "last_week": stats, "zombie_projects": zombies,
                      "waiting": waiting, "ideas": ideas, "rule_recommendations": recs,
                      "output_contract": {"format": "wechat_plain_text_v1", "markdown_tables": False}}
            result["wechat_text"] = self._append_management_section(agenda["wechat_text"], "上周回顾与管理", notes)
            return result
        if kind == "monthly":
            agenda = self.agenda("month", {"scope": "remaining", "section_limit": payload.get("section_limit", 5)})
            since = self.now - timedelta(days=30)
            stats = self._behavior_stats(since)
            someday = [i for i in items if i["type"] == "someday"]
            ideas = [i for i in items if i["type"] == "idea"]
            projects = [dict(r) for r in self.conn.execute("SELECT * FROM projects WHERE status='active' ORDER BY deadline_at")]
            p2 = [i for i in actionable if i["priority_label"] == "P2"]
            recs = self.recommend_rules({})["recommendations"]
            behavior = stats["events"]
            notes = [
                f"过去30天完成{behavior.get('complete', 0)}项，延期请求{behavior.get('reschedule_requested', 0)}项，取消{behavior.get('cancel', 0)}项。",
                f"活跃项目{len(projects)}个；需要防止被紧急事务挤压的P2事项{len(p2)}项。",
                f"Someday待复查{len(someday)}项；灵感{len(ideas)}条；待确认规则建议{len(recs)}条。",
            ]
            result = {"kind": kind, "month": local_now.strftime("%Y-%m"), "agenda": agenda,
                      "priorities": agenda["tasks"], "projects": projects, "p2_watch": p2,
                      "someday_review": someday, "ideas": ideas, "metrics": stats,
                      "rule_recommendations": recs,
                      "output_contract": {"format": "wechat_plain_text_v1", "markdown_tables": False}}
            result["wechat_text"] = self._append_management_section(agenda["wechat_text"], "月度管理观察", notes)
            return result
        raise SecretaryError("digest kind must be daily, weekly, or monthly")

    def _behavior_stats(self, since: datetime) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT event_type,COUNT(*) n FROM behavior_events WHERE occurred_at>=? GROUP BY event_type", (iso_utc(since),)
        ).fetchall()
        stats = {r["event_type"]: r["n"] for r in rows}
        ratios = []
        for row in self.conn.execute(
            "SELECT estimate_minutes,actual_minutes FROM items WHERE completed_at>=? AND estimate_minutes>0 AND actual_minutes>0", (iso_utc(since),)
        ):
            ratios.append(row["actual_minutes"] / row["estimate_minutes"])
        return {"events": stats, "estimate_ratio_mean": round(statistics.mean(ratios), 2) if ratios else None,
                "completed_with_actual": len(ratios)}

    def record_actual(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("item_id") or payload.get("id")
        item = self.get_item(item_id)
        actual = int(payload.get("actual_minutes", 0))
        if actual <= 0:
            raise SecretaryError("actual_minutes must be positive")
        with self.conn:
            self.conn.execute("UPDATE items SET actual_minutes=?,updated_at=? WHERE id=?", (actual, iso_utc(self.now), item_id))
            ratio = actual / item["estimate_minutes"] if item.get("estimate_minutes") else None
            self.record_behavior(item_id, "record_actual", {"actual_minutes": actual, "ratio": ratio})
        return {"item": self.get_item(item_id), "estimate_ratio": round(ratio, 3) if ratio else None}

    def recommend_rules(self, payload: dict[str, Any]) -> dict[str, Any]:
        minimum = int(self.setting("learning_min_samples", 8))
        rows = self.conn.execute(
            "SELECT category,estimate_minutes,actual_minutes FROM items "
            "WHERE status='completed' AND estimate_minutes>0 AND actual_minutes>0 ORDER BY category"
        ).fetchall()
        grouped: dict[str, list[float]] = {}
        for row in rows:
            grouped.setdefault(row["category"], []).append(row["actual_minutes"] / row["estimate_minutes"])
        created = []
        existing = []
        for category, ratios in grouped.items():
            if len(ratios) < minimum:
                continue
            median_ratio = statistics.median(ratios)
            current = float(self.setting(f"buffer_multiplier:{category}", self.setting("buffer_multiplier", 1.25)))
            suggested = round(max(1.0, min(2.0, median_ratio)), 2)
            if abs(suggested - current) < 0.1:
                continue
            present = self.conn.execute(
                "SELECT * FROM recommendations WHERE category=? AND kind='buffer_multiplier' AND status='pending'",
                (category,),
            ).fetchone()
            if present:
                existing.append(dict(present))
                continue
            rec_id = new_id("rec")
            evidence = {"sample_count": len(ratios), "median_ratio": round(median_ratio, 3),
                        "mean_ratio": round(statistics.mean(ratios), 3), "current_multiplier": current}
            with self.conn:
                self.conn.execute(
                    "INSERT INTO recommendations VALUES(?,?,?,?,?,?,?,?)",
                    (rec_id, category, "buffer_multiplier", json_dumps(evidence), suggested, "pending", iso_utc(self.now), None),
                )
            created.append(dict(self.conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()))
        return {"recommendations": existing + created, "minimum_samples": minimum,
                "policy": "suggestions only; settings change only through accept-rule with confirmed=true"}

    def accept_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise SecretaryError("accept-rule requires confirmed=true")
        rec_id = payload.get("recommendation_id") or payload.get("id")
        row = self.conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
        if not row or row["status"] != "pending":
            raise SecretaryError("pending recommendation not found")
        key = f"buffer_multiplier:{row['category']}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json_dumps(row["suggested_value"]), iso_utc(self.now)),
            )
            self.conn.execute("UPDATE recommendations SET status='accepted',decided_at=? WHERE id=?", (iso_utc(self.now), rec_id))
            self.audit("setting", key, "accept_rule", after={"value": row["suggested_value"], "recommendation_id": rec_id})
        return {"recommendation_id": rec_id, "setting": key, "value": row["suggested_value"]}

    def backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = payload.get("output")
        if target:
            dest = Path(target).expanduser().resolve()
        else:
            stamp = self.now.strftime("%Y%m%dT%H%M%SZ")
            dest = self.path.with_name(f"{self.path.stem}.backup-{stamp}{self.path.suffix}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup_conn = sqlite3.connect(str(dest))
        try:
            self.conn.backup(backup_conn)
        finally:
            backup_conn.close()
        return {"backup": str(dest), "bytes": dest.stat().st_size}

    def export(self, payload: dict[str, Any]) -> dict[str, Any]:
        fmt = payload.get("format", "json").lower()
        target = Path(payload.get("output") or self.path.parent / f"export.{fmt}").expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tables = ("projects", "items", "reminders", "behavior_events", "recommendations", "settings")
        if fmt == "json":
            data = {table: [dict(r) for r in self.conn.execute(f"SELECT * FROM {table}")] for table in tables}
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            files = [str(target)]
        elif fmt == "csv":
            target.mkdir(parents=True, exist_ok=True)
            files = []
            for table in tables:
                rows = [dict(r) for r in self.conn.execute(f"SELECT * FROM {table}")]
                path = target / f"{table}.csv"
                with path.open("w", newline="", encoding="utf-8-sig") as handle:
                    fields = list(rows[0]) if rows else ["empty"]
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                files.append(str(path))
        else:
            raise SecretaryError("export format must be json or csv")
        return {"format": fmt, "files": files}


def load_payload(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SecretaryError(f"invalid --payload JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SecretaryError("--payload must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local deterministic engine for personal-secretary-reminders")
    parser.add_argument("command", choices=[
        "init", "doctor", "draft", "clarify", "finalize", "create-project", "set-next-action",
        "get", "update", "complete", "cancel", "ack", "snooze", "reschedule", "fire-reminder", "mark-sync-error",
        "conflicts", "list", "agenda", "plan-now", "digest", "record-actual", "recommend-rules", "accept-rule",
        "backup", "export",
    ])
    parser.add_argument("argument", nargs="?", help="agenda period (week/month) or digest kind")
    parser.add_argument("--db", default=str(default_database_path()))
    parser.add_argument("--payload", help="JSON object for the command")
    parser.add_argument("--now", help="deterministic ISO timestamp, primarily for testing")
    return parser


def dispatch(db: SecretaryDB, command: str, argument: str | None, payload: dict[str, Any]) -> Any:
    db.initialize()
    handlers = {
        "init": lambda: db.initialize(), "doctor": lambda: db.doctor(),
        "draft": lambda: db.create_draft(payload), "clarify": lambda: db.clarify(payload),
        "finalize": lambda: db.finalize(payload), "create-project": lambda: db.create_project(payload),
        "set-next-action": lambda: db.set_next_action(payload), "get": lambda: db.get(payload),
        "update": lambda: db.update_item(payload), "complete": lambda: db.complete(payload),
        "cancel": lambda: db.cancel(payload), "ack": lambda: db.ack(payload),
        "snooze": lambda: db.snooze(payload), "reschedule": lambda: db.reschedule(payload),
        "fire-reminder": lambda: db.fire_reminder(payload),
        "mark-sync-error": lambda: db.mark_sync_error(payload), "conflicts": lambda: db.conflicts(payload),
        "list": lambda: db.list_items(payload),
        "agenda": lambda: db.agenda(argument or payload.get("period", "week"), payload),
        "plan-now": lambda: db.plan_now(payload),
        "digest": lambda: db.digest(argument or payload.get("kind", "daily"), payload),
        "record-actual": lambda: db.record_actual(payload), "recommend-rules": lambda: db.recommend_rules(payload),
        "accept-rule": lambda: db.accept_rule(payload), "backup": lambda: db.backup(payload),
        "export": lambda: db.export(payload),
    }
    return handlers[command]()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = None
    try:
        payload = load_payload(args.payload)
        now = parse_dt(args.now, "Asia/Shanghai") if args.now else None
        db = SecretaryDB(Path(args.db), now)
        result = dispatch(db, args.command, args.argument, payload)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except SecretaryError as exc:
        print(json.dumps({"ok": False, "error": {"type": "validation_error", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(json.dumps({"ok": False, "error": {"type": "database_error", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 3
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
