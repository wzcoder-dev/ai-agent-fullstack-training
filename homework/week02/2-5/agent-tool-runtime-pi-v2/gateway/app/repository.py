from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class GatewayRepository:
    """SQLite default implementation behind a repository-shaped seam."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
              id TEXT PRIMARY KEY, request_id TEXT NOT NULL, requested_model TEXT NOT NULL,
              actual_model TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL,
              input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
              cost_usd REAL NOT NULL, latency_ms INTEGER NOT NULL, error_code TEXT,
              metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def add_call(self, **record: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO llm_calls (
              id, request_id, requested_model, actual_model, status, attempts,
              input_tokens, output_tokens, cost_usd, latency_ms, error_code,
              metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()), record["request_id"], record["requested_model"], record.get("actual_model"),
                record["status"], record["attempts"], record.get("input_tokens", 0),
                record.get("output_tokens", 0), record.get("cost_usd", 0.0), record["latency_ms"],
                record.get("error_code"), json.dumps(record.get("metadata", {}), ensure_ascii=False),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def list_calls(self, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            "SELECT * FROM llm_calls ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 500),)
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
