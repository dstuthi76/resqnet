"""SQLite storage. One file, no server - fine for a pilot / demo.

For production swap this for Postgres (Supabase free tier works); the SQL is
plain enough to port.
"""
import json
import sqlite3
import threading

import config

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  received_at REAL, channel TEXT, text TEXT, lang TEXT,
  lat REAL, lng REAL, ts TEXT, hops INTEGER, device_id TEXT,
  sig_status TEXT, extracted TEXT, extractor TEXT,
  is_sos INTEGER, case_id TEXT, flags TEXT, truth TEXT
);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY,
  lat REAL, lng REAL, zone_id TEXT, location_approx INTEGER,
  people INTEGER, vulnerable TEXT, medical TEXT, needs TEXT, water_level TEXT,
  summary TEXT, score REAL, override_score REAL, reason TEXT,
  status TEXT, team_id TEXT, eta_min INTEGER,
  first_seen REAL, updated_at REAL, claimed_at REAL, closed_at REAL,
  message_count INTEGER, flags TEXT
);
CREATE TABLE IF NOT EXISTS teams (
  id TEXT PRIMARY KEY, name TEXT, kind TEXT, lat REAL, lng REAL,
  boats INTEGER, medical INTEGER
);
CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, pubkey TEXT, first_seen REAL);
CREATE TABLE IF NOT EXISTS ngos (id TEXT PRIMARY KEY, name TEXT, wallet TEXT, cases_reached INTEGER DEFAULT 0, deliveries INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS households (
  id_hash TEXT PRIMARY KEY, zone_id TEXT, wallet TEXT, wallet_key TEXT,
  channel TEXT, enrolled_at REAL, chain_tx TEXT
);
CREATE TABLE IF NOT EXISTS flood_readings (
  zone_id TEXT, event_id TEXT, hours INTEGER, fraction REAL, source TEXT, tx TEXT, ts REAL,
  PRIMARY KEY (zone_id, event_id)
);
CREATE TABLE IF NOT EXISTS confirmations (
  zone_id TEXT, event_id TEXT, ngo_id TEXT, tx TEXT, ts REAL,
  PRIMARY KEY (zone_id, event_id, ngo_id)
);
CREATE TABLE IF NOT EXISTS payouts (
  id_hash TEXT, zone_id TEXT, event_id TEXT, wallet TEXT, amount_wei TEXT, tx TEXT, ts REAL,
  PRIMARY KEY (id_hash, event_id)
);
CREATE TABLE IF NOT EXISTS triggered (zone_id TEXT, event_id TEXT, tx TEXT, ts REAL, PRIMARY KEY (zone_id, event_id));
CREATE TABLE IF NOT EXISTS relief_log (
  batch_id TEXT, from_party TEXT, to_party TEXT, photo_hash TEXT, ipfs_cid TEXT, tx TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def conn():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql, args=()):
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def kv_get(k, default=None):
    r = one("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r["v"]) if r else default


def kv_set(k, v):
    x("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))


def reset_all():
    with _lock:
        c = conn()
        for t in ["messages", "cases", "teams", "devices", "ngos", "households", "flood_readings",
                  "confirmations", "payouts", "triggered", "relief_log", "kv"]:
            c.execute(f"DELETE FROM {t}")
        c.commit()


def json_dumps(v):
    return json.dumps(v, ensure_ascii=False)


def loads(v, default=None):
    if v is None:
        return default if default is not None else []
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default
