import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, local_wall_to_utc, to_local_iso


class TimezoneTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AirlineRecoveryService(Path(self.tmp.name) / "tz.db")
        self.svc.seed_airport("ops", "ops_manager", {"code": "JFK", "country": "US", "curfew_start": "23:00", "curfew_end": "05:00", "timezone": "America/New_York"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "PVG", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00", "timezone": "Asia/Shanghai"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": "2026-07-10T00:00:00Z"})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "JFK", "duty_start": "2026-07-02T12:00:00Z", "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "JFK", "destination": "PVG", "valid_from": "2026-07-01T00:00:00Z", "valid_to": "2026-07-04T00:00:00Z"})

    def tearDown(self):
        self.tmp.cleanup()

    def make_flight(self, number, std, sta):
        return self.svc.create_flight("sched", "scheduler", {"flight_no": number, "origin": "JFK", "destination": "PVG", "std": std, "sta": sta, "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 150})

    def make_plan(self, flight, std, sta):
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1", "starts_at": "2026-07-02T00:00:00Z", "ends_at": "2026-07-03T00:00:00Z"})
        return self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": f"方案-{flight['flight_no']}",
                                                             "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC1", "crew_id": "CR1", "new_std": std, "new_sta": sta}]})


class CurfewTimezoneTest(TimezoneTestBase):
    def test_curfew_judged_by_destination_local_time(self):
        # 到达 UTC 23:30：上海当地是次日 07:30，不在宵禁窗口（旧逻辑按 UTC 时钟会误判）
        ok_flight = self.make_flight("CA100", "2026-07-02T20:00:00Z", "2026-07-02T23:30:00Z")
        ok_plan = self.make_plan(ok_flight, "2026-07-02T20:00:00Z", "2026-07-02T23:30:00Z")
        check = self.svc.validate_plan(ok_plan["id"], "auditor", "auditor")
        self.assertTrue(check["valid"], check["problems"])

        # 到达 UTC 16:30：上海当地是次日 00:30，落在 23:00-05:00 宵禁窗口
        bad_flight = self.make_flight("CA101", "2026-07-02T12:00:00Z", "2026-07-02T16:30:00Z")
        bad_plan = self.make_plan(bad_flight, "2026-07-02T12:00:00Z", "2026-07-02T16:30:00Z")
        check = self.svc.validate_plan(bad_plan["id"], "auditor", "auditor")
        self.assertFalse(check["valid"])
        self.assertIn("airport_curfew", [p["code"] for p in check["problems"]])

    def test_state_and_plan_show_local_time(self):
        flight = self.make_flight("CA102", "2026-07-02T20:00:00Z", "2026-07-02T23:30:00Z")
        state = self.svc.state()
        airports = {a["code"]: a for a in state["airports"]}
        self.assertEqual(airports["JFK"]["timezone"], "America/New_York")
        self.assertEqual(airports["JFK"]["timezone_revision"], 1)
        self.assertEqual(state["flights"][0]["std_local"], "2026-07-02T16:00:00-04:00")
        self.assertEqual(state["flights"][0]["sta_local"], "2026-07-03T07:30:00+08:00")
        plan = self.make_plan(flight, "2026-07-02T20:00:00Z", "2026-07-02T23:30:00Z")
        assignment = self.svc.get_plan(plan["id"])["assignments"][0]
        self.assertEqual(assignment["origin_timezone"], "America/New_York")
        self.assertEqual(assignment["destination_timezone"], "Asia/Shanghai")
        self.assertEqual(assignment["new_sta_local"], "2026-07-03T07:30:00+08:00")


