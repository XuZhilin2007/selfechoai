from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import Database, DatabaseVersionError, SCHEMA_V3_VERSION
from app.migrations.v003_auth import OwnerUser, migrate_v2_to_v3


V2_SCHEMA = """
CREATE TABLE personal_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    type TEXT NOT NULL CHECK (length(trim(type)) > 0),
    importance TEXT NOT NULL CHECK (importance IN ('high', 'medium', 'low', 'unknown')),
    urgency TEXT NOT NULL CHECK (urgency IN ('high', 'medium', 'low', 'unknown')),
    deadline TEXT,
    estimated_time INTEGER CHECK (estimated_time IS NULL OR estimated_time > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'trash')),
    next_action TEXT,
    extra_information TEXT,
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL
);

CREATE TABLE item_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES personal_items(id) ON DELETE CASCADE,
    original_text TEXT NOT NULL CHECK (length(trim(original_text)) > 0),
    input_method TEXT NOT NULL CHECK (input_method IN ('text', 'voice')),
    processing_status TEXT NOT NULL CHECK (
        processing_status IN ('pending', 'processing', 'succeeded', 'failed')
    ),
    failure_type TEXT CHECK (
        failure_type IS NULL OR failure_type IN (
            'configuration', 'network', 'api', 'invalid_output', 'internal'
        )
    ),
    failure_message TEXT,
    created_time TEXT NOT NULL
);

PRAGMA user_version = 2;
"""


def test_v2_database_migrates_to_v3_without_losing_user_data(tmp_path: Path):
    database_path = tmp_path / "v2.db"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(V2_SCHEMA)
    connection.execute(
        """
        INSERT INTO personal_items (
            id, title, type, importance, urgency, deadline, estimated_time,
            status, next_action, extra_information, created_time, updated_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            7,
            "保留的事项",
            "study",
            "high",
            "medium",
            "2026-09-01",
            90,
            "active",
            "复习第四章",
            '{"context":"保留背景"}',
            "2026-08-24T00:00:00+00:00",
            "2026-08-24T01:00:00+00:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO item_inputs (
            id, item_id, original_text, input_method, processing_status,
            failure_type, failure_message, created_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            11,
            7,
            "必须原样保留的首次输入",
            "text",
            "succeeded",
            None,
            None,
            "2026-08-24T00:00:00+00:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO item_inputs (
            id, item_id, original_text, input_method, processing_status,
            failure_type, failure_message, created_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            15,
            None,
            "尚未关联且失败的原始输入",
            "voice",
            "failed",
            "network",
            "网络暂时不可用",
            "2026-08-24T02:00:00+00:00",
        ),
    )
    connection.executemany(
        """
        INSERT INTO item_inputs (
            id, item_id, original_text, input_method, processing_status,
            failure_type, failure_message, created_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                17,
                None,
                "等待整理的原始输入",
                "text",
                "pending",
                None,
                None,
                "2026-08-24T03:00:00+00:00",
            ),
            (
                19,
                7,
                "正在整理的补充输入",
                "text",
                "processing",
                None,
                None,
                "2026-08-24T04:00:00+00:00",
            ),
        ),
    )
    connection.commit()
    connection.close()

    result = migrate_v2_to_v3(
        database_path,
        OwnerUser(
            email="OWNER@Example.com ",
            password_hash="$argon2id$v=19$m=19456,t=2,p=1$test$hash",
            display_name="Owner",
            timezone="Asia/Shanghai",
        ),
    )

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    owner = connection.execute("SELECT * FROM users").fetchone()
    item = connection.execute("SELECT * FROM personal_items WHERE id = 7").fetchone()
    inputs = connection.execute(
        "SELECT * FROM item_inputs ORDER BY id"
    ).fetchall()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()

    assert result.owner_user_id == owner["id"]
    assert result.personal_item_count == 1
    assert result.item_input_count == 4
    assert owner["email"] == "owner@example.com"
    assert owner["display_name"] == "Owner"
    assert item["id"] == 7
    assert item["user_id"] == owner["id"]
    assert [row["id"] for row in inputs] == [11, 15, 17, 19]
    assert inputs[0]["item_id"] == 7
    assert inputs[0]["original_text"] == "必须原样保留的首次输入"
    assert inputs[0]["processing_status"] == "succeeded"
    assert inputs[1]["item_id"] is None
    assert inputs[1]["original_text"] == "尚未关联且失败的原始输入"
    assert inputs[1]["processing_status"] == "failed"
    assert inputs[1]["failure_type"] == "network"
    assert inputs[2]["item_id"] is None
    assert inputs[2]["original_text"] == "等待整理的原始输入"
    assert inputs[2]["processing_status"] == "pending"
    assert inputs[3]["item_id"] == 7
    assert inputs[3]["original_text"] == "正在整理的补充输入"
    assert inputs[3]["processing_status"] == "processing"
    assert {row["user_id"] for row in inputs} == {owner["id"]}
    assert version == SCHEMA_V3_VERSION
    assert foreign_key_errors == []
    assert integrity == "ok"

    with pytest.raises(DatabaseVersionError, match="explicit migration"):
        Database(database_path).initialize()
