#!/usr/bin/env python3
"""Airline disruption recovery engine using standard-library SQLite and HTTP."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

PORT = 8202
ROLES = {"viewer", "scheduler", "ops_manager", "auditor"}
# 历史数据没有时区列，迁移时统一补这个默认区
DEFAULT_TIMEZONE = (os.environ.get("AIRLINE_DEFAULT_TZ", "UTC").strip() or "UTC")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime:
    if not value:
        raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def parse_clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(400, "invalid_clock", f"时刻格式应为 HH:MM: {value}") from exc


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def get_zone(name: str) -> ZoneInfo:
    """校验 IANA 时区名（如 Asia/Shanghai、Europe/London）。"""
    name = (name or "").strip()
    if not name or len(name) > 64 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_+\-/:]*", name) or ".." in name:
        raise ApiError(400, "invalid_timezone", f"时区标识无效: {name}")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ApiError(400, "invalid_timezone", f"未知 IANA 时区: {name}") from exc


def to_local(moment: datetime, zone: ZoneInfo) -> datetime:
    """UTC 时刻 -> 当地挂钟时间（携带该刻实际偏移，含 DST）。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone)


def wall_kind(wall: datetime, zone: ZoneInfo) -> str:
    """判断当地挂钟时间的类型：normal / gap（夏令时空档）/ repeated（重复小时）。"""
    first = wall.replace(fold=0).utcoffset()
    second = wall.replace(fold=1).utcoffset()
    if first == second:
        return "normal"
    if second - first > timedelta(0):
        return "gap"  # 春季跳表后不存在的小时
    return "repeated"  # 秋季回表重复出现的小时


def localize(wall: datetime, zone: ZoneInfo) -> tuple[datetime, str]:
    """把没有时区的当地挂钟按固定口径落到 UTC：
    gap（空档）：按 PEP 495 fold=0 顺延到跳表之后；
    repeated（重复小时）：取第一次出现（夏令时偏移，fold=0）。
    返回（当地 datetime，kind）。
    """
    aware = wall.replace(tzinfo=zone)
    kind = wall_kind(aware, zone)
    # fold=0：空档时 utcoffset 取跳表前偏移，相减后正好落在空档之后；重复时取夏令时那次
    return aware, kind


def local_str(wall: datetime) -> str:
    return wall.strftime("%Y-%m-%d %H:%M:%S %Z%z")


def in_curfew(local_clock: time, curfew_start: time, curfew_end: time) -> bool:
    """宵禁按当地挂钟时刻判定；start==end 视为全天宵禁，空宵禁窗口不限制。"""
    if curfew_start == curfew_end:
        return True
    if curfew_start > curfew_end:  # 跨午夜，如 23:00–05:00
        return local_clock >= curfew_start or local_clock < curfew_end
    return curfew_start <= local_clock < curfew_end