class TimezoneUpdateTest(TimezoneTestBase):
    def test_first_submitter_wins_optimistic_lock(self):
        updated = self.svc.update_airport_timezone("JFK", "ops", "ops_manager", {"timezone": "America/Chicago", "expected_revision": 1})
        self.assertEqual(updated["timezone"], "America/Chicago")
        self.assertEqual(updated["timezone_revision"], 2)
        with self.assertRaises(ApiError) as ctx:
            self.svc.update_airport_timezone("JFK", "ops", "ops_manager", {"timezone": "Asia/Shanghai", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "timezone_conflict")
        # 新值可以基于新版本号再次提交
        updated = self.svc.update_airport_timezone("JFK", "ops", "ops_manager", {"timezone": "Asia/Shanghai", "expected_revision": 2})
        self.assertEqual(updated["timezone_revision"], 3)

    def test_invalid_timezone_and_revision_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.update_airport_timezone("JFK", "ops", "ops_manager", {"timezone": "Not/AZone", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "invalid_timezone")
        with self.assertRaises(ApiError) as ctx:
            self.svc.update_airport_timezone("JFK", "ops", "ops_manager", {"timezone": "Asia/Shanghai"})
        self.assertEqual(ctx.exception.code, "revision_required")
        with self.assertRaises(ApiError) as ctx:
            self.svc.update_airport_timezone("JFK", "sched", "scheduler", {"timezone": "Asia/Shanghai", "expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)

    def test_draft_conclusions_invalidated_locked_keeps_snapshot(self):
        flight = self.make_flight("CA103", "2026-07-02T20:00:00Z", "2026-07-02T23:30:00Z")
        draft = self.make_plan(flight, "2026-07-02T20:00:00Z", "2026-07-02T23:30:00Z")
        check = self.svc.validate_plan(draft["id"], "auditor", "auditor")
        self.assertIsNotNone(check["plan"]["score"])
        # 时区更新后，未锁定方案的旧结论失效，展示改用当前时区
        self.svc.update_airport_timezone("PVG", "ops", "ops_manager", {"timezone": "Asia/Tokyo", "expected_revision": 1})
        stale = self.svc.get_plan(draft["id"])
        self.assertTrue(stale["validation_stale"])
        self.assertIsNone(stale["score"])
        self.assertEqual(stale["assignments"][0]["destination_timezone"], "Asia/Tokyo")
        self.assertEqual(stale["assignments"][0]["new_sta_local"], "2026-07-03T08:30:00+09:00")
        # 锁定方案按时区快照解释：之后再改时区，锁定方案仍用锁定当时的时区
        locked_plan = self.make_plan(flight, "2026-07-02T20:00:00Z", "2026-07-02T23:30:00Z")
        self.svc.lock_plan(locked_plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.svc.update_airport_timezone("PVG", "ops", "ops_manager", {"timezone": "Asia/Shanghai", "expected_revision": 2})
        locked = self.svc.get_plan(locked_plan["id"])
        self.assertEqual(locked["status"], "locked")
        self.assertFalse(locked["validation_stale"])
        self.assertEqual(locked["assignments"][0]["destination_timezone"], "Asia/Tokyo")
        self.assertEqual(locked["assignments"][0]["new_sta_local"], "2026-07-03T08:30:00+09:00")


class DstRuleTest(unittest.TestCase):
    def test_spring_forward_gap_shifts_to_after_gap(self):
        # 2026-03-08 02:30 美东当地不存在（春令时跳时），固定口径：顺延到跳时之后
        result = local_wall_to_utc(datetime(2026, 3, 8, 2, 30), "America/New_York")
        self.assertEqual(result, datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc))

    def test_fall_back_ambiguous_uses_first_occurrence(self):
        # 2026-11-01 01:30 美东当地出现两次，固定口径：取第一次（夏令时偏移）
        result = local_wall_to_utc(datetime(2026, 11, 1, 1, 30), "America/New_York")
        self.assertEqual(result, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))

    def test_utc_to_local_display(self):
        self.assertEqual(to_local_iso(datetime(2026, 7, 2, 23, 30, tzinfo=timezone.utc), "Asia/Shanghai"),
                         "2026-07-03T07:30:00+08:00")

    def test_unknown_timezone_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            local_wall_to_utc(datetime(2026, 1, 1, 0, 0), "Not/AZone")
        self.assertEqual(ctx.exception.code, "invalid_timezone")


class MigrationTest(unittest.TestCase):
    def _old_schema(self, path, add_timezone_column=False):
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE airports(code TEXT PRIMARY KEY, country TEXT NOT NULL, curfew_start TEXT NOT NULL, curfew_end TEXT NOT NULL);
            INSERT INTO airports VALUES('AAA','CN','23:00','05:00');
            CREATE TABLE recovery_plans(id INTEGER PRIMARY KEY AUTOINCREMENT, disruption_id INTEGER NOT NULL, name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1, score_json TEXT, metrics_json TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, locked_at TEXT, locked_by TEXT);
        """)
        if add_timezone_column:
            conn.execute("ALTER TABLE airports ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
        conn.close()

    def test_migration_fills_default_timezone_and_is_retryable(self):
        path = Path(self.tmp.name) / "old.db"
        self._old_schema(path)
        svc = AirlineRecoveryService(path)
        row = svc.repo.conn.execute("SELECT * FROM airports WHERE code='AAA'").fetchone()
        self.assertEqual(row["timezone"], "UTC")
        self.assertEqual(row["timezone_revision"], 1)
        self.assertIn("timezone_snapshot_json", svc.repo._columns("recovery_plans"))
        # 模拟迁移中途失败后重试：重复初始化必须幂等且可恢复
        svc.repo._init()
        svc.repo._init()
        row = svc.repo.conn.execute("SELECT * FROM airports WHERE code='AAA'").fetchone()
        self.assertEqual(row["timezone"], "UTC")

    def test_migration_resumes_after_partial_alter(self):
        path = Path(self.tmp.name) / "partial.db"
        self._old_schema(path, add_timezone_column=True)
        svc = AirlineRecoveryService(path)
        row = svc.repo.conn.execute("SELECT * FROM airports WHERE code='AAA'").fetchone()
        self.assertEqual(row["timezone"], "UTC")
        self.assertEqual(row["timezone_revision"], 1)
        self.assertIn("timezone_snapshot_json", svc.repo._columns("recovery_plans"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
