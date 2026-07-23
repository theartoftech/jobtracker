import copy
import json
import sqlite3
import unittest
from typing import Any, Dict, List

from server.analytics import (
    MAX_GAPS_PER_IMPORT,
    MAX_POSTINGS_PER_IMPORT,
    AuthenticationError,
    ImportValidationError,
    authenticate_import_token,
    canonicalize_url,
    import_workflow_run,
    validate_import_payload,
)
from server.migrations import apply_migrations


Payload = Dict[str, Any]


def create_database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE task_entries (
            date_key TEXT NOT NULL, task_id TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
            PRIMARY KEY (date_key, task_id)
        );
        CREATE TABLE attachments (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attachment_type TEXT NOT NULL,
            filename TEXT NOT NULL, stored_name TEXT NOT NULL, mime_type TEXT NOT NULL,
            size INTEGER NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (job_id, attachment_type)
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    apply_migrations(db)
    return db


def gap(
    skill_key: str = "python",
    classification: str = "required",
    resolution: str = "unanswered",
    active: bool = True,
) -> Payload:
    return {
        "skill_key": skill_key,
        "display_name": skill_key.replace("_", " ").title(),
        "raw_posting_label": f"Professional {skill_key} experience",
        "classification": classification,
        "hard_blocker": classification == "required",
        "resolution_state": resolution,
        "resolution_evidence": None,
        "active": active,
    }


def posting(
    posting_key: str = "acme-backend-123",
    requisition_id: str = "REQ-123",
    url: str = "https://jobs.example.com/acme/123?source=jobagent",
    company: str = "Acme",
    title: str = "Backend Engineer",
    gaps: List[Payload] = None,
) -> Payload:
    return {
        "posting_key": posting_key,
        "requisition_id": requisition_id,
        "canonical_url": url,
        "company": company,
        "title": title,
        "location": "Remote, US",
        "work_model": "remote",
        "source_decision": "strong_apply",
        "fit_score": 91.5,
        "strict_workflow_decision": "apply",
        "primary_blocker": "skill_gap",
        "already_applied": False,
        "gaps": gaps if gaps is not None else [gap()],
    }


def valid_payload(run_date: str = "2026-07-20") -> Payload:
    return {
        "schema_version": 1,
        "run_date": run_date,
        "source_artifact_name": f"strict-review-{run_date}.json",
        "source_sha256": "a" * 64,
        "workflow_profile_version": "resume-strict-v4",
        "postings": [posting()],
        "training_statuses": [
            {
                "skill_key": "python",
                "status": "learning",
                "target_date": "2026-09-01",
                "notes": "Backend course",
                "learning_resources": ["https://training.example.com/python"],
            }
        ],
    }


class AuthenticationTests(unittest.TestCase):
    def test_scoped_token_is_required_and_compared(self) -> None:
        authenticate_import_token("Bearer correct-token", "correct-token")
        with self.assertRaises(AuthenticationError):
            authenticate_import_token("Bearer wrong-token", "correct-token")
        with self.assertRaises(AuthenticationError):
            authenticate_import_token("correct-token", "correct-token")
        with self.assertRaises(AuthenticationError):
            authenticate_import_token("Bearer anything", "")


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = create_database()

    def tearDown(self) -> None:
        self.db.close()

    def test_valid_import_inserts_complete_batch(self) -> None:
        summary = import_workflow_run(self.db, valid_payload(), "2026-07-20T15:00:00Z")

        self.assertEqual(summary["run"]["action"], "inserted")
        self.assertEqual(summary["postings"], {"inserted": 1, "updated": 0, "unchanged": 0, "rejected": 0})
        self.assertEqual(summary["observations"]["inserted"], 1)
        self.assertEqual(summary["training_statuses"]["inserted"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_review_runs").fetchone()[0], 1)
        row = self.db.execute("SELECT * FROM reviewed_postings").fetchone()
        self.assertEqual(row["requisition_id"], "REQ-123")
        self.assertEqual(row["canonical_url"], "https://jobs.example.com/acme/123?source=jobagent")
        self.assertEqual(row["first_observed"], "2026-07-20")
        self.assertEqual(row["last_observed"], "2026-07-20")

    def test_exact_same_day_reimport_is_idempotent(self) -> None:
        payload = valid_payload()
        first = import_workflow_run(self.db, payload, "2026-07-20T15:00:00Z")
        second = import_workflow_run(self.db, payload, "2026-07-20T16:00:00Z")

        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual(second["run"]["action"], "unchanged")
        self.assertEqual(second["postings"]["unchanged"], 1)
        self.assertEqual(second["observations"]["unchanged"], 1)
        self.assertEqual(second["training_statuses"]["unchanged"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_review_runs").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviewed_postings").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM skill_gap_observations").fetchone()[0], 1)

    def test_same_day_changed_run_updates_and_removes_stale_observations(self) -> None:
        original = valid_payload()
        import_workflow_run(self.db, original, "2026-07-20T15:00:00Z")
        changed = copy.deepcopy(original)
        changed["source_sha256"] = "b" * 64
        changed["postings"][0]["fit_score"] = 94
        changed["postings"][0]["gaps"] = [gap("kafka", "preferred")]

        summary = import_workflow_run(self.db, changed, "2026-07-20T16:00:00Z")

        self.assertEqual(summary["run"]["action"], "updated")
        self.assertEqual(summary["postings"]["updated"], 1)
        self.assertEqual(summary["observations"]["inserted"], 1)
        self.assertEqual(summary["observations"]["removed"], 1)
        rows = self.db.execute("SELECT skill_key FROM skill_gap_observations").fetchall()
        self.assertEqual([row[0] for row in rows], ["kafka"])

    def test_postings_deduplicate_by_requisition_then_canonical_url(self) -> None:
        first = valid_payload("2026-07-20")
        import_workflow_run(self.db, first, "2026-07-20T15:00:00Z")

        by_requisition = valid_payload("2026-07-21")
        by_requisition["postings"][0]["canonical_url"] = "https://jobs.example.com/acme/new-url"
        by_requisition["source_sha256"] = "b" * 64
        import_workflow_run(self.db, by_requisition, "2026-07-21T15:00:00Z")

        by_url = valid_payload("2026-07-22")
        by_url["postings"][0]["requisition_id"] = ""
        by_url["postings"][0]["canonical_url"] = "https://jobs.example.com/acme/new-url"
        by_url["source_sha256"] = "c" * 64
        import_workflow_run(self.db, by_url, "2026-07-22T15:00:00Z")

        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviewed_postings").fetchone()[0], 1)
        row = self.db.execute("SELECT requisition_id, first_observed, last_observed FROM reviewed_postings").fetchone()
        self.assertEqual(tuple(row), ("REQ-123", "2026-07-20", "2026-07-22"))

    def test_different_requisition_ids_remain_separate_even_with_same_url(self) -> None:
        payload = valid_payload()
        payload["postings"] = [
            posting(requisition_id="REQ-1", posting_key="one"),
            posting(requisition_id="REQ-2", posting_key="two"),
        ]
        import_workflow_run(self.db, payload, "2026-07-20T15:00:00Z")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviewed_postings").fetchone()[0], 2)

    def test_application_linkage_order_and_requisition_safety(self) -> None:
        jobs = [
            {"id": "job-by-req", "requisitionId": "REQ-123", "jobUrl": "https://wrong.example/1", "company": "Acme", "title": "Backend Engineer"},
            {"id": "job-other-req", "requisitionId": "REQ-999", "jobUrl": "https://jobs.example.com/acme/123?source=jobagent", "company": "Acme", "title": "Backend Engineer"},
            {"id": "job-no-req", "jobUrl": "https://jobs.example.com/no-req", "company": "Other", "title": "Platform Engineer"},
            {"id": "job-company-title", "company": "Fallback Co", "title": "Developer"},
        ]
        for job in jobs:
            self.db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?)",
                (job["id"], json.dumps(job, separators=(",", ":")), "2026-07-01T00:00:00Z"),
            )

        payload = valid_payload()
        payload["postings"] = [
            posting(),
            posting("no-req", "", "https://jobs.example.com/no-req", "Other", "Platform Engineer"),
            posting("fallback", "", "https://jobs.example.com/unmatched", "Fallback Co", "Developer"),
            posting("must-not-fallback", "REQ-X", "https://jobs.example.com/unmatched-2", "Fallback Co", "Developer"),
        ]
        import_workflow_run(self.db, payload, "2026-07-20T15:00:00Z")
        links = {
            row["posting_key"]: row["tracker_application_id"]
            for row in self.db.execute("SELECT posting_key, tracker_application_id FROM reviewed_postings")
        }
        self.assertEqual(links["acme-backend-123"], "job-by-req")
        self.assertEqual(links["no-req"], "job-no-req")
        self.assertEqual(links["fallback"], "job-company-title")
        self.assertIsNone(links["must-not-fallback"])

    def test_invalid_payloads_are_rejected_before_any_write(self) -> None:
        mutations = {
            "unknown schema": ("schema_version", 99),
            "malformed date": ("run_date", "07/20/2026"),
            "malformed URL": ("postings.0.canonical_url", "javascript:alert(1)"),
            "classification": ("postings.0.gaps.0.classification", "optional"),
            "resolution": ("postings.0.gaps.0.resolution_state", "made_up"),
            "unknown field": ("postings.0.unexpected", "ignored?"),
        }
        for label, (path, value) in mutations.items():
            with self.subTest(label=label):
                payload = valid_payload()
                target: Any = payload
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target[int(part)] if part.isdigit() else target[part]
                target[parts[-1]] = value
                with self.assertRaises(ImportValidationError) as context:
                    import_workflow_run(self.db, payload, "2026-07-20T15:00:00Z")
                self.assertGreaterEqual(context.exception.rejected_count, 1)
                self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_review_runs").fetchone()[0], 0)

    def test_one_invalid_record_rolls_back_the_complete_batch(self) -> None:
        payload = valid_payload()
        payload["postings"].append(posting("bad", "REQ-BAD", "not-a-url"))
        with self.assertRaises(ImportValidationError):
            import_workflow_run(self.db, payload, "2026-07-20T15:00:00Z")
        for table in ("workflow_review_runs", "reviewed_postings", "skill_gap_observations", "skill_training_status"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_false_positive_cannot_be_active(self) -> None:
        payload = valid_payload()
        payload["postings"][0]["gaps"] = [gap("python", resolution="posting_false_positive", active=True)]
        with self.assertRaises(ImportValidationError):
            import_workflow_run(self.db, payload, "2026-07-20T15:00:00Z")

    def test_record_count_limits_are_enforced(self) -> None:
        too_many_postings = valid_payload()
        too_many_postings["postings"] = [
            posting(str(index), f"REQ-{index}", f"https://example.com/{index}", gaps=[])
            for index in range(MAX_POSTINGS_PER_IMPORT + 1)
        ]
        with self.assertRaises(ImportValidationError):
            import_workflow_run(self.db, too_many_postings, "2026-07-20T15:00:00Z")

        too_many_gaps = valid_payload()
        too_many_gaps["postings"][0]["gaps"] = [
            gap(f"skill_{index}") for index in range(MAX_GAPS_PER_IMPORT + 1)
        ]
        with self.assertRaises(ImportValidationError):
            import_workflow_run(self.db, too_many_gaps, "2026-07-20T15:00:00Z")

    def test_validation_edge_cases_are_explicit(self) -> None:
        with self.assertRaises(ImportValidationError):
            validate_import_payload([])

        invalid_roots = []
        for field, value in (
            ("schema_version", True),
            ("run_date", None),
            ("source_artifact_name", 3),
            ("source_sha256", "xyz"),
            ("workflow_profile_version", "x" * 161),
            ("postings", "not-an-array"),
            ("training_statuses", "not-an-array"),
        ):
            payload = valid_payload()
            payload[field] = value
            invalid_roots.append(payload)

        malformed_posting = valid_payload()
        malformed_posting["postings"] = ["not-an-object"]
        invalid_roots.append(malformed_posting)
        malformed_gap_list = valid_payload()
        malformed_gap_list["postings"][0]["gaps"] = "not-an-array"
        invalid_roots.append(malformed_gap_list)
        malformed_gap = valid_payload()
        malformed_gap["postings"][0]["gaps"] = ["not-an-object"]
        invalid_roots.append(malformed_gap)
        duplicate_gap = valid_payload()
        duplicate_gap["postings"][0]["gaps"] = [gap(), gap()]
        invalid_roots.append(duplicate_gap)
        invalid_fields = valid_payload()
        invalid_fields["postings"][0].update(
            {
                "location": "x" * 301,
                "work_model": "somewhere",
                "fit_score": True,
                "already_applied": "yes",
            }
        )
        invalid_fields["postings"][0]["gaps"][0].update(
            {
                "skill_key": "Not Canonical",
                "display_name": "",
                "hard_blocker": 1,
                "active": "yes",
                "resolution_evidence": 42,
            }
        )
        invalid_roots.append(invalid_fields)
        out_of_range = valid_payload()
        out_of_range["postings"][0]["fit_score"] = 101
        invalid_roots.append(out_of_range)
        duplicate_req = valid_payload()
        duplicate_req["postings"].append(posting("duplicate", "REQ-123", "https://example.com/other"))
        invalid_roots.append(duplicate_req)
        duplicate_url = valid_payload()
        duplicate_url["postings"] = [
            posting("one", "", "https://example.com/same"),
            posting("two", "", "https://example.com/same"),
        ]
        invalid_roots.append(duplicate_url)
        malformed_training = valid_payload()
        malformed_training["training_statuses"] = ["not-an-object"]
        invalid_roots.append(malformed_training)
        invalid_training_fields = valid_payload()
        invalid_training_fields["training_statuses"][0].update(
            {
                "skill_key": "Bad Key",
                "status": "finished",
                "target_date": None,
                "notes": 2,
                "learning_resources": [2],
            }
        )
        invalid_roots.append(invalid_training_fields)
        invalid_resources = valid_payload()
        invalid_resources["training_statuses"][0]["learning_resources"] = [
            "https://example.com/resource" for _ in range(51)
        ]
        invalid_roots.append(invalid_resources)
        duplicate_training = valid_payload()
        duplicate_training["training_statuses"].append(
            copy.deepcopy(duplicate_training["training_statuses"][0])
        )
        invalid_roots.append(duplicate_training)

        for index, payload in enumerate(invalid_roots):
            with self.subTest(index=index):
                with self.assertRaises(ImportValidationError):
                    validate_import_payload(payload)

        errors: List[str] = []
        self.assertEqual(
            canonicalize_url("HTTPS://EXAMPLE.COM:443/path/#fragment", "url", errors),
            "https://example.com/path",
        )
        self.assertEqual(
            canonicalize_url("http://[::1]:8080/", "url", errors),
            "http://[::1]:8080/",
        )
        self.assertEqual(canonicalize_url("http://example.com:bad", "url", errors), "")
        self.assertTrue(errors)

    def test_database_error_rolls_back_run_and_all_records(self) -> None:
        self.db.execute(
            """
            CREATE TRIGGER reject_reviewed_posting BEFORE INSERT ON reviewed_postings
            BEGIN SELECT RAISE(ABORT, 'forced test failure'); END
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            import_workflow_run(self.db, valid_payload(), "2026-07-20T15:00:00Z")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_review_runs").fetchone()[0], 0)

    def test_ambiguous_url_without_requisition_rolls_back_new_run(self) -> None:
        payload = valid_payload()
        payload["postings"] = [
            posting("one", "REQ-1", "https://example.com/shared"),
            posting("two", "REQ-2", "https://example.com/shared"),
        ]
        import_workflow_run(self.db, payload, "2026-07-20T15:00:00Z")
        ambiguous = valid_payload("2026-07-21")
        ambiguous["source_sha256"] = "b" * 64
        ambiguous["postings"] = [posting("missing", "", "https://example.com/shared")]
        with self.assertRaises(ImportValidationError):
            import_workflow_run(self.db, ambiguous, "2026-07-21T15:00:00Z")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM workflow_review_runs").fetchone()[0], 1)

    def test_changed_training_and_removed_posting_appearance_are_updated(self) -> None:
        payload = valid_payload()
        import_workflow_run(self.db, payload, "2026-07-20T15:00:00Z")
        changed = copy.deepcopy(payload)
        changed["source_sha256"] = "b" * 64
        changed["postings"] = [posting("replacement", "REQ-NEW", "https://example.com/new")]
        changed["training_statuses"][0]["status"] = "evidence_developed"
        summary = import_workflow_run(self.db, changed, "2026-07-20T16:00:00Z")
        self.assertEqual(summary["posting_appearances"]["removed"], 1)
        self.assertEqual(summary["training_statuses"]["updated"], 1)
        self.assertEqual(
            self.db.execute("SELECT status FROM skill_training_status").fetchone()[0],
            "evidence_developed",
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM skill_gap_observations WHERE active = 1").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
