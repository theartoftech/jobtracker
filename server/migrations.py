"""Explicit, additive SQLite schema migrations for Job Tracker."""

import sqlite3
from typing import Callable, Dict, Sequence


LATEST_SCHEMA_VERSION = 1


def _migration_001_analytics(db: sqlite3.Connection) -> None:
    statements: Sequence[str] = (
        """
        CREATE TABLE workflow_review_runs (
            id TEXT PRIMARY KEY,
            run_date TEXT NOT NULL UNIQUE,
            imported_at TEXT NOT NULL,
            source_artifact_name TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            import_schema_version INTEGER NOT NULL,
            workflow_profile_version TEXT,
            reviewed_posting_count INTEGER NOT NULL CHECK (reviewed_posting_count >= 0)
        )
        """,
        """
        CREATE TABLE reviewed_postings (
            id TEXT PRIMARY KEY,
            posting_key TEXT NOT NULL,
            requisition_id TEXT,
            canonical_url TEXT NOT NULL,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            location TEXT,
            work_model TEXT,
            source_decision TEXT NOT NULL,
            fit_score REAL NOT NULL CHECK (fit_score >= 0 AND fit_score <= 100),
            strict_workflow_decision TEXT NOT NULL,
            primary_blocker TEXT NOT NULL DEFAULT 'none',
            first_observed TEXT NOT NULL,
            last_observed TEXT NOT NULL,
            tracker_application_id TEXT,
            already_applied INTEGER NOT NULL DEFAULT 0 CHECK (already_applied IN (0, 1)),
            FOREIGN KEY (tracker_application_id) REFERENCES jobs(id) ON DELETE SET NULL
        )
        """,
        """
        CREATE UNIQUE INDEX reviewed_postings_requisition_uidx
        ON reviewed_postings(requisition_id)
        WHERE requisition_id IS NOT NULL AND requisition_id <> ''
        """,
        """
        CREATE INDEX reviewed_postings_url_idx ON reviewed_postings(canonical_url)
        """,
        """
        CREATE INDEX reviewed_postings_application_idx ON reviewed_postings(tracker_application_id)
        """,
        """
        CREATE TRIGGER clear_reviewed_posting_application_link
        AFTER DELETE ON jobs
        BEGIN
            UPDATE reviewed_postings
            SET tracker_application_id = NULL
            WHERE tracker_application_id = OLD.id;
        END
        """,
        """
        CREATE TABLE reviewed_posting_runs (
            review_run_id TEXT NOT NULL,
            posting_id TEXT NOT NULL,
            source_decision TEXT NOT NULL,
            fit_score REAL NOT NULL CHECK (fit_score >= 0 AND fit_score <= 100),
            strict_workflow_decision TEXT NOT NULL,
            primary_blocker TEXT NOT NULL,
            already_applied INTEGER NOT NULL CHECK (already_applied IN (0, 1)),
            PRIMARY KEY (review_run_id, posting_id),
            FOREIGN KEY (review_run_id) REFERENCES workflow_review_runs(id),
            FOREIGN KEY (posting_id) REFERENCES reviewed_postings(id)
        )
        """,
        """
        CREATE INDEX reviewed_posting_runs_posting_idx ON reviewed_posting_runs(posting_id)
        """,
        """
        CREATE TABLE skill_gap_observations (
            id TEXT PRIMARY KEY,
            posting_id TEXT NOT NULL,
            skill_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            raw_posting_label TEXT NOT NULL,
            gap_classification TEXT NOT NULL CHECK (gap_classification IN ('required', 'preferred')),
            hard_blocker INTEGER NOT NULL DEFAULT 0 CHECK (hard_blocker IN (0, 1)),
            resolution_state TEXT NOT NULL CHECK (
                resolution_state IN (
                    'unanswered', 'professional_evidence', 'limited_exposure',
                    'do_not_claim', 'posting_false_positive'
                )
            ),
            resolution_evidence TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            first_observed TEXT NOT NULL,
            last_observed TEXT NOT NULL,
            source_review_run_id TEXT NOT NULL,
            FOREIGN KEY (posting_id) REFERENCES reviewed_postings(id),
            FOREIGN KEY (source_review_run_id) REFERENCES workflow_review_runs(id),
            UNIQUE (posting_id, skill_key, gap_classification, source_review_run_id)
        )
        """,
        """
        CREATE INDEX skill_gap_observations_skill_idx
        ON skill_gap_observations(skill_key, active, last_observed)
        """,
        """
        CREATE INDEX skill_gap_observations_run_idx
        ON skill_gap_observations(source_review_run_id)
        """,
        """
        CREATE TABLE skill_training_status (
            skill_key TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (
                status IN (
                    'not_evaluated', 'not_pursuing', 'planned', 'learning',
                    'practicing', 'evidence_developed'
                )
            ),
            target_date TEXT,
            notes TEXT,
            learning_resources TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        db.execute(statement)


MIGRATIONS: Dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migration_001_analytics,
}


def apply_migrations(db: sqlite3.Connection) -> None:
    """Apply all pending migrations once, without changing existing tables."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )
    applied = {int(row[0]) for row in db.execute("SELECT version FROM schema_migrations").fetchall()}
    unknown = applied - set(MIGRATIONS)
    if unknown:
        raise RuntimeError(f"Database contains unknown schema migration versions: {sorted(unknown)}")

    for version in sorted(MIGRATIONS):
        if version in applied:
            continue
        with db:
            MIGRATIONS[version](db)
            db.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, "skill-gap analytics backend"),
            )
