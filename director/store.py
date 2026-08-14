"""SQLite store for Director: agents, threads, messages, the live event log,
devices, machines, jobs, approvals and memory.

One connection in WAL mode guarded by a lock. Every query here is small and
indexed, so the async server can call it inline without an executor hop.

The event log is the spine of the product: everything the phone renders — a
token delta, a tool chip, an approval card, a screenshot, a status change —
is an append-only row with a monotonic id. Clients resume by passing the last
id they saw, which is what makes "live updates while work runs" survive a
phone locking its screen or a train tunnel.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    emoji        TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'director',
    subtitle     TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    backend      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    reasoning    TEXT NOT NULL DEFAULT '',
    tools        TEXT NOT NULL DEFAULT '[]',
    sort         INTEGER NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    preview    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'idle',
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_agent ON threads(agent_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  TEXT NOT NULL DEFAULT '',
    agent_id   TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_thread ON events(thread_id, id);

CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'phone',
    token_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS devices_token ON devices(token_hash);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'phone',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS machines (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    platform   TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL,
    caps       TEXT NOT NULL DEFAULT '{}',
    online     INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS machines_token ON machines(token_hash);

CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    thread_id  TEXT NOT NULL DEFAULT '',
    agent_id   TEXT NOT NULL DEFAULT '',
    machine_id TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'queued',
    request    TEXT NOT NULL DEFAULT '{}',
    result     TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_machine ON jobs(machine_id, status);
CREATE INDEX IF NOT EXISTS jobs_thread ON jobs(thread_id, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id         TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL DEFAULT '',
    agent_id   TEXT NOT NULL DEFAULT '',
    tool       TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'pending',
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decided_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS routines (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    prompt     TEXT NOT NULL DEFAULT '',
    schedule   TEXT NOT NULL DEFAULT '{}',
    spoken     TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    next_run   REAL NOT NULL DEFAULT 0,
    last_run   REAL NOT NULL DEFAULT 0,
    runs       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS routines_due ON routines(enabled, next_run);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id           TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL DEFAULT '',
    endpoint     TEXT NOT NULL,
    subscription TEXT NOT NULL,
    created_at   REAL NOT NULL,
    failures     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS push_endpoint ON push_subscriptions(endpoint);

CREATE TABLE IF NOT EXISTS memory (
    id         TEXT PRIMARY KEY,
    scope      TEXT NOT NULL DEFAULT 'global',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_scope_key ON memory(scope, key);
"""

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            conn = sqlite3.connect(str(config.db_path()), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
            _CONN = conn
        return _CONN


# Columns added after the first boxes were already running. CREATE TABLE IF NOT
# EXISTS will not add them, so every deploy checks and fills the gaps.
ADDED_COLUMNS = {
    "agents": [("avatar", "TEXT NOT NULL DEFAULT ''"),
               ("auto_approve", "INTEGER NOT NULL DEFAULT 0"),
               ("notify", "INTEGER NOT NULL DEFAULT 1"),
               ("members", "TEXT NOT NULL DEFAULT '[]'"),
               ("rules", "TEXT NOT NULL DEFAULT ''")],
    "threads": [("muted", "INTEGER NOT NULL DEFAULT 0"),
                ("summary", "TEXT NOT NULL DEFAULT ''"),
                ("compacted_through", "INTEGER NOT NULL DEFAULT 0"),
                ("compacted_at", "REAL NOT NULL DEFAULT 0")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        try:
            present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError:
            continue
        for name, definition in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def close() -> None:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None


def _rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _LOCK:
        cur = connect().execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def _row(sql: str, params: Iterable[Any] = ()) -> dict | None:
    got = _rows(sql, params)
    return got[0] if got else None


def _exec(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _LOCK:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


# ---------------- agents ----------------

def _decode_agent(row: dict) -> dict:
    row["tools"] = _loads(row.get("tools"), [])
    row["members"] = [str(item) for item in _loads(row.get("members"), []) if str(item).strip()]
    row["rules"] = str(row.get("rules") or "")
    row["auto_approve"] = int(row.get("auto_approve") or 0)
    row["notify"] = int(row.get("notify") if row.get("notify") is not None else 1)
    return row


def is_group(agent: dict | None) -> bool:
    return str((agent or {}).get("kind") or "") == "group"


def create_agent(*, name: str, emoji: str = "", kind: str = "director",
                 subtitle: str = "", system_prompt: str = "", backend: str = "",
                 model: str = "", reasoning: str = "", tools: list[str] | None = None,
                 sort: int = 0, agent_id: str = "", avatar: str = "",
                 members: list[str] | None = None, rules: str = "") -> dict:
    now = time.time()
    prefix = "grp" if kind == "group" else "agt"
    aid = agent_id or new_id(prefix)
    _exec(
        "INSERT INTO agents (id, name, emoji, kind, subtitle, system_prompt, backend,"
        " model, reasoning, tools, sort, archived, created_at, avatar, members, rules)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
        (aid, name, emoji, kind, subtitle, system_prompt, backend, model, reasoning,
         json.dumps(list(tools or [])), sort, now, avatar,
         json.dumps(list(members or [])), str(rules or "")),
    )
    return get_agent(aid) or {}


def get_agent(agent_id: str) -> dict | None:
    row = _row("SELECT * FROM agents WHERE id = ?", (agent_id,))
    return _decode_agent(row) if row else None


def list_agents(*, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM agents"
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY sort, created_at"
    return [_decode_agent(row) for row in _rows(sql)]


def update_agent(agent_id: str, patch: dict) -> dict | None:
    allowed = {"name", "emoji", "kind", "subtitle", "system_prompt", "backend",
               "model", "reasoning", "tools", "sort", "archived", "avatar",
               "auto_approve", "notify", "members", "rules"}
    sets, params = [], []
    for key, value in (patch or {}).items():
        if key not in allowed:
            continue
        if key in ("tools", "members"):
            value = json.dumps(list(value or []))
        if key in ("archived", "auto_approve", "notify"):
            value = 1 if value else 0
        sets.append(f"{key} = ?")
        params.append(value)
    if sets:
        params.append(agent_id)
        _exec(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", params)
    return get_agent(agent_id)


def delete_agent(agent_id: str) -> None:
    _exec("DELETE FROM agents WHERE id = ?", (agent_id,))


# ---------------- threads & messages ----------------

def create_thread(agent_id: str, *, title: str = "") -> dict:
    now = time.time()
    tid = new_id("thr")
    _exec(
        "INSERT INTO threads (id, agent_id, title, preview, status, archived, created_at, updated_at)"
        " VALUES (?,?,?,'', 'idle', 0, ?, ?)",
        (tid, agent_id, title, now, now),
    )
    return get_thread(tid) or {}


def get_thread(thread_id: str) -> dict | None:
    return _row("SELECT * FROM threads WHERE id = ?", (thread_id,))


def list_threads(agent_id: str = "", *, limit: int = 50) -> list[dict]:
    if agent_id:
        return _rows(
            "SELECT * FROM threads WHERE agent_id = ? AND archived = 0"
            " ORDER BY updated_at DESC LIMIT ?", (agent_id, limit))
    return _rows(
        "SELECT * FROM threads WHERE archived = 0 ORDER BY updated_at DESC LIMIT ?",
        (limit,))


def latest_thread(agent_id: str) -> dict | None:
    rows = list_threads(agent_id, limit=1)
    return rows[0] if rows else None


def touch_thread(thread_id: str, *, preview: str = "", status: str = "",
                 title: str = "") -> None:
    sets = ["updated_at = ?"]
    params: list[Any] = [time.time()]
    if preview:
        sets.append("preview = ?")
        params.append(preview[:280])
    if status:
        sets.append("status = ?")
        params.append(status)
    if title:
        sets.append("title = ?")
        params.append(title[:120])
    params.append(thread_id)
    _exec(f"UPDATE threads SET {', '.join(sets)} WHERE id = ?", params)


def archive_thread(thread_id: str) -> None:
    _exec("UPDATE threads SET archived = 1 WHERE id = ?", (thread_id,))


def add_message(thread_id: str, role: str, content: str, meta: dict | None = None) -> dict:
    now = time.time()
    mid = new_id("msg")
    _exec(
        "INSERT INTO messages (id, thread_id, role, content, meta, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (mid, thread_id, role, content, json.dumps(meta or {}), now),
    )
    return {"id": mid, "thread_id": thread_id, "role": role, "content": content,
            "meta": meta or {}, "created_at": now}


def list_messages(thread_id: str, *, limit: int = 5000,
                  after_sequence: int = 0, through_sequence: int = 0) -> list[dict]:
    where = ["thread_id = ?", "rowid > ?"]
    params: list[Any] = [thread_id, int(after_sequence or 0)]
    if through_sequence:
        where.append("rowid <= ?")
        params.append(int(through_sequence))
    params.append(limit)
    rows = _rows(
        f"SELECT rowid AS sequence, * FROM messages WHERE {' AND '.join(where)}"
        " ORDER BY rowid LIMIT ?", params)
    for row in rows:
        row["meta"] = _loads(row.get("meta"), {})
    return rows


def search_messages(query: str, *, limit: int = 20) -> list[dict]:
    """Search readable transcript rows across live private and group chats."""
    needle = f"%{str(query or '').strip().lower()}%"
    rows = _rows(
        "SELECT m.rowid AS sequence, m.*, a.id AS agent_id, "
        "a.name AS agent_name, a.kind AS agent_kind "
        "FROM messages m JOIN threads t ON t.id = m.thread_id "
        "JOIN agents a ON a.id = t.agent_id "
        "WHERE t.archived = 0 AND a.archived = 0 "
        "AND m.role IN ('user','assistant','system') "
        "AND lower(m.content) LIKE ? ORDER BY m.rowid DESC LIMIT ?",
        (needle, max(1, int(limit or 20))),
    )
    for row in rows:
        row["meta"] = _loads(row.get("meta"), {})
    return rows


def latest_message_sequence(thread_id: str) -> int:
    row = _row("SELECT COALESCE(MAX(rowid), 0) AS sequence FROM messages WHERE thread_id = ?",
               (thread_id,))
    return int(row["sequence"]) if row else 0


def threads_due_for_compaction(*, before: float, force: bool = False,
                               limit: int = 50) -> list[dict]:
    idle = "" if force else "AND t.status = 'idle' AND t.updated_at <= ?"
    params: list[Any] = [] if force else [before]
    params.append(limit)
    return _rows(
        "SELECT t.*, (SELECT COALESCE(MAX(m.rowid), 0) FROM messages m"
        " WHERE m.thread_id = t.id) AS latest_sequence FROM threads t"
        " WHERE t.archived = 0 " + idle +
        " AND t.compacted_through < (SELECT COALESCE(MAX(m.rowid), 0) FROM messages m"
        " WHERE m.thread_id = t.id) ORDER BY t.updated_at LIMIT ?", params)


def save_compaction(thread_id: str, summary: str, through_sequence: int) -> None:
    _exec(
        "UPDATE threads SET summary = ?, compacted_through = ?, compacted_at = ?"
        " WHERE id = ? AND compacted_through < ?",
        (summary, int(through_sequence), time.time(), thread_id, int(through_sequence)),
    )


def clear_messages(thread_id: str) -> None:
    _exec("DELETE FROM messages WHERE thread_id = ?", (thread_id,))


# ---------------- events ----------------

def add_event(kind: str, payload: dict | None = None, *, thread_id: str = "",
              agent_id: str = "") -> dict:
    now = time.time()
    cur = _exec(
        "INSERT INTO events (thread_id, agent_id, kind, payload, created_at)"
        " VALUES (?,?,?,?,?)",
        (thread_id, agent_id, kind, json.dumps(payload or {}), now),
    )
    return {"id": int(cur.lastrowid), "thread_id": thread_id, "agent_id": agent_id,
            "kind": kind, "payload": payload or {}, "created_at": now}


def list_events(*, since: int = 0, thread_id: str = "", limit: int = 500) -> list[dict]:
    if thread_id:
        rows = _rows(
            "SELECT * FROM events WHERE id > ? AND thread_id = ? ORDER BY id LIMIT ?",
            (since, thread_id, limit))
    else:
        rows = _rows("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                     (since, limit))
    for row in rows:
        row["payload"] = _loads(row.get("payload"), {})
    return rows


def list_job_events(job_id: str, *, since: int = 0, limit: int = 1000) -> list[dict]:
    """Return the persisted event stream for one background job."""
    rows = _rows(
        "SELECT * FROM events WHERE id > ? "
        "AND json_extract(payload, '$.job_id') = ? ORDER BY id LIMIT ?",
        (int(since or 0), str(job_id or ""), max(1, int(limit or 1000))),
    )
    for row in rows:
        row["payload"] = _loads(row.get("payload"), {})
    return rows


def latest_event_id() -> int:
    row = _row("SELECT COALESCE(MAX(id), 0) AS id FROM events")
    return int(row["id"]) if row else 0


def prune_events(*, keep: int = 20000) -> int:
    row = _row("SELECT COALESCE(MAX(id), 0) AS id FROM events")
    if not row:
        return 0
    cutoff = int(row["id"]) - keep
    if cutoff <= 0:
        return 0
    cur = _exec("DELETE FROM events WHERE id <= ?", (cutoff,))
    return cur.rowcount or 0


# ---------------- devices ----------------

def create_device(*, name: str, kind: str, token_hash: str) -> dict:
    now = time.time()
    did = new_id("dev")
    _exec(
        "INSERT INTO devices (id, name, kind, token_hash, created_at, last_seen)"
        " VALUES (?,?,?,?,?,?)",
        (did, name, kind, token_hash, now, now),
    )
    return {"id": did, "name": name, "kind": kind, "created_at": now}


def device_by_token_hash(token_hash: str) -> dict | None:
    return _row("SELECT * FROM devices WHERE token_hash = ?", (token_hash,))


def list_devices() -> list[dict]:
    return _rows("SELECT id, name, kind, created_at, last_seen FROM devices ORDER BY created_at")


def touch_device(device_id: str) -> None:
    _exec("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time(), device_id))


def delete_device(device_id: str) -> None:
    _exec("DELETE FROM devices WHERE id = ?", (device_id,))


# ---------------- pairing codes ----------------

def add_pairing_code(code_hash: str, *, kind: str, ttl: float) -> dict:
    now = time.time()
    _exec("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
    _exec("INSERT OR REPLACE INTO pairing_codes (code_hash, kind, created_at, expires_at)"
          " VALUES (?,?,?,?)", (code_hash, kind, now, now + ttl))
    return {"kind": kind, "created_at": now, "expires_at": now + ttl}


def take_pairing_code(code_hash: str) -> dict | None:
    """Look a code up and consume it. One use, then it is gone."""
    now = time.time()
    _exec("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
    row = _row("SELECT * FROM pairing_codes WHERE code_hash = ?", (code_hash,))
    if not row:
        return None
    _exec("DELETE FROM pairing_codes WHERE code_hash = ?", (code_hash,))
    return row


def list_pairing_codes() -> list[dict]:
    _exec("DELETE FROM pairing_codes WHERE expires_at < ?", (time.time(),))
    return _rows("SELECT kind, created_at, expires_at FROM pairing_codes ORDER BY created_at")


# ---------------- machines (Windows / other clients) ----------------

def upsert_machine(*, name: str, platform: str, token_hash: str,
                   caps: dict | None = None) -> dict:
    now = time.time()
    existing = _row("SELECT * FROM machines WHERE name = ?", (name,))
    if existing:
        _exec(
            "UPDATE machines SET platform = ?, token_hash = ?, caps = ?, last_seen = ?"
            " WHERE id = ?",
            (platform, token_hash, json.dumps(caps or {}), now, existing["id"]),
        )
        return get_machine(existing["id"]) or {}
    mid = new_id("mch")
    _exec(
        "INSERT INTO machines (id, name, platform, token_hash, caps, online, created_at, last_seen)"
        " VALUES (?,?,?,?,?,0,?,?)",
        (mid, name, platform, token_hash, json.dumps(caps or {}), now, now),
    )
    return get_machine(mid) or {}


def get_machine(machine_id: str) -> dict | None:
    row = _row("SELECT * FROM machines WHERE id = ?", (machine_id,))
    if row:
        row["caps"] = _loads(row.get("caps"), {})
    return row


def machine_by_token_hash(token_hash: str) -> dict | None:
    row = _row("SELECT * FROM machines WHERE token_hash = ?", (token_hash,))
    if row:
        row["caps"] = _loads(row.get("caps"), {})
    return row


def machine_by_name(name: str) -> dict | None:
    row = _row("SELECT * FROM machines WHERE name = ?", (name,))
    if row:
        row["caps"] = _loads(row.get("caps"), {})
    return row


def list_machines() -> list[dict]:
    rows = _rows("SELECT * FROM machines ORDER BY created_at")
    for row in rows:
        row["caps"] = _loads(row.get("caps"), {})
    return rows


def set_machine_online(machine_id: str, online: bool) -> None:
    _exec("UPDATE machines SET online = ?, last_seen = ? WHERE id = ?",
          (1 if online else 0, time.time(), machine_id))


# ---------------- jobs ----------------

def create_job(*, kind: str, request: dict, thread_id: str = "", agent_id: str = "",
               machine_id: str = "", status: str = "queued") -> dict:
    now = time.time()
    jid = new_id("job")
    _exec(
        "INSERT INTO jobs (id, kind, thread_id, agent_id, machine_id, status, request,"
        " result, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'{}',?,?)",
        (jid, kind, thread_id, agent_id, machine_id, status, json.dumps(request or {}), now, now),
    )
    return get_job(jid) or {}


def get_job(job_id: str) -> dict | None:
    row = _row("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row:
        row["request"] = _loads(row.get("request"), {})
        row["result"] = _loads(row.get("result"), {})
    return row


def update_job(job_id: str, *, status: str = "", result: dict | None = None) -> dict | None:
    sets = ["updated_at = ?"]
    params: list[Any] = [time.time()]
    if status:
        sets.append("status = ?")
        params.append(status)
    if result is not None:
        sets.append("result = ?")
        params.append(json.dumps(result))
    params.append(job_id)
    _exec(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
    return get_job(job_id)


def list_jobs(*, machine_id: str = "", thread_id: str = "", status: str = "",
              limit: int = 50) -> list[dict]:
    clauses, params = [], []
    if machine_id:
        clauses.append("machine_id = ?")
        params.append(machine_id)
    if thread_id:
        clauses.append("thread_id = ?")
        params.append(thread_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = _rows(f"SELECT * FROM jobs{where} ORDER BY created_at DESC LIMIT ?", params)
    for row in rows:
        row["request"] = _loads(row.get("request"), {})
        row["result"] = _loads(row.get("result"), {})
    return rows


# ---------------- approvals ----------------

def create_approval(*, thread_id: str, agent_id: str, tool: str, summary: str,
                    detail: str = "", payload: dict | None = None) -> dict:
    now = time.time()
    aid = new_id("apr")
    _exec(
        "INSERT INTO approvals (id, thread_id, agent_id, tool, summary, detail, payload,"
        " status, note, created_at, decided_at) VALUES (?,?,?,?,?,?,?, 'pending', '', ?, 0)",
        (aid, thread_id, agent_id, tool, summary, detail, json.dumps(payload or {}), now),
    )
    return get_approval(aid) or {}


def get_approval(approval_id: str) -> dict | None:
    row = _row("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if row:
        row["payload"] = _loads(row.get("payload"), {})
    return row


def decide_approval(approval_id: str, status: str, note: str = "") -> dict | None:
    _exec("UPDATE approvals SET status = ?, note = ?, decided_at = ? WHERE id = ?",
          (status, note, time.time(), approval_id))
    return get_approval(approval_id)


def list_approvals(*, status: str = "pending", limit: int = 50) -> list[dict]:
    rows = _rows("SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                 (status, limit))
    for row in rows:
        row["payload"] = _loads(row.get("payload"), {})
    return rows


# ---------------- routines ----------------

def create_routine(*, agent_id: str, name: str, prompt: str, schedule: dict,
                   spoken: str = "", next_run: float = 0.0) -> dict:
    now = time.time()
    rid = new_id("rtn")
    _exec(
        "INSERT INTO routines (id, agent_id, name, prompt, schedule, spoken, enabled,"
        " next_run, last_run, runs, created_at) VALUES (?,?,?,?,?,?,1,?,0,0,?)",
        (rid, agent_id, name, prompt, json.dumps(schedule or {}), spoken, next_run, now),
    )
    return get_routine(rid) or {}


def get_routine(routine_id: str) -> dict | None:
    row = _row("SELECT * FROM routines WHERE id = ?", (routine_id,))
    if row:
        row["schedule"] = _loads(row.get("schedule"), {})
    return row


def list_routines(*, agent_id: str = "", include_disabled: bool = True) -> list[dict]:
    clauses, params = [], []
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if not include_disabled:
        clauses.append("enabled = 1")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = _rows(f"SELECT * FROM routines{where} ORDER BY next_run", params)
    for row in rows:
        row["schedule"] = _loads(row.get("schedule"), {})
    return rows


def due_routines(now: float | None = None) -> list[dict]:
    moment = now if now is not None else time.time()
    rows = _rows("SELECT * FROM routines WHERE enabled = 1 AND next_run > 0"
                 " AND next_run <= ? ORDER BY next_run", (moment,))
    for row in rows:
        row["schedule"] = _loads(row.get("schedule"), {})
    return rows


def update_routine(routine_id: str, patch: dict) -> dict | None:
    allowed = {"name", "prompt", "schedule", "spoken", "enabled", "next_run",
               "last_run", "runs"}
    sets, params = [], []
    for key, value in (patch or {}).items():
        if key not in allowed:
            continue
        if key == "schedule":
            value = json.dumps(value or {})
        if key == "enabled":
            value = 1 if value else 0
        sets.append(f"{key} = ?")
        params.append(value)
    if sets:
        params.append(routine_id)
        _exec(f"UPDATE routines SET {', '.join(sets)} WHERE id = ?", params)
    return get_routine(routine_id)


def delete_routine(routine_id: str) -> None:
    _exec("DELETE FROM routines WHERE id = ?", (routine_id,))


# ---------------- push subscriptions ----------------

def add_push_subscription(subscription: dict, *, device_id: str = "") -> dict:
    endpoint = str((subscription or {}).get("endpoint") or "")
    if not endpoint:
        return {}
    now = time.time()
    existing = _row("SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    if existing:
        _exec("UPDATE push_subscriptions SET subscription = ?, device_id = ?, failures = 0"
              " WHERE endpoint = ?", (json.dumps(subscription), device_id, endpoint))
        return get_push_subscription(existing["id"]) or {}
    sid = new_id("psh")
    _exec("INSERT INTO push_subscriptions (id, device_id, endpoint, subscription,"
          " created_at, failures) VALUES (?,?,?,?,?,0)",
          (sid, device_id, endpoint, json.dumps(subscription), now))
    return get_push_subscription(sid) or {}


def get_push_subscription(subscription_id: str) -> dict | None:
    row = _row("SELECT * FROM push_subscriptions WHERE id = ?", (subscription_id,))
    if row:
        row["subscription"] = _loads(row.get("subscription"), {})
    return row


def list_push_subscriptions() -> list[dict]:
    rows = _rows("SELECT * FROM push_subscriptions ORDER BY created_at")
    for row in rows:
        row["subscription"] = _loads(row.get("subscription"), {})
    return rows


def drop_push_subscription(endpoint: str) -> None:
    _exec("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def note_push_failure(endpoint: str) -> None:
    _exec("UPDATE push_subscriptions SET failures = failures + 1 WHERE endpoint = ?",
          (endpoint,))


# ---------------- memory ----------------

def remember(key: str, value: str, *, scope: str = "global") -> dict:
    now = time.time()
    existing = _row("SELECT * FROM memory WHERE scope = ? AND key = ?", (scope, key))
    if existing:
        _exec("UPDATE memory SET value = ?, updated_at = ? WHERE id = ?",
              (value, now, existing["id"]))
        return {"id": existing["id"], "scope": scope, "key": key, "value": value,
                "created_at": existing["created_at"], "updated_at": now}
    mid = new_id("mem")
    _exec("INSERT INTO memory (id, scope, key, value, created_at, updated_at)"
          " VALUES (?,?,?,?,?,?)", (mid, scope, key, value, now, now))
    return {"id": mid, "scope": scope, "key": key, "value": value,
            "created_at": now, "updated_at": now}


def recall(key: str, *, scope: str = "global") -> str:
    row = _row("SELECT value FROM memory WHERE scope = ? AND key = ?", (scope, key))
    return str(row["value"]) if row else ""


def list_memory(*, scope: str = "", limit: int = 200) -> list[dict]:
    if scope:
        return _rows("SELECT * FROM memory WHERE scope = ? ORDER BY updated_at DESC LIMIT ?",
                     (scope, limit))
    return _rows("SELECT * FROM memory ORDER BY updated_at DESC LIMIT ?", (limit,))


def forget(key: str, *, scope: str = "global") -> None:
    _exec("DELETE FROM memory WHERE scope = ? AND key = ?", (scope, key))
