import json
import sqlite3
import unittest
from typing import Any, Dict, List

from server.analytics import (
    get_skill_gap_detail,
    get_skill_gap_summary,
    get_workflow_run,
    list_workflow_runs,
    import_workflow_run,
)
from tests.test_analytics_import import create_database, gap, posting, valid_payload


Payload = Dict[str, Any]


def import_run(
    db: sqlite3.Connection,
    run_date: str,
    postings: List[Payload],
    sha_character: str,
    training_statuses: List[Payload] = None,
) -> None:
    payload = valid_payload(run_date)
    payload["source_sha256"] = sha_character * 64
    payload["postings"] = postings
    payload["training_statuses"] = training_statuses or []
    import_workflow_run(db, payload, f"{run_date}T12:00:00Z")


class AnalyticsReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = create_database()
        application = {
            "id": "application-1",
            "requisitionId": "REQ-SAME",
            "company": "Acme",
            "title": "Backend Engineer",
            "jobUrl": "https://jobs.example.com/same",
            "status": "Interviewing",
            "interviews": [{"id": "int-1", "outcome": "Advanced"}],
        }
        self.db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("application-1", json.dumps(application), "2026-07-01T00:00:00Z"),
        )
        self._seed_runs()

    def tearDown(self) -> None:
        self.db.close()

    def _seed_runs(self) -> None:
        import_run(
            self.db,
            "2026-06-10",
            [
                posting("old", "REQ-OLD", "https://jobs.example.com/old", gaps=[gap("python")]),
                posting("same", "REQ-SAME", "https://jobs.example.com/same", gaps=[gap("python")]),
            ],
            "1",
        )
        import_run(
            self.db,
            "2026-06-25",
            [posting("same", "REQ-SAME", "https://jobs.example.com/same", gaps=[gap("python")])],
            "2",
        )
        import_run(
            self.db,
            "2026-07-10",
            [posting("same", "REQ-SAME", "https://jobs.example.com/same", gaps=[gap("python")])],
            "3",
        )
        import_run(
            self.db,
            "2026-07-15",
            [
                posting("new", "REQ-NEW", "https://jobs.example.com/new", "Beta", "Python Engineer", [gap("python")]),
                posting("resolved", "REQ-GO", "https://jobs.example.com/go", "Beta", "Go Engineer", [gap("go", resolution="professional_evidence", active=False)]),
            ],
            "4",
        )
        import_run(
            self.db,
            "2026-07-18",
            [posting("pref", "REQ-PREF", "https://jobs.example.com/pref", "Gamma", "Data Engineer", [gap("python", "preferred")])],
            "5",
            [
                {
                    "skill_key": "python",
                    "status": "learning",
                    "target_date": "2026-09-01",
                    "notes": "Backend practice",
                    "learning_resources": ["https://training.example.com/python"],
                }
            ],
        )
        import_run(
            self.db,
            "2026-07-19",
            [posting("false", "REQ-FALSE", "https://jobs.example.com/false", gaps=[gap("python", resolution="posting_false_positive", active=False)])],
            "6",
        )
        location_posting = posting(
            "location", "REQ-LOCATION", "https://jobs.example.com/location", gaps=[gap("python")]
        )
        location_posting["source_decision"] = "do_not_apply"
        location_posting["strict_workflow_decision"] = "do_not_apply"
        location_posting["primary_blocker"] = "location"
        import_run(self.db, "2026-07-20", [location_posting], "7")

    def test_summary_counts_unique_requisitions_and_period_trend(self) -> None:
        result = get_skill_gap_summary(self.db, {"days": "30", "as_of": "2026-07-20"})
        python = next(item for item in result["skills"] if item["skill_key"] == "python")

        self.assertEqual(python["unique_affected_requisitions"], 4)
        self.assertEqual(python["required_count"], 3)
        self.assertEqual(python["preferred_count"], 1)
        self.assertEqual(python["high_fit_affected_roles"], 3)
        self.assertEqual(python["average_fit_score"], 91.5)
        self.assertEqual(python["first_observed"], "2026-06-25")
        self.assertEqual(python["last_observed"], "2026-07-20")
        self.assertEqual(python["trend"]["previous_unique_requisitions"], 2)
        self.assertEqual(python["trend"]["absolute_change"], 2)
        self.assertEqual(python["trend"]["percent_change"], 100.0)
        self.assertEqual(python["training"]["status"], "learning")

    def test_false_positives_are_excluded_from_active_but_resolutions_are_queryable(self) -> None:
        active = get_skill_gap_summary(self.db, {"days": "30", "as_of": "2026-07-20"})
        self.assertNotIn("go", {item["skill_key"] for item in active["skills"]})
        all_states = get_skill_gap_summary(
            self.db, {"days": "30", "as_of": "2026-07-20", "state": "all"}
        )
        by_skill = {item["skill_key"]: item for item in all_states["skills"]}
        self.assertEqual(by_skill["go"]["resolved_count"], 1)
        self.assertEqual(by_skill["python"]["false_positive_count"], 1)

    def test_filters_cover_decision_classification_company_title_and_training(self) -> None:
        common = {"days": "30", "as_of": "2026-07-20"}
        preferred = get_skill_gap_summary(self.db, {**common, "classification": "preferred"})
        self.assertEqual(preferred["skills"][0]["unique_affected_requisitions"], 1)
        company = get_skill_gap_summary(self.db, {**common, "company": "beta"})
        self.assertEqual(company["skills"][0]["unique_affected_requisitions"], 1)
        title = get_skill_gap_summary(self.db, {**common, "title": "data"})
        self.assertEqual(title["skills"][0]["required_count"], 0)
        decision = get_skill_gap_summary(self.db, {**common, "decision": "do_not_apply"})
        self.assertEqual(decision["skills"][0]["unique_affected_requisitions"], 1)
        training = get_skill_gap_summary(self.db, {**common, "training_status": "learning"})
        self.assertEqual([item["skill_key"] for item in training["skills"]], ["python"])

    def test_training_priority_is_explainable_and_excludes_unrelated_rejections(self) -> None:
        result = get_skill_gap_summary(self.db, {"days": "30", "as_of": "2026-07-20"})
        python = next(item for item in result["skills"] if item["skill_key"] == "python")
        priority = python["training_priority"]

        self.assertEqual(priority["eligible_affected_requisitions"], 3)
        self.assertEqual(priority["excluded_requisitions"], 1)
        self.assertEqual(priority["components"]["decision_points"], 30.0)
        self.assertEqual(priority["components"]["required_points"], 8.0)
        self.assertEqual(priority["components"]["preferred_points"], 2.0)
        self.assertEqual(priority["components"]["hard_blocker_points"], 6.0)
        self.assertEqual(priority["components"]["fit_points"], 9.15)
        self.assertEqual(priority["components"]["trend_points"], 2.0)
        self.assertEqual(priority["score"], 57.15)

    def test_skill_detail_links_postings_and_application_outcomes(self) -> None:
        detail = get_skill_gap_detail(
            self.db, "python", {"days": "30", "as_of": "2026-07-20"}
        )
        same = next(item for item in detail["affected_postings"] if item["requisition_id"] == "REQ-SAME")
        self.assertEqual(same["tracker_application_id"], "application-1")
        self.assertEqual(same["application_outcome"]["status"], "Interviewing")
        self.assertEqual(same["application_outcome"]["interview_outcomes"], ["Advanced"])
        self.assertEqual(detail["application_outcomes"]["statuses"], {"Interviewing": 1})
        self.assertEqual(same["canonical_url"], "https://jobs.example.com/same")

    def test_workflow_run_list_and_detail_include_zero_gap_postings(self) -> None:
        import_run(
            self.db,
            "2026-07-21",
            [posting("zero", "REQ-ZERO", "https://jobs.example.com/zero", gaps=[])],
            "8",
        )
        runs = list_workflow_runs(self.db)
        self.assertEqual(runs["runs"][0]["run_date"], "2026-07-21")
        self.assertEqual(runs["runs"][0]["reviewed_posting_count"], 1)
        self.assertEqual(runs["runs"][0]["gap_observation_count"], 0)

        detail = get_workflow_run(self.db, "2026-07-21")
        self.assertEqual(detail["postings"][0]["requisition_id"], "REQ-ZERO")
        self.assertEqual(detail["postings"][0]["gaps"], [])

    def test_invalid_read_filters_and_missing_resources_raise_explicit_errors(self) -> None:
        with self.assertRaises(ValueError):
            get_skill_gap_summary(self.db, {"days": "45"})
        with self.assertRaises(ValueError):
            get_skill_gap_summary(self.db, {"classification": "optional"})
        with self.assertRaises(KeyError):
            get_skill_gap_detail(self.db, "missing_skill", {"days": "30", "as_of": "2026-07-20"})
        with self.assertRaises(KeyError):
            get_workflow_run(self.db, "2020-01-01")

    def test_60_and_90_day_windows_and_additional_invalid_filters(self) -> None:
        self.assertEqual(get_skill_gap_summary(self.db, {"days": "60"})["period"]["days"], 60)
        self.assertEqual(get_skill_gap_summary(self.db, {"days": "90"})["period"]["days"], 90)
        resolved = get_skill_gap_summary(
            self.db, {"days": "30", "as_of": "2026-07-20", "state": "resolved"}
        )
        self.assertEqual([item["skill_key"] for item in resolved["skills"]], ["go"])
        for filters in (
            {"unknown": "value"},
            {"days": "many"},
            {"decision": "maybe"},
            {"state": "maybe"},
            {"training_status": "finished"},
            {"as_of": "bad-date"},
        ):
            with self.subTest(filters=filters):
                with self.assertRaises(ValueError):
                    get_skill_gap_summary(self.db, filters)
        with self.assertRaises(ValueError):
            get_skill_gap_detail(self.db, "Bad Key", {})
        with self.assertRaises(ValueError):
            get_workflow_run(self.db, "bad-date")


if __name__ == "__main__":
    unittest.main()