class Repository:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._write_lock = threading.Lock()
        self._init()

    @contextmanager
    def tx(self):
        # 单连接多线程下串行化写事务，使 BEGIN IMMEDIATE 的首写获胜语义在进程内成立
        self._write_lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._write_lock.release()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS airports(
                code TEXT PRIMARY KEY, country TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'UTC',
                curfew_start TEXT NOT NULL, curfew_end TEXT NOT NULL,
                tz_version INTEGER NOT NULL DEFAULT 0, tz_updated_at TEXT, tz_updated_by TEXT);
            CREATE TABLE IF NOT EXISTS aircraft(id TEXT PRIMARY KEY, model TEXT NOT NULL, maintenance_due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS crew(id TEXT PRIMARY KEY, name TEXT NOT NULL, base TEXT NOT NULL, duty_start TEXT NOT NULL, max_duty_minutes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS permits(id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL, destination TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, curfew_exempt INTEGER NOT NULL DEFAULT 0, UNIQUE(origin,destination,valid_from,valid_to));
            CREATE TABLE IF NOT EXISTS flights(
                id INTEGER PRIMARY KEY AUTOINCREMENT, flight_no TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, destination TEXT NOT NULL,
                std TEXT NOT NULL, sta TEXT NOT NULL, aircraft_id TEXT NOT NULL REFERENCES aircraft(id), crew_id TEXT NOT NULL REFERENCES crew(id),
                passenger_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'scheduled', delay_minutes INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, cancel_reason TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS disruptions(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, resource_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery_plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, disruption_id INTEGER NOT NULL REFERENCES disruptions(id), name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1, score_json TEXT, metrics_json TEXT,
                conclusion_status TEXT, timezone_snapshot_json TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, locked_at TEXT, locked_by TEXT
            );
            CREATE TABLE IF NOT EXISTS assignments(
                id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES recovery_plans(id) ON DELETE CASCADE,
                flight_id INTEGER NOT NULL REFERENCES flights(id), aircraft_id TEXT NOT NULL REFERENCES aircraft(id), crew_id TEXT NOT NULL REFERENCES crew(id),
                new_std TEXT NOT NULL, new_sta TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned', delay_minutes INTEGER NOT NULL DEFAULT 0,
                missed_connections INTEGER NOT NULL DEFAULT 0, UNIQUE(plan_id,flight_id)
            );
            CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            """
        )
        self._migrate()

    def _migrate(self) -> None:
        """版本化迁移：每步幂等并记录在 schema_migrations。
        老库的机场数据缺时区，补默认区 DEFAULT_TIMEZONE；中途失败只留下已加好的列，
        再次启动会跳过已完成的列并补完剩余部分，然后才记版本，可安全重试。"""
        migrations = [
            (1, "airports", [
                f"ALTER TABLE airports ADD COLUMN timezone TEXT NOT NULL DEFAULT '{DEFAULT_TIMEZONE}'",
                "ALTER TABLE airports ADD COLUMN tz_version INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE airports ADD COLUMN tz_updated_at TEXT",
                "ALTER TABLE airports ADD COLUMN tz_updated_by TEXT",
            ]),
            (2, "recovery_plans", [
                "ALTER TABLE recovery_plans ADD COLUMN conclusion_status TEXT",
                "ALTER TABLE recovery_plans ADD COLUMN timezone_snapshot_json TEXT",
            ]),
        ]
        applied = {r["version"] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        for version, table, statements in migrations:
            if version in applied:
                continue
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._columns(table)
                for stmt in statements:
                    column = stmt.split("ADD COLUMN", 1)[1].split()[0]
                    if column not in existing:
                        self.conn.execute(stmt)
                self.conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, iso()))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))


class AirlineRecoveryService:
    def __init__(self, db_path: str | Path):
        self.repo = Repository(db_path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str]:
        actor, role = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip()
        if not actor or role not in ROLES:
            raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        return actor, role

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def seed_airport(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager":
            raise ApiError(403, "seed_forbidden", "只有运行经理可以维护机场数据")
        code = str(body.get("code", "")).upper().strip()
        country = str(body.get("country", "")).upper().strip()
        if not code or not country:
            raise ApiError(400, "missing_fields", "code 和 country 必填")
        start = str(body.get("curfew_start", "23:00")); end = str(body.get("curfew_end", "06:00"))
        parse_clock(start); parse_clock(end)
        tz_name = str(body.get("timezone", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE).strip()
        get_zone(tz_name)  # 校验 IANA 时区
        with self.repo.tx() as conn:
            # 已存在的机场：改宵禁/国家不动其时区；时区只在首次创建时写入，避免覆盖别人的更新
            conn.execute("""INSERT INTO airports(code,country,timezone,curfew_start,curfew_end) VALUES(?,?,?,?,?)
                            ON CONFLICT(code) DO UPDATE SET
                              country=excluded.country, curfew_start=excluded.curfew_start, curfew_end=excluded.curfew_end""",
                         (code, country, tz_name, start, end))
            return dict(conn.execute("SELECT * FROM airports WHERE code=?", (code,)).fetchone())

    def update_airport_timezone(self, actor: str, role: str, code: str, body: dict[str, Any]) -> dict[str, Any]:
        """两人同时提交同一机场时区时，只有先拿到写事务（先到）的一方写入；
        后到者凭 expected_tz_version 得到 409。"""
        if role != "ops_manager":
            raise ApiError(403, "timezone_forbidden", "只有运行经理可以修改机场时区")
        code = code.upper().strip()
        tz_name = str(body.get("timezone", "")).strip()
        expected = body.get("expected_tz_version")
        if not isinstance(expected, int):
            raise ApiError(400, "version_required", "expected_tz_version 必须是整数")
        get_zone(tz_name)
        with self.repo.tx() as conn:
            airport = conn.execute("SELECT * FROM airports WHERE code=?", (code,)).fetchone()
            if not airport:
                raise ApiError(404, "airport_not_found", "机场不存在")
            if airport["tz_version"] != expected:
                raise ApiError(409, "timezone_conflict",
                               "机场时区已被先提交的人更新，请刷新后重试",
                               {"current_timezone": airport["timezone"], "current_tz_version": airport["tz_version"]})
            if airport["timezone"] == tz_name:
                return dict(airport)  # 幂等：相同值不改版本
            conn.execute("UPDATE airports SET timezone=?,tz_version=tz_version+1,tz_updated_at=?,tz_updated_by=? WHERE code=?",
                         (tz_name, iso(), actor, code))
            # 未执行（草稿）方案的旧校验结论失效；已锁定方案不动，按锁定当时的时区快照解释
            stale = [r["id"] for r in conn.execute(
                """SELECT p.id FROM recovery_plans p JOIN assignments a ON a.plan_id=p.id JOIN flights f ON f.id=a.flight_id
                   WHERE p.status='draft' AND (f.origin=? OR f.destination=?)""", (code, code))]
            if stale:
                conn.execute("UPDATE recovery_plans SET conclusion_status='stale',score_json=NULL,metrics_json=NULL WHERE id IN (%s)"
                             % ",".join("?" * len(stale)), stale)
            for plan_id in stale:
                Repository.audit(conn, plan_id, actor, role, "timezone_invalidated",
                                 {"airport": code, "old_timezone": airport["timezone"], "new_timezone": tz_name})
            Repository.audit(conn, None, actor, role, "airport_timezone_updated",
                             {"airport": code, "old_timezone": airport["timezone"], "new_timezone": tz_name,
                              "tz_version": expected + 1, "invalidated_plans": stale})
            return dict(conn.execute("SELECT * FROM airports WHERE code=?", (code,)).fetchone())

    def _curfew_snapshot(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """锁定瞬间相关机场的时区与宵禁快照，供锁定方案按当时时区解释。"""
        snapshot: dict[str, Any] = {}
        for row in conn.execute("SELECT code,timezone,curfew_start,curfew_end,tz_version FROM airports"):
            snapshot[row["code"]] = {"timezone": row["timezone"], "curfew_start": row["curfew_start"],
                                     "curfew_end": row["curfew_end"], "tz_version": row["tz_version"]}
        return snapshot

    def convert_time(self, body: dict[str, Any]) -> dict[str, Any]:
        """UTC <-> 当地挂钟换算。固定口径：
        gap（夏令时空档）顺延到跳表后；repeated（重复小时）取第一次（fold=0）。"""
        tz_name = str(body.get("timezone", "")).strip()
        code = str(body.get("airport", "")).upper().strip()
        with self._readonly() as conn:
            zone = get_zone(tz_name) if tz_name else None
            airport = None
            if code:
                airport = conn.execute("SELECT * FROM airports WHERE code=?", (code,)).fetchone()
                if not airport:
                    raise ApiError(404, "airport_not_found", "机场不存在")
                zone = get_zone(airport["timezone"])
        if zone is None:
            raise ApiError(400, "timezone_required", "必须提供 timezone 或 airport")
        if body.get("utc") is not None:
            moment = parse_time(body["utc"])
            local = to_local(moment, zone)
            return {"timezone": zone.key, "utc": iso(moment), "local": local_str(local),
                    "wall_clock": local.strftime("%H:%M"), "dst": bool(local.dst()),
                    "utc_offset": local.strftime("%z"), "wall_kind": wall_kind(local, zone)}
        wall_value = body.get("local")
        if wall_value:
            try:
                wall = datetime.fromisoformat(str(wall_value))
            except ValueError as exc:
                raise ApiError(400, "invalid_local", f"当地时间格式错误: {wall_value}") from exc
            if wall.tzinfo is not None:
                raise ApiError(400, "invalid_local", "local 应为不带偏移的当地挂钟时间")
            aware, kind = localize(wall, zone)
            utc_value = aware.astimezone(timezone.utc)
            if kind == "gap":  # 空档顺延
                resolved = utc_value.astimezone(zone)
            else:
                resolved = aware
            return {"timezone": zone.key, "local": local_str(resolved), "utc": iso(utc_value),
                    "dst": bool(resolved.dst()), "utc_offset": resolved.strftime("%z"),
                    "wall_kind": kind,
                    "policy": "gap 顺延跳表后" if kind == "gap" else ("重复小时取第一次（fold=0）" if kind == "repeated" else None)}
        raise ApiError(400, "time_required", "必须提供 utc 或 local")

    @contextmanager
    def _readonly(self) -> Iterator[sqlite3.Connection]:
        self.repo._write_lock.acquire()
        try:
            yield self.repo.conn
        finally:
            self.repo._write_lock.release()

    def seed_aircraft(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "seed_forbidden", "只有运行经理可以维护飞机")
        ident, model = str(body.get("id", "")).strip(), str(body.get("model", "")).strip()
        if not ident or not model: raise ApiError(400, "missing_fields", "id 和 model 必填")
        due = iso(parse_time(body.get("maintenance_due")))
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO aircraft(id,model,maintenance_due,status) VALUES(?,?,?,?)", (ident, model, due, body.get("status", "active")))
            return dict(conn.execute("SELECT * FROM aircraft WHERE id=?", (ident,)).fetchone())

    def seed_crew(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "seed_forbidden", "只有运行经理可以维护机组")
        ident, name, base = str(body.get("id", "")).strip(), str(body.get("name", "")).strip(), str(body.get("base", "")).upper().strip()
        duty = parse_time(body.get("duty_start")); maximum = body.get("max_duty_minutes")
        if not ident or not name or not base or not isinstance(maximum, int) or maximum <= 0:
            raise ApiError(400, "invalid_crew", "id、name、base 和正整数 max_duty_minutes 必填")
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO crew(id,name,base,duty_start,max_duty_minutes,status) VALUES(?,?,?,?,?,?)", (ident, name, base, iso(duty), maximum, body.get("status", "active")))
            return dict(conn.execute("SELECT * FROM crew WHERE id=?", (ident,)).fetchone())

    def create_permit(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "permit_forbidden", "只有运行经理可以维护航线许可")
        origin, destination = str(body.get("origin", "")).upper(), str(body.get("destination", "")).upper()
        if not origin or not destination: raise ApiError(400, "missing_fields", "origin 和 destination 必填")
        valid_from, valid_to = parse_time(body.get("valid_from")), parse_time(body.get("valid_to"))
        if valid_to <= valid_from: raise ApiError(400, "invalid_permit", "许可结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("INSERT INTO permits(origin,destination,valid_from,valid_to,curfew_exempt) VALUES(?,?,?,?,?)",
                                   (origin, destination, iso(valid_from), iso(valid_to), int(bool(body.get("curfew_exempt")))))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "permit_exists", "相同航线与有效期的许可已存在") from exc
            return dict(conn.execute("SELECT * FROM permits WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_flight(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "flight_forbidden", "当前角色不能创建航班")
        required = ("flight_no", "origin", "destination", "std", "sta", "aircraft_id", "crew_id")
        if any(not body.get(k) for k in required): raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(k for k in required if not body.get(k))}")
        std, sta = parse_time(body["std"]), parse_time(body["sta"])
        if sta <= std: raise ApiError(400, "invalid_times", "到达时间必须晚于起飞时间")
        passengers = body.get("passenger_count", 0)
        if not isinstance(passengers, int) or passengers < 0: raise ApiError(400, "invalid_passengers", "passenger_count 必须是非负整数")
        with self.repo.tx() as conn:
            for table, ident in (("aircraft", body["aircraft_id"]), ("crew", body["crew_id"])):
                row = conn.execute(f"SELECT status FROM {table} WHERE id=?", (ident,)).fetchone()
                if not row or row["status"] != "active": raise ApiError(409, "resource_unavailable", f"{table} {ident} 不可用")
            try:
                cur = conn.execute("""INSERT INTO flights(flight_no,origin,destination,std,sta,aircraft_id,crew_id,passenger_count,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?)""",
                                   (body["flight_no"].upper(), body["origin"].upper(), body["destination"].upper(), iso(std), iso(sta),
                                    body["aircraft_id"], body["crew_id"], passengers, iso()))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "flight_exists", "航班号已存在") from exc
            return dict(conn.execute("SELECT * FROM flights WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_disruption(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "disruption_forbidden", "当前角色不能登记中断")
        kind, resource = str(body.get("kind", "")).strip(), str(body.get("resource_id", "")).strip()
        if kind not in {"airport_closure", "aircraft_fault", "crew_timeout"} or not resource:
            raise ApiError(400, "invalid_disruption", "kind 或 resource_id 无效")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if end <= start: raise ApiError(400, "invalid_times", "中断结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            cur = conn.execute("INSERT INTO disruptions(kind,resource_id,starts_at,ends_at,created_at) VALUES(?,?,?,?,?)",
                               (kind, resource.upper() if kind == "airport_closure" else resource, iso(start), iso(end), iso()))
            return dict(conn.execute("SELECT * FROM disruptions WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_plan(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "plan_forbidden", "当前角色不能创建恢复方案")
        disruption_id, name = body.get("disruption_id"), str(body.get("name", "")).strip()
        assignments = body.get("assignments", [])
        if not isinstance(disruption_id, int) or not name or not isinstance(assignments, list):
            raise ApiError(400, "invalid_plan", "disruption_id、name 和 assignments 必填")
        with self.repo.tx() as conn:
            if not conn.execute("SELECT 1 FROM disruptions WHERE id=?", (disruption_id,)).fetchone():
                raise ApiError(404, "disruption_not_found", "中断事件不存在")
            cur = conn.execute("INSERT INTO recovery_plans(disruption_id,name,created_by,created_at) VALUES(?,?,?,?)", (disruption_id, name, actor, iso()))
            plan_id = cur.lastrowid
            for item in assignments:
                self._insert_assignment(conn, plan_id, item, replace=False)
            Repository.audit(conn, plan_id, actor, role, "plan_created", {"disruption_id": disruption_id, "assignment_count": len(assignments)})
            return self.get_plan(plan_id)

    def _insert_assignment(self, conn: sqlite3.Connection, plan_id: int, item: dict[str, Any], replace: bool) -> None:
        required = ("flight_id", "aircraft_id", "crew_id", "new_std", "new_sta")
        if any(item.get(k) in (None, "") for k in required): raise ApiError(400, "invalid_assignment", f"飞行调整缺少字段: {', '.join(k for k in required if item.get(k) in (None, ''))}")
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        if plan["status"] != "draft": raise ApiError(409, "plan_locked", "已锁定方案不能修改")
        std, sta = parse_time(item["new_std"]), parse_time(item["new_sta"])
        if sta <= std: raise ApiError(400, "invalid_times", "新到达时间必须晚于新起飞时间")
        flight = conn.execute("SELECT * FROM flights WHERE id=?", (item["flight_id"],)).fetchone()
        if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
        if flight["status"] == "canceled" and item.get("status", "planned") != "canceled":
            raise ApiError(409, "canceled_flight", "已取消航班不能安排执行")
        delay = int((std - parse_time(flight["std"])).total_seconds() // 60)
        missed = int(item.get("missed_connections", 0))
        if missed < 0: raise ApiError(400, "invalid_connections", "missed_connections 不能为负")
        try:
            if replace:
                conn.execute("""UPDATE assignments SET aircraft_id=?,crew_id=?,new_std=?,new_sta=?,status=?,delay_minutes=?,missed_connections=?
                                WHERE plan_id=? AND flight_id=?""",
                             (item["aircraft_id"], item["crew_id"], iso(std), iso(sta), item.get("status", "planned"), delay, missed, plan_id, item["flight_id"]))
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise KeyError
            else:
                conn.execute("""INSERT INTO assignments(plan_id,flight_id,aircraft_id,crew_id,new_std,new_sta,status,delay_minutes,missed_connections)
                                VALUES(?,?,?,?,?,?,?,?,?)""",
                             (plan_id, item["flight_id"], item["aircraft_id"], item["crew_id"], iso(std), iso(sta), item.get("status", "planned"), delay, missed))
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "assignment_conflict", "方案中该航班已存在或资源无效") from exc
        except KeyError as exc:
            raise ApiError(404, "assignment_not_found", "待替换的航班调整不存在") from exc

    def add_assignment(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "assignment_forbidden", "当前角色不能修改方案")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] != "draft": raise ApiError(409, "plan_locked", "已锁定方案不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "方案已被其他人更新")
            self._insert_assignment(conn, plan_id, body, replace=True)
            conn.execute("UPDATE recovery_plans SET revision=revision+1,conclusion_status=NULL,score_json=NULL,metrics_json=NULL WHERE id=?", (plan_id,))
            Repository.audit(conn, plan_id, actor, role, "assignment_reassigned", {"flight_id": body.get("flight_id")})
            return self.get_plan(plan_id)

    def _validate_plan(self, conn: sqlite3.Connection, plan_id: int) -> list[dict[str, Any]]:
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        # 已锁定方案按锁定当时的时区/宵禁快照解释；草稿方案按当前机场数据校验
        snapshot = json.loads(plan["timezone_snapshot_json"]) if plan["timezone_snapshot_json"] else None
        rows = [dict(r) for r in conn.execute("""SELECT a.*, f.flight_no, f.origin, f.destination, f.passenger_count, f.status flight_status
                                                  FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? ORDER BY a.new_std""", (plan_id,))]
        if not rows: raise ApiError(409, "empty_plan", "方案没有飞行调整")
        problems: list[dict[str, Any]] = []
        by_aircraft: dict[str, list[dict[str, Any]]] = {}
        by_crew: dict[str, list[dict[str, Any]]] = {}
        airport_cache: dict[str, sqlite3.Row | dict[str, Any] | None] = {}

        def effective_airport(code: str):
            if code in airport_cache:
                return airport_cache[code]
            airport = snapshot[code] if snapshot and code in snapshot else conn.execute("SELECT * FROM airports WHERE code=?", (code,)).fetchone()
            airport_cache[code] = airport
            return airport
        for row in rows:
            if row["status"] == "canceled": continue
            std, sta = parse_time(row["new_std"]), parse_time(row["new_sta"])
            aircraft = conn.execute("SELECT * FROM aircraft WHERE id=?", (row["aircraft_id"],)).fetchone()
            crew = conn.execute("SELECT * FROM crew WHERE id=?", (row["crew_id"],)).fetchone()
            origin = effective_airport(row["origin"])
            destination = effective_airport(row["destination"])
            if not aircraft or aircraft["status"] != "active": problems.append({"assignment_id": row["id"], "code": "aircraft_unavailable"})
            elif parse_time(aircraft["maintenance_due"]) < sta: problems.append({"assignment_id": row["id"], "code": "maintenance_due", "resource": aircraft["id"]})
            if not crew or crew["status"] != "active": problems.append({"assignment_id": row["id"], "code": "crew_unavailable"})
            if not origin or not destination: problems.append({"assignment_id": row["id"], "code": "airport_unknown"})
            if crew:
                duty_start, max_duty = parse_time(crew["duty_start"]), crew["max_duty_minutes"]
                if (sta - duty_start).total_seconds() / 60 > max_duty: problems.append({"assignment_id": row["id"], "code": "duty_limit", "resource": crew["id"]})
            if destination:
                curfew_start, curfew_end = parse_clock(destination["curfew_start"]), parse_clock(destination["curfew_end"])
                permit = conn.execute("""SELECT * FROM permits WHERE origin=? AND destination=? AND valid_from<=? AND valid_to>=?""",
                                      (row["origin"], row["destination"], row["new_sta"], row["new_sta"])).fetchone()
                dest_zone = get_zone(destination["timezone"])
                arrival_local = to_local(sta, dest_zone)  # UTC 到达时刻 -> 目的地当地时间
                if in_curfew(arrival_local.timetz().replace(tzinfo=None), curfew_start, curfew_end) and not (permit and permit["curfew_exempt"]):
                    problems.append({"assignment_id": row["id"], "code": "airport_curfew",
                                     "timezone": destination["timezone"], "arrival_local": local_str(arrival_local),
                                     "curfew_window": f"{curfew_start.strftime('%H:%M')}-{curfew_end.strftime('%H:%M')} 当地时间"})
                if row["origin"] != row["destination"] and not permit: problems.append({"assignment_id": row["id"], "code": "route_permit_missing"})
                elif row["origin"] != row["destination"] and not (parse_time(permit["valid_from"]) <= std <= parse_time(permit["valid_to"])):
                    problems.append({"assignment_id": row["id"], "code": "route_permit_window"})
            by_aircraft.setdefault(row["aircraft_id"], []).append(row)
            by_crew.setdefault(row["crew_id"], []).append(row)
        for bucket_name, buckets in (("aircraft", by_aircraft), ("crew", by_crew)):
            for resource, items in buckets.items():
                for i, left in enumerate(items):
                    for right in items[i + 1:]:
                        if overlaps(parse_time(left["new_std"]), parse_time(left["new_sta"]), parse_time(right["new_std"]), parse_time(right["new_sta"])):
                            problems.append({"code": f"{bucket_name}_overlap", "resource": resource, "assignments": [left["id"], right["id"]]})
        return problems

    def validate_plan(self, plan_id: int, actor: str, role: str) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager", "auditor"}: raise ApiError(403, "validate_forbidden", "当前角色不能校验方案")
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] == "locked": raise ApiError(409, "plan_locked", "已锁定方案的结论按锁定当时时区解释，不能重新校验")
            problems = self._validate_plan(conn, plan_id)
            if problems:
                conn.execute("UPDATE recovery_plans SET conclusion_status='invalid',score_json=NULL,metrics_json=NULL WHERE id=?", (plan_id,))
            else:
                metrics = self._metrics(conn, plan_id)
                conn.execute("UPDATE recovery_plans SET conclusion_status='valid',metrics_json=?,score_json=? WHERE id=?",
                             (json.dumps(metrics, ensure_ascii=False), json.dumps(self._score(metrics)), plan_id))
            return {"valid": not problems, "problems": problems, "plan": self.get_plan(plan_id)}

    def _metrics(self, conn: sqlite3.Connection, plan_id: int) -> dict[str, Any]:
        rows = conn.execute("""SELECT a.*,f.passenger_count FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=?""", (plan_id,)).fetchall()
        canceled = sum(1 for row in rows if row["status"] == "canceled")
        return {"flight_count": len(rows), "canceled": canceled, "total_delay_minutes": sum(max(0, row["delay_minutes"]) for row in rows),
                "affected_passengers": sum(row["passenger_count"] for row in rows), "missed_connections": sum(row["missed_connections"] for row in rows)}

    @staticmethod
    def _score(metrics: dict[str, Any]) -> dict[str, int]:
        score = metrics["canceled"] * 100000 + metrics["missed_connections"] * 5000 + metrics["total_delay_minutes"] * 100 + metrics["affected_passengers"]
        return {"cost_score": score, "lower_is_better": 1}

    def lock_plan(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "lock_forbidden", "只有运行经理可以锁定恢复方案")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] == "locked": return self.get_plan(plan_id)
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "方案版本已变化")
            problems = self._validate_plan(conn, plan_id)
            if problems: raise ApiError(409, "plan_invalid", "方案未通过约束校验", problems)
            conflicts = []
            for row in conn.execute("SELECT * FROM assignments WHERE plan_id=? AND status!='canceled'", (plan_id,)).fetchall():
                conflicting = conn.execute("""SELECT a.*,p.name plan_name FROM assignments a JOIN recovery_plans p ON p.id=a.plan_id
                    WHERE p.id!=? AND p.status='locked' AND a.status!='canceled' AND (a.aircraft_id=? OR a.crew_id=?)
                    AND a.new_std<? AND a.new_sta>?""",
                    (plan_id, row["aircraft_id"], row["crew_id"], row["new_sta"], row["new_std"])).fetchall()
                conflicts.extend({"assignment_id": row["id"], "conflict_plan_id": item["plan_id"], "conflict_plan": item["plan_name"], "resource": item["aircraft_id"] if item["aircraft_id"] == row["aircraft_id"] else item["crew_id"]} for item in conflicting)
            if conflicts: raise ApiError(409, "locked_resource_conflict", "与已锁定方案存在飞机或机组冲突", conflicts)
            metrics = self._metrics(conn, plan_id)
            snapshot = self._curfew_snapshot(conn)
            conn.execute("""UPDATE recovery_plans SET status='locked',conclusion_status='valid',timezone_snapshot_json=?,metrics_json=?,score_json=?,locked_at=?,locked_by=? WHERE id=?""",
                         (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), json.dumps(metrics, ensure_ascii=False), json.dumps(self._score(metrics)), iso(), actor, plan_id))
            for row in conn.execute("""SELECT a.*,f.flight_no FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? AND a.status!='canceled'""", (plan_id,)):
                conn.execute("UPDATE flights SET std=?,sta=?,aircraft_id=?,crew_id=?,delay_minutes=?,revision=revision+1,updated_at=? WHERE id=?",
                             (row["new_std"], row["new_sta"], row["aircraft_id"], row["crew_id"], max(0, row["delay_minutes"]), iso(), row["flight_id"]))
                conn.execute("UPDATE assignments SET status='active' WHERE id=?", (row["id"],))
            Repository.audit(conn, plan_id, actor, role, "plan_locked", {"metrics": metrics})
            return self.get_plan(plan_id)

    def cancel_flight(self, flight_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消航班")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            flight = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
            if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
            if flight["status"] == "canceled": return {"flight": dict(flight), "idempotent": True}
            conn.execute("UPDATE flights SET status='canceled',cancel_reason=?,revision=revision+1,updated_at=? WHERE id=?", (reason, iso(), flight_id))
            Repository.audit(conn, None, actor, role, "flight_canceled", {"flight_id": flight_id, "reason": reason})
            return {"flight": dict(conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()), "idempotent": False}

    def recover_flight(self, flight_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "recover_forbidden", "当前角色不能恢复航班")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            flight = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
            if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
            if flight["revision"] != expected: raise ApiError(409, "revision_conflict", "航班版本已变化")
            if flight["status"] != "canceled": raise ApiError(409, "not_canceled", "只有取消航班可以恢复")
            std, sta = parse_time(body.get("new_std")), parse_time(body.get("new_sta"))
            if sta <= std: raise ApiError(400, "invalid_times", "到达时间必须晚于起飞时间")
            aircraft_id, crew_id = body.get("aircraft_id", flight["aircraft_id"]), body.get("crew_id", flight["crew_id"])
            conn.execute("""UPDATE flights SET status='scheduled',std=?,sta=?,aircraft_id=?,crew_id=?,cancel_reason=NULL,
                            delay_minutes=0,revision=revision+1,updated_at=? WHERE id=?""",
                         (iso(std), iso(sta), aircraft_id, crew_id, iso(), flight_id))
            Repository.audit(conn, None, actor, role, "flight_recovered", {"flight_id": flight_id})
            return {"flight": dict(conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone())}

    def _plan_airport(self, plan: sqlite3.Row | dict[str, Any], code: str, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """方案里某机场所用的时区与宵禁：锁定方案取快照，草稿取当前。"""
        snapshot = json.loads(plan["timezone_snapshot_json"]) if plan["timezone_snapshot_json"] else None
        if snapshot and code in snapshot:
            return snapshot[code]
        row = conn.execute("SELECT * FROM airports WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        conn = self.repo.conn
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        assignments = [dict(r) for r in conn.execute("""SELECT a.*,f.flight_no,f.origin,f.destination,f.passenger_count FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? ORDER BY a.new_std""", (plan_id,))]
        for item in assignments:
            # 页面要显示当地时间：按方案有效时区（锁定=快照，草稿=当前）把 UTC 调整时刻换算出来
            for side, column, utc_column in (("origin", item["origin"], "new_std"), ("destination", item["destination"], "new_sta")):
                airport = self._plan_airport(plan, column, conn)
                if airport:
                    local = to_local(parse_time(item[utc_column]), get_zone(airport["timezone"]))
                    item[f"{side}_timezone"] = airport["timezone"]
                    item[f"{side}_local"] = local_str(local)
                    item[f"{side}_dst"] = bool(local.dst())
        result = dict(plan)
        result["metrics"] = json.loads(plan["metrics_json"]) if plan["metrics_json"] else self._metrics(conn, plan_id)
        result["score"] = json.loads(plan["score_json"]) if plan["score_json"] else None
        result["timezone_snapshot"] = json.loads(plan["timezone_snapshot_json"]) if plan["timezone_snapshot_json"] else None
        result["assignments"] = assignments
        return result

    def compare_plans(self, disruption_id: int) -> dict[str, Any]:
        plans = []
        for row in self.repo.conn.execute("SELECT id FROM recovery_plans WHERE disruption_id=? ORDER BY id", (disruption_id,)):
            plan = self.get_plan(row["id"])
            # 以结论状态为准：时区更新后被置 stale 的旧结论必须重新校验，不能当作可用方案
            if plan["conclusion_status"] == "valid":
                plan["valid"] = True
            else:
                problems = self._validate_plan(self.repo.conn, row["id"])
                plan["valid"] = not problems
            plans.append(plan)
        plans.sort(key=lambda item: item["score"]["cost_score"] if item["valid"] and item["score"] else 10**18)
        recommended = next((p["id"] for p in plans if p["valid"]), None)
        return {"disruption_id": disruption_id, "recommended_plan_id": recommended, "plans": plans}

    def state(self) -> dict[str, Any]:
        conn = self.repo.conn
        airports = []
        airport_zones: dict[str, ZoneInfo] = {}
        for row in conn.execute("SELECT * FROM airports ORDER BY code"):
            data = dict(row)
            airport_zones[row["code"]] = get_zone(row["timezone"])
            airports.append(data)
        now = utcnow()
        flights = []
        for r in conn.execute("SELECT * FROM flights ORDER BY std"):
            data = dict(r)
            # 页面显示每个起降点的当地时间
            for side, code_column, utc_column in (("origin", "origin", "std"), ("destination", "destination", "sta")):
                zone = airport_zones.get(data[code_column])
                if zone:
                    local = to_local(parse_time(data[utc_column]), zone)
                    data[f"{side}_timezone"] = zone.key
                    data[f"{side}_local"] = local_str(local)
            flights.append(data)
        plans = [self.get_plan(r["id"]) for r in conn.execute("SELECT id FROM recovery_plans ORDER BY id DESC LIMIT 20")]
        return {"airports": airports, "flights": flights,
                "disruptions": [dict(r) for r in conn.execute("SELECT * FROM disruptions ORDER BY id DESC")],
                "plans": plans, "server_time": iso(), "server_local": local_str(to_local(now, ZoneInfo(DEFAULT_TIMEZONE)))}


def respond(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode()
    handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: AirlineRecoveryService
    web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length: return {}
        try: value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "airline-recovery"}
        actor, role = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state()
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "disruptions"] and parts[2].isdigit() and parts[3] == "compare": return 200, self.service.compare_plans(int(parts[2]))
        if path == "/api/time-convert":
            query = parse_qs(urlparse(self.path).query)
            body = {k: v[0] for k, v in query.items()}
            return 200, self.service.convert_time(body)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        table = {
            "/api/airports": lambda: (201, self.service.seed_airport(actor, role, body)),
            "/api/aircraft": lambda: (201, self.service.seed_aircraft(actor, role, body)),
            "/api/crew": lambda: (201, self.service.seed_crew(actor, role, body)),
            "/api/permits": lambda: (201, self.service.create_permit(actor, role, body)),
            "/api/flights": lambda: (201, self.service.create_flight(actor, role, body)),
            "/api/disruptions": lambda: (201, self.service.create_disruption(actor, role, body)),
            "/api/recovery-plans": lambda: (201, self.service.create_plan(actor, role, body)),
            "/api/time-convert": lambda: (200, self.service.convert_time(body)),
        }
        if path in table: return table[path]()
        if len(parts) == 4 and parts[:2] == ["api", "airports"] and parts[3] == "timezone":
            return 200, self.service.update_airport_timezone(actor, role, parts[2], body)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            plan_id, action = int(parts[2]), parts[3]
            if action == "assignments": return 200, self.service.add_assignment(plan_id, actor, role, body)
            if action == "validate": return 200, self.service.validate_plan(plan_id, actor, role)
            if action == "lock": return 200, self.service.lock_plan(plan_id, actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "flights"] and parts[2].isdigit():
            flight_id, action = int(parts[2]), parts[3]
            if action == "cancel": return 200, self.service.cancel_flight(flight_id, actor, role, body)
            if action == "recover": return 200, self.service.recover_flight(flight_id, actor, role, body)
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path)
            respond(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            respond(self, exc.status, payload)
        except Exception as exc:
            print(f"unhandled error: {exc!r}"); respond(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = AirlineRecoveryService(db_path)
    handler = type("AirlineHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("AIRLINE_DB", "airline_recovery.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"airline-recovery listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
