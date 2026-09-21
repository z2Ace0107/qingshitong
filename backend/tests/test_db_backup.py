from __future__ import annotations

import sqlite3

from app import db


def test_backup_database_uses_online_backup_and_integrity_check(tmp_path, monkeypatch):
    live_path = tmp_path / "live.sqlite3"
    backup_path = tmp_path / "backups" / "qingshitong.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", live_path)
    db.init_db()
    with db.connect() as connection:
        connection.execute("CREATE TABLE backup_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO backup_probe(value) VALUES ('kept')")

    result = db.backup_database(backup_path)

    assert backup_path.is_file()
    assert result["backup_method"] == "sqlite_connection_backup"
    assert result["integrity_check"] == "ok"
    assert len(result["sha256"]) == 64
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("SELECT value FROM backup_probe").fetchone()[0] == "kept"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
