import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import (DEFAULT_TIMEZONE, ApiError, AirlineRecoveryService, Repository, get_zone,
                 in_curfew, iso, localize, to_local, wall_kind)


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TimezoneConversionTest(unittest.TestCase):
    def test_utc_to_local_with_offset_and_dst(self):
        london = get_zone("Europe/London")
        winter = to_local(utc(2026, 1, 15, 22, 30), london)
        summer = to_local(utc(2026, 6, 15, 22, 30), london)
        self.assertEqual(winter.strftime("%z"), "+0000")
        self.assertEqual(summer.strftime("%z"), "+0100")
        self.assertTrue(summer.dst())
        self.assertFalse(winter.dst())

    def test_gap_wall_time_is_skipped_forward(self):
        # 2026-03-29 伦敦 01:30 是春季空档（01:00->02:00）
        london = get_zone("Europe/London")
        wall = datetime(2026, 3, 29, 1, 30)
        aware, kind = localize(wall, london)
        self.assertEqual(kind, "gap")
        # 固定口径：顺延，落在 02:30 BST
        resolved = aware.astimezone(timezone.utc).astimezone(london)
        self.assertEqual(resolved.strftime("%H:%M"), "02:30")
        self.assertEqual(aware.astimezone(timezone.utc), utc(2026, 3, 29, 1, 30))

    def test_repeated_wall_time_takes_first_occurrence(self):
        # 2026-10-25 伦敦 01:30 出现两次；固定取第一次（BST, fold=0）
        london = get_zone("Europe/London")
        wall = datetime(2026, 10, 25, 1, 30)
        aware, kind = localize(wall, london)
        self.assertEqual(kind, "repeated")
        self.assertEqual(aware.strftime("%z"), "+0100")
        self.assertEqual(aware.astimezone(timezone.utc), utc(2026, 10, 25, 0, 30))
        # 第二次应是 01:30 GMT
        second = wall.replace(tzinfo=london, fold=1)
        self.assertEqual(second.astimezone(timezone.utc), utc(2026, 10, 25, 1, 30))

    def test_in_curfew_overnight_uses_local_clock(self):
        self.assertTrue(in_curfew(datetime(2026, 6, 15, 23, 30).timetz(), datetime(2026, 6, 15, 23, 0).timetz(), datetime(2026, 6, 16, 5, 0).timetz()))
        self.assertFalse(in_curfew(datetime(2026, 6, 15, 22, 59).timetz(), datetime(2026, 6, 15, 23, 0).timetz(), datetime(2026, 6, 16, 5, 0).timetz()))


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "old.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_database_gets_default_timezone_and_is_retryable(self):
        # 手工造一个老结构库（无机场时区列）
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE airports(code TEXT PRIMARY KEY, country TEXT NOT NULL, curfew_start TEXT NOT NULL, curfew_end TEXT NOT NULL)")
        conn.execute("INSERT INTO airports VALUES('OLD','CN','23:00','05:00')")
        conn.commit(); conn.close()
        svc = AirlineRecoveryService(self.db)
        airport = svc.repo.conn.execute("SELECT * FROM airports WHERE code='OLD'").fetchone()
        self.assertEqual(airport["timezone"], DEFAULT_TIMEZONE)
        versions = [r[0] for r in svc.repo.conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        self.assertEqual(versions, [1, 2])
        # 再开一次：已应用版本跳过，不重复、不报错
        AirlineRecoveryService(self.db)

    def test_migration_is_idempotent_when_partially_applied(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE airports(code TEXT PRIMARY KEY, country TEXT NOT NULL, curfew_start TEXT NOT NULL, curfew_end TEXT NOT NULL)")
        # 模拟上次迁移只加了 timezone 列就中断（版本未记录）
        conn.execute(f"ALTER TABLE airports ADD COLUMN timezone TEXT NOT NULL DEFAULT '{DEFAULT_TIMEZONE}'")
        conn.commit(); conn.close()
        svc = AirlineRecoveryService(self.db)
        cols = {r["name"] for r in svc.repo.conn.execute("PRAGMA table_info(airports)")}
        self.assertIn("tz_version", cols)
        self.assertEqual([r[0] for r in svc.repo.conn.execute("SELECT version FROM schema_migrations")], [1, 2])


class ServiceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AirlineRecoveryService(Path(self.tmp.name) / "tz.db")
        # LHR 宵禁 23:30-06:00 当地；PVG 不宵禁（1 分钟窗口）
        self.svc.seed_airport("ops", "ops_manager", {"code": "LHR", "country": "GB", "timezone": "Europe/London", "curfew_start": "23:30", "curfew_end": "06:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "PVG", "country": "CN", "timezone": "Asia/Shanghai", "curfew_start": "23:59", "curfew_end": "00:00"})
        self.base = utc(2026, 6, 15, 20, 0)
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A350", "maintenance_due": iso(self.base + timedelta(days=5))})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "PVG", "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "PVG", "destination": "LHR", "valid_from": iso(self.base - timedelta(days=1)), "valid_to": iso(self.base + timedelta(days=2))})

    def tearDown(self):
        self.tmp.cleanup()

    def flight(self, no="PV100"):
        return self.svc.create_flight("sched", "scheduler", {"flight_no": no, "origin": "PVG", "destination": "LHR",
            "std": iso(self.base), "sta": iso(self.base + timedelta(hours=2)), "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 200})

    def test_convert_api_both_directions(self):
        out = self.svc.convert_time({"airport": "LHR", "utc": iso(utc(2026, 6, 15, 22, 30))})
        self.assertEqual(out["local"][:19], "2026-06-15 23:30:00")
        self.assertEqual(out["utc_offset"], "+0100")
        back = self.svc.convert_time({"timezone": "Europe/London", "local": "2026-06-15T23:30"})
        self.assertEqual(back["utc"], iso(utc(2026, 6, 15, 22, 30)))

    def test_curfew_judged_in_local_time_not_utc(self):
        flight = self.flight()
        # UTC 22:30 到达 LHR -> 当地 23:30（BST），正好进宵禁；UTC 挂钟 22:30 本不在 23:30-06:00
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1",
            "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=1))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "延误到宵禁",
            "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC1", "crew_id": "CR1",
                             "new_std": iso(self.base + timedelta(minutes=30)), "new_sta": iso(utc(2026, 6, 15, 22, 30))}]})
        check = self.svc.validate_plan(plan["id"], "auditor", "auditor")
        self.assertFalse(check["valid"])
        problem = next(p for p in check["problems"] if p["code"] == "airport_curfew")
        self.assertEqual(problem["timezone"], "Europe/London")
        self.assertIn("23:30:00", problem["arrival_local"])
        # 提前到当地 23:00（UTC 22:00）则合法
        good = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "提前到宵禁前",
            "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC1", "crew_id": "CR1",
                             "new_std": iso(self.base), "new_sta": iso(utc(2026, 6, 15, 22, 0))}]})
        self.assertTrue(self.svc.validate_plan(good["id"], "auditor", "auditor")["valid"])

    def test_concurrent_timezone_update_only_first_wins(self):
        airport = self.svc.repo.conn.execute("SELECT * FROM airports WHERE code='LHR'").fetchone()
        self.assertEqual(airport["tz_version"], 0)
        results: list = []

        def submit(tz_name: str):
            try:
                row = self.svc.update_airport_timezone("ops", "ops_manager", "LHR",
                                                       {"timezone": tz_name, "expected_tz_version": 0})
                results.append(("ok", row["timezone"]))
            except ApiError as exc:
                results.append(("conflict", exc.code))

        t1 = threading.Thread(target=submit, args=("America/New_York",))
        t2 = threading.Thread(target=submit, args=("Asia/Tokyo",))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(conflicts), 1, results)
        self.assertEqual(conflicts[0][1], "timezone_conflict")
        stored = self.svc.repo.conn.execute("SELECT * FROM airports WHERE code='LHR'").fetchone()
        self.assertEqual(stored["tz_version"], 1)
        self.assertEqual(stored["timezone"], oks[0][1])
        # 旧版本再次提交仍被拒；带新版本可继续改
        with self.assertRaises(ApiError) as ctx:
            self.svc.update_airport_timezone("ops", "ops_manager", "LHR", {"timezone": "UTC", "expected_tz_version": 0})
        self.assertEqual(ctx.exception.code, "timezone_conflict")
        self.svc.update_airport_timezone("ops", "ops_manager", "LHR", {"timezone": "UTC", "expected_tz_version": 1})

    def test_stale_conclusion_invalidated_but_locked_plan_keeps_snapshot(self):
        flight = self.flight()
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1",
            "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=1))})
        # UTC 22:00 -> LHR 当地 23:00，宵禁前，校验通过并锁定
        draft = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "待执行方案",
            "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC1", "crew_id": "CR1",
                             "new_std": iso(self.base), "new_sta": iso(utc(2026, 6, 15, 22, 0))}]})
        self.svc.validate_plan(draft["id"], "auditor", "auditor")
        locked = self.svc.create_plan("sched2", "scheduler", {"disruption_id": disruption["id"], "name": "锁定方案",
            "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC1", "crew_id": "CR1",
                             "new_std": iso(self.base), "new_sta": iso(utc(2026, 6, 15, 22, 0))}]})
        self.svc.lock_plan(locked["id"], "ops", "ops_manager", {"expected_revision": 1})
        # 机场时区改为纽约：UTC 22:00 -> 纽约 18:00，UTC 23:30 -> 纽约 19:30（旧结论不再适用）
        self.svc.update_airport_timezone("ops", "ops_manager", "LHR", {"timezone": "America/New_York", "expected_tz_version": 0})
        refreshed = self.svc.get_plan(draft["id"])
        self.assertEqual(refreshed["conclusion_status"], "stale")
        self.assertIsNone(refreshed["score"])
        locked_view = self.svc.get_plan(locked["id"])
        self.assertEqual(locked_view["timezone_snapshot"]["LHR"]["timezone"], "Europe/London")
        self.assertEqual(locked_view["assignments"][0]["destination_timezone"], "Europe/London")
        self.assertIn("23:00:00", locked_view["assignments"][0]["destination_local"])
        # 锁定方案不允许重新校验；草稿方案重新校验即得到新结论
        with self.assertRaises(ApiError) as ctx:
            self.svc.validate_plan(locked["id"], "auditor", "auditor")
        self.assertEqual(ctx.exception.code, "plan_locked")
        again = self.svc.validate_plan(draft["id"], "auditor", "auditor")
        self.assertTrue(again["valid"], again["problems"])
        self.assertEqual(again["plan"]["conclusion_status"], "valid")

    def test_page_shows_local_times(self):
        self.flight()
        state = self.svc.state()
        row = state["flights"][0]
        self.assertEqual(row["origin_timezone"], "Asia/Shanghai")
        self.assertEqual(row["destination_timezone"], "Europe/London")
        # UTC 20:00 -> 上海 04:00 次日
        self.assertIn("2026-06-16 04:00:00", row["origin_local"])
        self.assertIn("2026-06-15 23:00:00", row["destination_local"])
        self.assertEqual(state["airports"][0]["code"], "LHR")


if __name__ == "__main__":
    unittest.main()
