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


if __name__ == "__main__":
    unittest.main()
