from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_controller.runner_store import RunnerStore, SCHEMA_VERSION


class RunnerMigrationTests(unittest.TestCase):
    def test_fresh_migration_is_idempotent_and_integrity_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            first = RunnerStore(path)
            second = RunnerStore(path)
            self.assertEqual(first.schema_version(), SCHEMA_VERSION)
            self.assertEqual(second.schema_version(), SCHEMA_VERSION)
            self.assertEqual(second.integrity(), "ok")

    def test_existing_controller_tables_and_rows_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY,state TEXT NOT NULL)")
                connection.execute("INSERT INTO jobs VALUES('legacy-job','completed')")
            store = RunnerStore(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(
                    connection.execute("SELECT state FROM jobs WHERE job_id='legacy-job'").fetchone()[0],
                    "completed",
                )
                tables = {
                    row[0]
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            self.assertIn("runner_registry", tables)
            self.assertIn("runner_tasks", tables)
            self.assertEqual(store.list_runners()["runners"], [])

    def test_unrelated_v1_singleton_table_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE remote_work_agent(id INTEGER PRIMARY KEY,state TEXT)")
                connection.execute("INSERT INTO remote_work_agent VALUES(1,'offline')")
            RunnerStore(path)
            with sqlite3.connect(path) as connection:
                row = connection.execute("SELECT id,state FROM remote_work_agent").fetchone()
            self.assertEqual(row, (1, "offline"))

    def test_schema_three_enrollment_table_adds_revocation_column_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE runner_enrollments("
                    "enrollment_id TEXT PRIMARY KEY,runner_id TEXT NOT NULL,"
                    "token_digest TEXT NOT NULL UNIQUE,request_id TEXT NOT NULL UNIQUE,"
                    "request_digest TEXT NOT NULL,expires_at TEXT NOT NULL,"
                    "claimed_at TEXT,created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO runner_enrollments VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "ENR-LEGACY",
                        "RN-" + "A" * 20,
                        "sha256:" + "b" * 64,
                        "legacy-enrollment-request",
                        "sha256:" + "c" * 64,
                        "2026-08-11T01:15:00+00:00",
                        None,
                        "2026-08-11T01:00:00+00:00",
                    ),
                )
            store = RunnerStore(path)
            with sqlite3.connect(path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(runner_enrollments)")
                }
                row = connection.execute(
                    "SELECT enrollment_id,revoked_at FROM runner_enrollments"
                ).fetchone()
            self.assertIn("revoked_at", columns)
            self.assertEqual(row, ("ENR-LEGACY", None))
            self.assertEqual(store.schema_version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
