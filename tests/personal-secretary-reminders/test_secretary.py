import importlib.util
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "personal-secretary-reminders"
SCRIPT = SKILL_ROOT / "scripts" / "secretary.py"
CRON_RUNNER = SKILL_ROOT / "scripts" / "cron_runner.py"
SPEC = importlib.util.spec_from_file_location("secretary", SCRIPT)
secretary = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(secretary)


class SecretaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "reminders.sqlite3"
        self.now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)  # 10:00 Shanghai
        self.db = secretary.SecretaryDB(self.db_path, self.now)
        self.db.initialize()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def task_fields(self, **overrides):
        fields = {
            "type": "task",
            "title": "完成季度报告初稿",
            "desired_outcome": "形成可供经理审核的初稿",
            "next_action": "列出报告大纲",
            "deadline_at": "2026-07-16T17:30:00+08:00",
            "importance": 5,
            "category": "报告撰写",
            "estimate_minutes": 120,
            "energy": "high",
            "context": "电脑",
        }
        fields.update(overrides)
        return fields

    def finalize(self, fields):
        return self.db.finalize({"confirmed": True, "fields": fields})

    def test_init_and_doctor(self):
        result = self.db.doctor()
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(self.db.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_monorepo_skill_folder_matches_minimal_frontmatter(self):
        skill_file = SKILL_ROOT / "SKILL.md"
        lines = skill_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "---")
        closing = lines.index("---", 1)
        frontmatter = lines[1:closing]
        keys = [line.split(":", 1)[0] for line in frontmatter if ":" in line]
        self.assertEqual(keys, ["name", "description"])
        name = frontmatter[0].split(":", 1)[1].strip()
        self.assertEqual(name, SKILL_ROOT.name)

    def test_openclaw_data_path_is_separate_from_skill_code(self):
        fake_home = Path(self.temp.name) / "home"
        fake_home.mkdir()
        openclaw_path = secretary.default_database_path(
            home=fake_home,
            environ={},
        )
        self.assertEqual(openclaw_path, fake_home / ".openclaw" / "data" / secretary.APP_NAME / "reminders.sqlite3")
        self.assertNotIn("skills", openclaw_path.parts)

    def test_runtime_data_path_honors_explicit_overrides(self):
        fake_home = Path(self.temp.name) / "home"
        explicit = Path(self.temp.name) / "private" / "secretary.sqlite3"
        selected = secretary.default_database_path(
            home=fake_home,
            environ={"PERSONAL_SECRETARY_DB": str(explicit)},
        )
        self.assertEqual(selected, explicit)

    def test_draft_clarify_requires_confirmation(self):
        draft = self.db.create_draft({
            "raw_text": "月底前完成季度报告",
            "inferred_type": "task",
            "fields": {"title": "完成季度报告初稿"},
        })
        self.assertIn("desired_outcome", draft["missing_fields"])
        clarified = self.db.clarify({"draft_id": draft["id"], "fields": self.task_fields()})
        self.assertEqual(clarified["missing_fields"], [])
        with self.assertRaises(secretary.SecretaryError):
            self.db.finalize({"draft_id": draft["id"]})
        saved = self.db.finalize({"draft_id": draft["id"], "confirmed": True})
        self.assertEqual(saved["item"]["title"], "完成季度报告初稿")
        self.assertEqual(self.db.get_draft(draft["id"])["status"], "finalized")

    def test_unconfirmed_draft_expires_after_24_hours(self):
        draft = self.db.create_draft({"raw_text": "以后学一下摄影", "inferred_type": "someday"})
        self.db.now += timedelta(hours=25)
        expired = self.db.get_draft(draft["id"])
        self.assertEqual(expired["status"], "expired")

    def test_start_by_uses_25_percent_buffer_and_work_hours(self):
        result = self.finalize(self.task_fields())
        item = result["item"]
        # 120 * 1.25 = 150 working minutes before 17:30 => 15:00 local.
        self.assertEqual(item["start_by_at"], "2026-07-16T07:00:00Z")
        self.assertEqual(item["priority_label"], "P2")
        kinds = {r["kind"] for r in result["cron_plan"] + result["batched_reminders"]}
        self.assertIn("start_by", kinds)
        self.assertIn("progress_day_before", kinds)
        self.assertIn("deadline", kinds)

    def test_short_task_has_no_complex_buffer(self):
        result = self.finalize(self.task_fields(
            title="给张三打电话", desired_outcome="确认交付时间", next_action="拨打张三电话",
            estimate_minutes=10, deadline_at="2026-07-16T09:00:00+08:00", importance=3,
        ))
        item = result["item"]
        self.assertIsNone(item["start_by_at"])
        kinds = {r["kind"] for r in result["cron_plan"] + result["batched_reminders"]}
        self.assertIn("task_due_day", kinds)
        self.assertIn("task_ten_minutes", kinds)

    def test_idea_generates_no_reminders_but_appears_in_digest(self):
        result = self.finalize({"type": "idea", "title": "把月报做成视频", "category": "内容创意"})
        self.assertEqual(result["cron_plan"], [])
        self.assertEqual(result["batched_reminders"], [])
        weekly = self.db.digest("weekly", {})
        self.assertEqual([i["title"] for i in weekly["ideas"]], ["把月报做成视频"])

    def test_week_agenda_returns_remaining_items_and_current_risks(self):
        self.finalize({
            "type": "event", "title": "已经结束的周会",
            "start_at": "2026-07-14T09:00:00+08:00", "duration_minutes": 60,
            "importance": 3, "category": "会议",
        })
        self.finalize({
            "type": "event", "title": "本周客户会",
            "start_at": "2026-07-16T15:00:00+08:00", "duration_minutes": 60,
            "importance": 4, "category": "会议",
        })
        self.finalize({
            "type": "event", "title": "下周启动会",
            "start_at": "2026-07-20T09:00:00+08:00", "duration_minutes": 60,
            "importance": 4, "category": "会议",
        })
        overdue = self.finalize(self.task_fields(
            title="提交逾期材料", desired_outcome="材料完成提交", next_action="补齐缺失数据",
            deadline_at="2026-07-14T18:00:00+08:00", estimate_minutes=30,
        ))["item"]
        due = self.finalize(self.task_fields(
            title="完成本周报告", desired_outcome="报告可审核", next_action="完成分析章节",
            deadline_at="2026-07-17T18:00:00+08:00", estimate_minutes=60,
        ))["item"]
        self.finalize(self.task_fields(
            title="下周短任务", desired_outcome="短任务完成", next_action="执行短任务",
            deadline_at="2026-07-21T18:00:00+08:00", estimate_minutes=10,
        ))
        waiting = self.finalize({
            "type": "waiting", "title": "等小王发数据", "review_at": "2026-07-18T09:00:00+08:00",
            "importance": 4, "category": "报告", "waiting_for": "小王",
        })["item"]

        agenda = self.db.agenda("week", {})
        self.assertEqual([item["title"] for item in agenda["events"]], ["本周客户会"])
        self.assertIn(due["id"], {item["id"] for item in agenda["tasks"]})
        self.assertIn(overdue["id"], {item["id"] for item in agenda["overdue"]})
        self.assertEqual([item["id"] for item in agenda["reviews"]], [waiting["id"]])
        self.assertNotIn("下周短任务", {item["title"] for item in agenda["tasks"]})
        self.assertEqual(agenda["period_name"], "本周")
        self.assertEqual(agenda["scope"], "remaining")

    def test_full_week_scope_includes_past_event_but_remaining_does_not(self):
        self.finalize({
            "type": "event", "title": "周二复盘会",
            "start_at": "2026-07-14T09:00:00+08:00", "duration_minutes": 30,
            "importance": 3, "category": "会议",
        })
        remaining = self.db.agenda("week", {})
        full = self.db.agenda("week", {"scope": "full"})
        self.assertEqual(remaining["events"], [])
        self.assertEqual([item["title"] for item in full["events"]], ["周二复盘会"])

    def test_month_agenda_uses_calendar_month_boundary(self):
        july = self.finalize(self.task_fields(
            title="七月收尾", desired_outcome="七月工作收尾", next_action="核对收尾清单",
            deadline_at="2026-07-31T17:00:00+08:00", estimate_minutes=10,
        ))["item"]
        self.finalize(self.task_fields(
            title="八月事项", desired_outcome="八月事项完成", next_action="执行八月事项",
            deadline_at="2026-08-01T10:00:00+08:00", estimate_minutes=10,
        ))
        agenda = self.db.agenda("month", {})
        self.assertIn(july["id"], {item["id"] for item in agenda["tasks"]})
        self.assertNotIn("八月事项", {item["title"] for item in agenda["tasks"]})
        self.assertEqual(agenda["period_name"], "本月")

    def test_agenda_wechat_text_is_conclusion_first_and_table_free(self):
        for index in range(3):
            self.finalize(self.task_fields(
                title=f"本周事项{index}", desired_outcome=f"事项{index}完成", next_action=f"执行事项{index}",
                deadline_at=f"2026-07-{16 + index}T18:00:00+08:00", estimate_minutes=10,
            ))
        agenda = self.db.agenda("week", {"section_limit": 2})
        text = agenda["wechat_text"]
        self.assertTrue(text.startswith("【本周事项｜"))
        self.assertEqual(text.splitlines()[1].split("：", 1)[0], "结论")
        self.assertIn("【优先待办】", text)
        self.assertIn("回复“展开本周全部事项”", text)
        for banned in ("|", "```", "{\"", "priority_score", "item_id"):
            self.assertNotIn(banned, text)
        self.assertEqual(agenda["output_contract"]["format"], "wechat_plain_text_v1")

    def test_digest_and_plan_now_return_wechat_plain_text(self):
        self.finalize(self.task_fields(
            title="准备经营分析", desired_outcome="经营分析可汇报", next_action="整理关键数据",
            deadline_at="2026-07-17T18:00:00+08:00", estimate_minutes=30,
        ))
        weekly = self.db.digest("weekly", {})
        monthly = self.db.digest("monthly", {})
        daily = self.db.digest("daily", {})
        plan = self.db.plan_now({"available_minutes": 60, "energy": "high", "context": "电脑"})
        self.assertEqual([item["title"] for item in weekly["priorities"]], ["准备经营分析"])
        for result in (weekly, monthly, daily, plan):
            self.assertIn("wechat_text", result)
            self.assertNotIn("|", result["wechat_text"])
            self.assertNotIn("```", result["wechat_text"])

    def test_project_has_next_action_and_detects_zombie_after_completion(self):
        result = self.db.create_project({
            "title": "梳理家庭保险", "desired_outcome": "形成家庭保单与续保决策清单",
            "next_action": "收集所有现有保单", "deadline_at": "2026-08-31",
            "importance": 4, "category": "家庭", "estimate_minutes": 30,
        })
        project = result["project"]
        self.assertEqual(project["current_next_action_item_id"], result["next_action"]["id"])
        completed = self.db.complete({"item_id": result["next_action"]["id"]})
        self.assertTrue(completed["project_needs_next_action"])
        weekly = self.db.digest("weekly", {})
        self.assertEqual([p["id"] for p in weekly["zombie_projects"]], [project["id"]])

    def test_event_conflict_detection(self):
        event = self.finalize({
            "type": "event", "title": "项目会", "start_at": "2026-07-16T09:00:00+08:00",
            "duration_minutes": 60, "importance": 4, "category": "会议",
        })["item"]
        conflicts = self.db.conflicts({
            "start_at": "2026-07-16T09:30:00+08:00", "end_at": "2026-07-16T10:30:00+08:00"
        })
        self.assertEqual(conflicts["count"], 1)
        self.assertEqual(conflicts["conflicts"][0]["id"], event["id"])

    def test_priority_changes_with_time_and_plan_now_returns_at_most_three(self):
        for index in range(5):
            self.finalize(self.task_fields(
                title=f"任务{index}", desired_outcome=f"完成任务{index}", next_action=f"执行任务{index}",
                importance=5 if index == 0 else 3, estimate_minutes=20 + index,
                energy="low" if index < 2 else "high",
            ))
        result = self.db.plan_now({"available_minutes":30, "energy":"low", "context":"电脑", "limit":3})
        self.assertLessEqual(len(result["recommendations"]), 3)
        self.assertEqual(result["recommendations"][0]["item"]["title"], "任务0")
        self.assertIn(result["recommendations"][0]["priority_label"], {"P1", "P2"})

    def test_p1_follow_up_once_and_ack_cancels_it(self):
        result = self.finalize(self.task_fields(deadline_at="2026-07-15T20:00:00+08:00"))
        followups = [r for r in result["cron_plan"] if r["kind"] == "p1_follow_up"]
        self.assertEqual(len(followups), 1)
        self.db.bind_cron({"reminder_bindings": [{
            "reminder_id": followups[0]["reminder_id"], "cron_job_id": "job-follow"
        }]})
        ack = self.db.ack({"item_id": result["item"]["id"]})
        self.assertEqual(ack["cancelled_follow_up_count"], 1)
        self.assertEqual(ack["cron_job_ids_to_remove"], ["job-follow"])
        self.assertNotEqual(ack["item"]["status"], "completed")

    def test_fire_reminder_is_idempotent_and_suppresses_terminal_items(self):
        result = self.finalize(self.task_fields())
        reminder = result["cron_plan"][0]
        self.db.bind_cron({"reminder_bindings": [{
            "reminder_id": reminder["reminder_id"], "cron_job_id": "job-fire"
        }]})
        fired = self.db.fire_reminder({"reminder_id": reminder["reminder_id"]})
        self.assertTrue(fired["deliver"])
        self.assertEqual(fired["wechat_text"], reminder["message"])
        repeated = self.db.fire_reminder({"reminder_id": reminder["reminder_id"]})
        self.assertFalse(repeated["deliver"])
        self.assertIn("sent", repeated["reason"])

        terminal = self.finalize(self.task_fields(title="已经完成的事项"))
        terminal_reminder = terminal["cron_plan"][0]
        self.db.bind_cron({"reminder_bindings": [{
            "reminder_id": terminal_reminder["reminder_id"], "cron_job_id": "job-terminal"
        }]})
        self.db.complete({"item_id": terminal["item"]["id"]})
        suppressed = self.db.fire_reminder({"reminder_id": terminal_reminder["reminder_id"]})
        self.assertFalse(suppressed["deliver"])

    def test_cron_runner_outputs_exact_text_then_no_reply(self):
        result = self.finalize(self.task_fields())
        reminder = result["cron_plan"][0]
        self.db.bind_cron({"reminder_bindings": [{
            "reminder_id": reminder["reminder_id"], "cron_job_id": "job-runner"
        }]})
        first = subprocess.run(
            ["python3", str(CRON_RUNNER), "reminder", "--reminder-id", reminder["reminder_id"],
             "--db", str(self.db_path), "--now", "2026-07-15T10:00:00+08:00"],
            check=True, capture_output=True, text=True,
        )
        second = subprocess.run(
            ["python3", str(CRON_RUNNER), "reminder", "--reminder-id", reminder["reminder_id"],
             "--db", str(self.db_path), "--now", "2026-07-15T10:00:00+08:00"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(first.stdout.strip(), reminder["message"])
        self.assertEqual(second.stdout.strip(), "NO_REPLY")

    def test_pause_resume_reminder_state_and_terminal_sync_error(self):
        result = self.finalize(self.task_fields())
        reminder = result["cron_plan"][0]
        self.db.bind_cron({"reminder_bindings": [{
            "reminder_id": reminder["reminder_id"], "cron_job_id": "job-state"
        }]})
        paused = self.db.update_item({
            "action": "set-reminder-state", "reminder_ids": [reminder["reminder_id"]], "state": "paused"
        })
        self.assertEqual(paused["state"], "paused")
        resumed = self.db.update_item({
            "action": "set-reminder-state", "reminder_ids": [reminder["reminder_id"]], "state": "active"
        })
        self.assertEqual(resumed["cron_jobs"][0]["cron_job_id"], "job-state")
        completed = self.db.complete({"item_id": result["item"]["id"]})
        sync = self.db.mark_sync_error({"item_id": result["item"]["id"], "error": {"remove": "failed"}})
        self.assertEqual(sync["item"]["status"], "completed")
        self.assertTrue(sync["terminal_status_preserved"])

    def test_low_priority_system_reminders_are_batched(self):
        result = self.finalize(self.task_fields(importance=3))
        kinds = {r["kind"] for r in result["batched_reminders"]}
        self.assertIn("progress_day_before", kinds)
        self.assertIn("deadline", kinds)
        self.assertNotIn("start_by", kinds)

    def test_reschedule_returns_create_before_delete_protocol(self):
        result = self.finalize(self.task_fields())
        first = result["cron_plan"][0]
        self.db.bind_cron({"reminder_bindings": [{"reminder_id": first["reminder_id"], "cron_job_id": "old-job"}]})
        changed = self.db.reschedule({"item_id": result["item"]["id"], "deadline_at": "2026-07-18T17:30:00+08:00"})
        self.assertIn("old-job", changed["remove_after_success"])
        self.assertTrue(changed["cron_plan"])
        self.assertIn("create new cron jobs", changed["protocol"])
        new_first = changed["cron_plan"][0]
        bound = self.db.bind_cron({
            "reminder_bindings": [{"reminder_id": new_first["reminder_id"], "cron_job_id": "new-job"}],
            "retire_reminder_ids": changed["old_reminder_ids"],
        })
        self.assertIn("old-job", bound["cron_job_ids_to_remove"])
        old_status = self.db.conn.execute("SELECT status FROM reminders WHERE id=?", (first["reminder_id"],)).fetchone()[0]
        self.assertEqual(old_status, "replaced")

    def test_learning_threshold_and_explicit_acceptance(self):
        for index in range(7):
            result = self.finalize(self.task_fields(title=f"报告{index}"))
            self.db.complete({"item_id": result["item"]["id"], "actual_minutes": 180})
        self.assertEqual(self.db.recommend_rules({})["recommendations"], [])
        eighth = self.finalize(self.task_fields(title="报告7"))
        self.db.complete({"item_id": eighth["item"]["id"], "actual_minutes": 180})
        recs = self.db.recommend_rules({})["recommendations"]
        self.assertEqual(len(recs), 1)
        rec_id = recs[0]["id"]
        self.assertEqual(self.db.setting("buffer_multiplier:报告撰写", None), None)
        with self.assertRaises(secretary.SecretaryError):
            self.db.accept_rule({"recommendation_id": rec_id})
        accepted = self.db.accept_rule({"recommendation_id": rec_id, "confirmed": True})
        self.assertEqual(accepted["value"], 1.5)
        self.assertEqual(self.db.setting("buffer_multiplier:报告撰写"), 1.5)

    def test_backup_export_and_privacy_boundary(self):
        self.finalize({"type": "idea", "title": "一个灵感", "category": "创意"})
        backup = self.db.backup({"output": str(Path(self.temp.name) / "backup.sqlite3")})
        self.assertGreater(backup["bytes"], 0)
        exported = self.db.export({"format": "json", "output": str(Path(self.temp.name) / "export.json")})
        data = json.loads(Path(exported["files"][0]).read_text(encoding="utf-8"))
        self.assertEqual(data["items"][0]["title"], "一个灵感")
        package_root = SKILL_ROOT
        banned_suffixes = {".sqlite", ".sqlite3", ".db", ".key", ".pem"}
        self.assertFalse([p for p in package_root.rglob("*") if p.suffix.lower() in banned_suffixes])
        package_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in package_root.rglob("*")
            if path.is_file()
        )
        for banned in ("qclaw", "/Users/", "/root/", "PERSONAL_SECRETARY_RUNTIME"):
            self.assertNotIn(banned, package_text.lower() if banned == "qclaw" else package_text)

    def test_openclaw_deployment_tools_validate_and_dry_run(self):
        tools_dir = REPO_ROOT / "tools" / "openclaw-deploy"
        scripts = [
            tools_dir / "preflight_openclaw.sh",
            tools_dir / "deploy_openclaw.sh",
            tools_dir / "update_from_github.sh",
        ]
        subprocess.run(["bash", "-n", *(str(path) for path in scripts)], check=True)
        completed = subprocess.run(
            [str(tools_dir / "deploy_openclaw.sh"), "--source", str(SKILL_ROOT), "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("DRY_RUN_OK", completed.stdout)
        self.assertIn(".openclaw/data/personal-secretary-reminders", completed.stdout)

    def test_monorepo_update_selects_only_requested_skill_path(self):
        tools_dir = REPO_ROOT / "tools" / "openclaw-deploy"
        source_repo = Path(self.temp.name) / "source-repo"
        source_repo.mkdir()
        shutil.copytree(SKILL_ROOT, source_repo / SKILL_ROOT.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=source_repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=source_repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-m", "test skill"],
            cwd=source_repo, check=True, capture_output=True,
        )
        subprocess.run(["git", "tag", "personal-secretary-reminders-test"], cwd=source_repo, check=True)
        completed = subprocess.run(
            [str(tools_dir / "update_from_github.sh"),
             "--repo", str(source_repo),
             "--ref", "personal-secretary-reminders-test",
             "--skill-path", "personal-secretary-reminders",
             "--dry-run"],
            check=True, capture_output=True, text=True,
        )
        self.assertIn("DRY_RUN_OK", completed.stdout)
        self.assertIn("skill=personal-secretary-reminders", completed.stdout)

    def test_two_sqlite_connections_can_write_with_wal(self):
        self.db.close()

        def write_idea(index):
            connection = secretary.SecretaryDB(self.db_path, self.now)
            try:
                connection.initialize()
                return connection.finalize({"confirmed": True, "fields": {
                    "type": "idea", "title": f"并发灵感{index}", "category": "测试"
                }})["item"]["id"]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(write_idea, range(2)))
        check = secretary.SecretaryDB(self.db_path, self.now)
        try:
            self.assertEqual(len(set(ids)), 2)
            self.assertEqual(check.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
        finally:
            check.close()
        # tearDown expects an open connection.
        self.db = secretary.SecretaryDB(self.db_path, self.now)

    def test_cli_end_to_end_in_temporary_data_directory(self):
        cli_db = Path(self.temp.name) / "cli.sqlite3"

        def run(*args):
            completed = subprocess.run(
                ["python3", str(SCRIPT), *args, "--db", str(cli_db), "--now", "2026-07-15T10:00:00+08:00"],
                check=True, capture_output=True, text=True,
            )
            return json.loads(completed.stdout)

        self.assertTrue(run("init")["ok"])
        saved = run("finalize", "--payload", json.dumps({
            "confirmed": True,
            "fields": {"type": "idea", "title": "CLI 灵感", "category": "验收"},
        }, ensure_ascii=False))
        self.assertEqual(saved["result"]["item"]["title"], "CLI 灵感")
        weekly = run("digest", "weekly")
        self.assertEqual(weekly["result"]["ideas"][0]["title"], "CLI 灵感")
        agenda = run("agenda", "week")
        self.assertEqual(agenda["result"]["period"], "week")
        self.assertIn("wechat_text", agenda["result"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
