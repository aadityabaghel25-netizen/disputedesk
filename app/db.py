"""
Storage, using SQLite (a database that is just one file, built into Python).

Why SQLite and not Postgres? Zero setup for learning. Every query is in this one
file, so switching to Postgres later only touches this file.

Tables:
  events    every webhook PayPal sent us (raw, for debugging and replay)
  disputes  the latest copy of each dispute from PayPal
  evidence  facts we collected for a dispute (each has an id like E1, E2 ...)
  analyses  the AI's draft reply + verifier results + suggested action
  audit     an append-only, hash-chained log of everything that happened
  rules     seller rules edited in the UI (override the .env defaults)
  blocks    planned work on a dispute, for the deadline board (internal; never sent to PayPal)
  calls     the seller's call recordings as transcripts (audio is never stored). dispute_id '' = not matched yet
"""

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    resource_id  TEXT,
    verified     INTEGER NOT NULL,
    received_at  TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id   TEXT PRIMARY KEY,
    data         TEXT NOT NULL,
    status       TEXT,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    dispute_id   TEXT NOT NULL,
    evidence_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    source       TEXT NOT NULL,
    trusted      INTEGER NOT NULL,
    summary      TEXT NOT NULL,
    content      TEXT NOT NULL,
    PRIMARY KEY (dispute_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS analyses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    result       TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS blocks (
    dispute_id   TEXT PRIMARY KEY,
    handler      TEXT NOT NULL,
    start_at     TEXT NOT NULL,
    end_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    name         TEXT PRIMARY KEY,
    value        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    party        TEXT NOT NULL,
    call_date    TEXT NOT NULL,
    transcript   TEXT NOT NULL,
    origin       TEXT NOT NULL,
    audio_sha256 TEXT,
    filename     TEXT NOT NULL DEFAULT '',
    matched_on   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    dispute_id   TEXT,
    details      TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()   # the web server may run background jobs in parallel
        with self.lock:
            self.conn.executescript(SCHEMA)
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(calls)")}
            for col in ("filename", "matched_on"):   # databases made before the recording library
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE calls ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- events --------------------------------------------------------------

    def save_event(self, event: dict, verified: bool) -> bool:
        """Store a webhook. Returns False if we already saw it (PayPal can resend)."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                (event.get("id", ""), event.get("event_type", ""),
                 (event.get("resource") or {}).get("dispute_id") or (event.get("resource") or {}).get("id"),
                 int(verified), now_iso(), json.dumps(event)))
            self.conn.commit()
            return cur.rowcount == 1

    def recent_events(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT event_id, event_type, resource_id, verified, received_at FROM events "
            "ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- disputes ------------------------------------------------------------

    def upsert_dispute(self, dispute: dict) -> None:
        # PayPal drops the message history when a case escalates (seen in sandbox at the CHARGEBACK stage).
        # Keep every message we have ever seen, so the buyer's words and injection checks survive.
        old = self.get_dispute(dispute["dispute_id"])
        if old:
            key = lambda m: (m.get("time_posted"), m.get("content"))  # noqa: E731
            seen = {key(m) for m in dispute.get("messages") or []}
            kept = [m for m in old.get("messages") or [] if key(m) not in seen]
            if kept:
                dispute = {**dispute, "messages": kept + list(dispute.get("messages") or [])}
        with self.lock:
            self.conn.execute(
                "INSERT INTO disputes VALUES (?,?,?,?) ON CONFLICT(dispute_id) DO UPDATE SET "
                "data=excluded.data, status=excluded.status, updated_at=excluded.updated_at",
                (dispute["dispute_id"], json.dumps(dispute), dispute.get("status"), now_iso()))
            self.conn.commit()

    def get_dispute(self, dispute_id: str) -> dict | None:
        row = self.conn.execute("SELECT data FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def list_disputes(self) -> list[dict]:
        rows = self.conn.execute("SELECT data FROM disputes ORDER BY updated_at DESC").fetchall()
        return [json.loads(r["data"]) for r in rows]

    # -- evidence ------------------------------------------------------------

    def replace_evidence(self, dispute_id: str, items: list[dict]) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM evidence WHERE dispute_id=?", (dispute_id,))
            for it in items:
                self.conn.execute(
                    "INSERT INTO evidence VALUES (?,?,?,?,?,?,?)",
                    (dispute_id, it["id"], it["kind"], it["source"], int(it["trusted"]),
                     it["summary"], json.dumps(it["content"])))
            self.conn.commit()

    def get_evidence(self, dispute_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM evidence WHERE dispute_id=? ORDER BY CAST(SUBSTR(evidence_id, 2) AS INTEGER)",
            (dispute_id,)).fetchall()
        return [{"id": r["evidence_id"], "kind": r["kind"], "source": r["source"],
                 "trusted": bool(r["trusted"]), "summary": r["summary"],
                 "content": json.loads(r["content"])} for r in rows]

    # -- calls ---------------------------------------------------------------

    def add_call(self, actor: str, party: str, call_date: str, transcript: str, origin: str,
                 audio_sha256: str | None, filename: str = "") -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO calls (dispute_id, created_at, actor, party, call_date, transcript, origin, audio_sha256, filename) "
                "VALUES ('',?,?,?,?,?,?,?,?)", (now_iso(), actor, party, call_date, transcript, origin, audio_sha256, filename))
            self.conn.commit()
            return cur.lastrowid

    def list_calls(self, dispute_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM calls WHERE dispute_id=? ORDER BY id", (dispute_id,)).fetchall()
        return [dict(r) for r in rows]

    def all_calls(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM calls ORDER BY id DESC").fetchall()]

    def get_call(self, call_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return dict(row) if row else None

    def assign_call(self, call_id: int, dispute_id: str, matched_on: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE calls SET dispute_id=?, matched_on=? WHERE id=?", (dispute_id, matched_on, call_id))
            self.conn.commit()

    def delete_call(self, call_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM calls WHERE id=?", (call_id,))
            self.conn.commit()

    # -- analyses ------------------------------------------------------------

    def save_analysis(self, dispute_id: str, result: dict) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO analyses (dispute_id, created_at, result) VALUES (?,?,?)",
                                    (dispute_id, now_iso(), json.dumps(result)))
            self.conn.commit()
            return cur.lastrowid

    def latest_analysis(self, dispute_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM analyses WHERE dispute_id=? ORDER BY id DESC LIMIT 1", (dispute_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "created_at": row["created_at"], "state": row["state"],
                **json.loads(row["result"])}

    def set_analysis_state(self, analysis_id: int, state: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE analyses SET state=? WHERE id=?", (state, analysis_id))
            self.conn.commit()

    def all_analyses(self) -> list[dict]:
        """Latest analysis per dispute (for dashboard stats)."""
        rows = self.conn.execute(
            "SELECT * FROM analyses WHERE id IN (SELECT MAX(id) FROM analyses GROUP BY dispute_id)").fetchall()
        return [{"id": r["id"], "dispute_id": r["dispute_id"], "state": r["state"], **json.loads(r["result"])}
                for r in rows]

    # -- work blocks -----------------------------------------------------------

    def get_blocks(self) -> dict[str, dict]:
        return {r["dispute_id"]: dict(r) for r in self.conn.execute("SELECT * FROM blocks").fetchall()}

    def set_block(self, dispute_id: str, handler: str, start_at: str, end_at: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO blocks VALUES (?,?,?,?) ON CONFLICT(dispute_id) DO UPDATE SET "
                              "handler=excluded.handler, start_at=excluded.start_at, end_at=excluded.end_at",
                              (dispute_id, handler, start_at, end_at))
            self.conn.commit()

    # -- seller rules ----------------------------------------------------------

    def get_rules(self) -> dict[str, str]:
        return {r["name"]: r["value"] for r in self.conn.execute("SELECT * FROM rules").fetchall()}

    def set_rule(self, name: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO rules VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                              (name, value))
            self.conn.commit()

    # -- audit log (hash chain) ----------------------------------------------

    def audit(self, actor: str, action: str, dispute_id: str | None, details: dict) -> None:
        """Append to the audit log. Each row stores the hash of the previous row,
        so editing or deleting any old row breaks the chain and is detectable.
        (Same idea as a blockchain or git commits, without the hype.)"""
        with self.lock:
            last = self.conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else "GENESIS"
            ts = now_iso()
            body = json.dumps([ts, actor, action, dispute_id, details, prev], sort_keys=True)
            h = hashlib.sha256(body.encode()).hexdigest()
            self.conn.execute(
                "INSERT INTO audit (ts, actor, action, dispute_id, details, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (ts, actor, action, dispute_id, json.dumps(details), prev, h))
            self.conn.commit()

    def audit_log(self, dispute_id: str | None = None, limit: int = 100) -> list[dict]:
        if dispute_id:
            rows = self.conn.execute("SELECT * FROM audit WHERE dispute_id=? ORDER BY id DESC LIMIT ?",
                                     (dispute_id, limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    def verify_audit_chain(self) -> tuple[bool, int | None]:
        """Re-compute every hash. Returns (ok, first_bad_row_id)."""
        prev = "GENESIS"
        for r in self.conn.execute("SELECT * FROM audit ORDER BY id").fetchall():
            body = json.dumps([r["ts"], r["actor"], r["action"], r["dispute_id"],
                               json.loads(r["details"]), prev], sort_keys=True)
            if r["prev_hash"] != prev or hashlib.sha256(body.encode()).hexdigest() != r["hash"]:
                return False, r["id"]
            prev = r["hash"]
        return True, None
