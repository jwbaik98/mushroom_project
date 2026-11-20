"""SQLite 기반 측정값·경보·제어·진단 이력 저장소."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id TEXT NOT NULL, ts TEXT NOT NULL,
    temperature REAL, humidity REAL, co2 REAL, lux REAL, substrate_moisture REAL);
CREATE INDEX IF NOT EXISTS idx_readings_farm_ts ON readings(farm_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id TEXT NOT NULL, ts TEXT NOT NULL,
    level TEXT, metric TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS actuator_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id TEXT NOT NULL, ts TEXT NOT NULL,
    device TEXT, state INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id TEXT NOT NULL, ts TEXT NOT NULL,
    result TEXT, detail TEXT);
"""

COLS = ("temperature", "humidity", "co2", "lux", "substrate_moisture")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _query(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def add_reading(self, farm_id, reading, ts=None):
        self._exec(f"INSERT INTO readings (farm_id, ts, {', '.join(COLS)}) VALUES (?, ?, {', '.join('?' * len(COLS))})",
                   (farm_id, ts or _now(), *[reading.get(c) for c in COLS]))

    def readings(self, farm_id, hours=24, limit=5000):
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        return self._query("SELECT * FROM readings WHERE farm_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                           (farm_id, since, limit))[::-1]

    def latest(self, farm_id):
        rows = self._query("SELECT * FROM readings WHERE farm_id=? ORDER BY id DESC LIMIT 1", (farm_id,))
        return rows[0] if rows else None

    def add_alert(self, farm_id, level, metric, message):
        # 같은 경보가 10분 내 반복되면 중복 저장하지 않음
        since = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        dup = self._query("SELECT 1 FROM alerts WHERE farm_id=? AND metric=? AND level=? AND ts>=? LIMIT 1",
                          (farm_id, metric, level, since))
        if not dup:
            self._exec("INSERT INTO alerts (farm_id, ts, level, metric, message) VALUES (?,?,?,?,?)",
                       (farm_id, _now(), level, metric, message))
            return True
        return False

    def alerts(self, farm_id=None, limit=100):
        if farm_id:
            return self._query("SELECT * FROM alerts WHERE farm_id=? ORDER BY id DESC LIMIT ?", (farm_id, limit))
        return self._query("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))

    def log_actuator(self, farm_id, device, state, source):
        self._exec("INSERT INTO actuator_log (farm_id, ts, device, state, source) VALUES (?,?,?,?,?)",
                   (farm_id, _now(), device, int(bool(state)), source))

    def actuator_log(self, farm_id, limit=50):
        return self._query("SELECT * FROM actuator_log WHERE farm_id=? ORDER BY id DESC LIMIT ?", (farm_id, limit))

    def add_diagnosis(self, farm_id, result, detail=None):
        self._exec("INSERT INTO diagnoses (farm_id, ts, result, detail) VALUES (?,?,?,?)",
                   (farm_id, _now(), result, json.dumps(detail or {}, ensure_ascii=False)))

    def diagnoses(self, farm_id=None, limit=50):
        if farm_id:
            return self._query("SELECT * FROM diagnoses WHERE farm_id=? ORDER BY id DESC LIMIT ?", (farm_id, limit))
        return self._query("SELECT * FROM diagnoses ORDER BY id DESC LIMIT ?", (limit,))

    def purge(self, days=90):
        before = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        for t in ("readings", "alerts", "actuator_log"):
            self._exec(f"DELETE FROM {t} WHERE ts < ?", (before,))
