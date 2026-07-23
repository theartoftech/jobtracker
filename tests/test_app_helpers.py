import json
import tempfile
import unittest
from pathlib import Path

import server.app as app


class AppHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        app.DATA_DIR = self.root
        app.UPLOAD_DIR = self.root / "uploads"
        app.CHUNK_UPLOAD_DIR = app.UPLOAD_DIR / ".chunks"
        app.DB_PATH = self.root / "jobtracker.db"
        app.ensure_storage()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_path_and_filename_helpers_reject_invalid_values(self) -> None:
        self.assertEqual(app.clean_string(None), "")
        self.assertEqual(app.safe_filename(" ??? "), "attachment")
        self.assertEqual(app.safe_token("../token value"), "token-value")
        self.assertEqual(app.attachment_id("job", "resume"), "job:resume")
        self.assertEqual(app.parse_attachment_path("/wrong"), (None, None))
        self.assertEqual(
            app.parse_attachment_path("/api/jobs/job/attachments/unknown"), (None, None)
        )
        self.assertEqual(app.parse_attachment_chunk_path("/wrong"), (None, None, None, None))
        self.assertEqual(
            app.parse_attachment_chunk_path("/api/jobs/job/attachments/unknown/chunks/id/0"),
            (None, None, None, None),
        )

    def test_state_tolerates_malformed_legacy_entries_without_touching_analytics(self) -> None:
        app.write_state(
            {
                "jobs": ["bad", {"company": "Generated ID"}],
                "taskDays": {"bad": "not-an-object", "day": {"bad": "not-an-entry"}},
            }
        )
        with app.connect_db() as db:
            generated = db.execute("SELECT data FROM jobs").fetchone()[0]
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?)",
                ("malformed", "{not-json", "2026-01-01T00:00:00Z"),
            )
        state = app.read_state()
        self.assertEqual(len(state["jobs"]), 1)
        self.assertEqual(json.loads(generated)["company"], "Generated ID")
        self.assertEqual(state["selectedJobId"], "")

    def test_remove_upload_handles_empty_and_missing_names(self) -> None:
        app.remove_upload("")
        app.remove_upload("missing.txt")
        self.assertFalse((app.UPLOAD_DIR / "missing.txt").exists())


if __name__ == "__main__":
    unittest.main()
