import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Tuple

from server.backup import backup_database, main as backup_main
from server.migrations import LATEST_SCHEMA_VERSION, apply_migrations


RowSnapshot = List[Tuple[object, ...]]


def create_legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE task_entries (
                date_key TEXT NOT NULL,
                task_id TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date_key, task_id)
            );
            CREATE TABLE attachments (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                attachment_type TEXT NOT NULL,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (job_id, attachment_type)
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        job_data = '{"id":"job-1","company":"Acme","title":"Engineer","interviews":[{"id":"i-1","outcome":"Pending"}]}'
        db.execute("INSERT INTO jobs VALUES (?, ?, ?)", ("job-1", job_data, "2026-06-01T00:00:00Z"))
        db.execute(
            "INSERT INTO task_entries VALUES (?, ?, ?, ?, ?)",
            ("2026-06-01", "task-1", 1, "exact notes", "2026-06-01T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-1:resume",
                "job-1",
                "resume",
                "resume.pdf",
                "stored-resume.pdf",
                "application/pdf",
                13,
                "2026-06-01T00:00:00Z",
            ),
        )
        db.execute("INSERT INTO settings VALUES (?, ?)", ("selectedJobId", "job-1"))


def snapshot_core_rows(path: Path) -> Dict[str, RowSnapshot]:
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("jobs", "task_entries", "attachments", "settings")
        }


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "jobtracker.db"
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.attachment = self.uploads / "stored-resume.pdf"
        self.attachment.write_bytes(b"resume-bytes\x00")
        create_legacy_database(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_additive_migration_preserves_core_rows_and_attachment_bytes(self) -> None:
        before_rows = snapshot_core_rows(self.db_path)
        before_hash = hashlib.sha256(self.attachment.read_bytes()).hexdigest()

        with sqlite3.connect(self.db_path) as db:
            apply_migrations(db)

        self.assertEqual(snapshot_core_rows(self.db_path), before_rows)
        self.assertEqual(hashlib.sha256(self.attachment.read_bytes()).hexdigest(), before_hash)
        with sqlite3.connect(self.db_path) as db:
            tables = {
                row[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            self.assertTrue(
                {
                    "schema_migrations",
                    "workflow_review_runs",
                    "reviewed_postings",
                    "skill_gap_observations",
                    "skill_training_status",
                }.issubset(tables)
            )
            versions = db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual(versions[-1][0], LATEST_SCHEMA_VERSION)

    def test_migrations_are_repeatable(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            apply_migrations(db)
            first_schema = db.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            apply_migrations(db)
            second_schema = db.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            versions = db.execute("SELECT version, COUNT(*) FROM schema_migrations GROUP BY version").fetchall()
        self.assertEqual(first_schema, second_schema)
        self.assertTrue(all(count == 1 for _, count in versions))

    def test_reviewed_posting_application_link_uses_on_delete_set_null(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA foreign_keys = ON")
            apply_migrations(db)
            db.execute(
                """
                INSERT INTO reviewed_postings (
                    id, posting_key, canonical_url, company, title, source_decision,
                    fit_score, strict_workflow_decision, primary_blocker,
                    first_observed, last_observed, tracker_application_id, already_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "posting-1",
                    "key-1",
                    "https://example.com/jobs/1",
                    "Acme",
                    "Engineer",
                    "apply",
                    90,
                    "apply",
                    "none",
                    "2026-06-01",
                    "2026-06-01",
                    "job-1",
                    1,
                ),
            )
            db.execute("DELETE FROM jobs WHERE id = 'job-1'")
            linked_id = db.execute(
                "SELECT tracker_application_id FROM reviewed_postings WHERE id = 'posting-1'"
            ).fetchone()[0]
        self.assertIsNone(linked_id)

    def test_previous_server_deletion_clears_link_even_with_foreign_keys_disabled(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            apply_migrations(db)
            db.execute(
                """
                INSERT INTO reviewed_postings (
                    id, posting_key, canonical_url, company, title, source_decision,
                    fit_score, strict_workflow_decision, primary_blocker,
                    first_observed, last_observed, tracker_application_id, already_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "posting-old-server",
                    "key-old-server",
                    "https://example.com/jobs/old-server",
                    "Acme",
                    "Engineer",
                    "apply",
                    90,
                    "apply",
                    "none",
                    "2026-06-01",
                    "2026-06-01",
                    "job-1",
                    1,
                ),
            )
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            db.execute("DELETE FROM jobs WHERE id = 'job-1'")
            linked_id = db.execute(
                "SELECT tracker_application_id FROM reviewed_postings WHERE id = 'posting-old-server'"
            ).fetchone()[0]
        self.assertIsNone(linked_id)

    def test_sqlite_backup_captures_wal_state_and_passes_integrity_check(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            apply_migrations(db)
            db.execute("INSERT INTO settings VALUES (?, ?)", ("wal-only", "included"))

        backup_path = self.root / "backup" / "jobtracker-copy.db"
        result = backup_database(self.db_path, backup_path)

        self.assertEqual(result.integrity_check, "ok")
        self.assertEqual(result.destination, backup_path)
        with sqlite3.connect(backup_path) as db:
            self.assertEqual(
                db.execute("SELECT value FROM settings WHERE key = 'wal-only'").fetchone()[0],
                "included",
            )
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backup_rejects_missing_same_and_existing_destinations(self) -> None:
        with self.assertRaises(FileNotFoundError):
            backup_database(self.root / "missing.db", self.root / "backup.db")
        with self.assertRaises(ValueError):
            backup_database(self.db_path, self.db_path)
        destination = self.root / "existing.db"
        destination.write_bytes(b"exists")
        with self.assertRaises(FileExistsError):
            backup_database(self.db_path, destination)

    def test_backup_command_reports_verified_result(self) -> None:
        destination = self.root / "command-backup.db"
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = backup_main([str(self.db_path), str(destination)])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["integrity_check"], "ok")
        self.assertEqual(result["destination"], str(destination))

    def test_unknown_migration_version_fails_explicitly(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
            )
            db.execute("INSERT INTO schema_migrations VALUES (999, 'future', '2026-01-01T00:00:00Z')")
            with self.assertRaises(RuntimeError):
                apply_migrations(db)


if __name__ == "__main__":
    unittest.main()
