"""SQLite log of every qualification decision."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import LeadIn, OutputResult, Qualification

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    source      TEXT NOT NULL,
    email       TEXT,
    company     TEXT,
    tier        TEXT NOT NULL,
    score       REAL NOT NULL,
    rule_score  REAL NOT NULL,
    llm_score   REAL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    lead_json   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    routing_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS decisions_created_at ON decisions (created_at DESC);
CREATE INDEX IF NOT EXISTS decisions_tier ON decisions (tier);
"""


class DecisionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn:
            yield self._conn

    def save(self, lead: LeadIn, result: Qualification) -> str:
        decision_id = uuid.uuid4().hex
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO decisions (id, created_at, source, email, company, tier, score, rule_score,
                   llm_score, needs_review, lead_json, result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    result.created_at.isoformat(),
                    lead.source,
                    lead.email,
                    result.extracted.company or lead.company,
                    result.tier.value,
                    result.score,
                    result.rule_score,
                    result.llm_score,
                    int(result.needs_review),
                    lead.model_dump_json(),
                    result.model_dump_json(),
                ),
            )
        return decision_id

    def save_routing(self, decision_id: str, results: list[OutputResult]) -> None:
        payload = json.dumps([r.model_dump() for r in results])
        with self._tx() as conn:
            conn.execute("UPDATE decisions SET routing_json = ? WHERE id = ?", (payload, decision_id))

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        return _row(row) if row else None

    def recent(self, limit: int = 50, tier: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM decisions"
        params: list[Any] = []
        if tier:
            query += " WHERE tier = ?"
            params.append(tier)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._tx() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT tier, COUNT(*) AS n, AVG(score) AS avg_score, SUM(needs_review) AS review FROM decisions GROUP BY tier"
            ).fetchall()
        by_tier = {
            r["tier"]: {
                "count": r["n"],
                "avg_score": round(r["avg_score"] or 0, 1),
                "needs_review": r["review"] or 0,
            }
            for r in rows
        }
        return {"total": sum(v["count"] for v in by_tier.values()), "by_tier": by_tier}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["lead"] = json.loads(data.pop("lead_json"))
    data["result"] = json.loads(data.pop("result_json"))
    data["routing"] = json.loads(data.pop("routing_json"))
    data["needs_review"] = bool(data["needs_review"])
    return data
