import http.client
import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import server.app as app
from server.analytics import MAX_ANALYTICS_BODY_BYTES
from tests.test_analytics_import import valid_payload


class ApiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        app.DATA_DIR = self.data_dir
        app.UPLOAD_DIR = self.data_dir / "uploads"
        app.CHUNK_UPLOAD_DIR = app.UPLOAD_DIR / ".chunks"
        app.DB_PATH = self.data_dir / "jobtracker.db"
        os.environ["JOBTRACKER_ANALYTICS_IMPORT_TOKEN"] = "scoped-test-token"
        app.ensure_storage()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.JobTrackerHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.environ.pop("JOBTRACKER_ANALYTICS_IMPORT_TOKEN", None)
        self.temp_dir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Length", str(len(body)))
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, response_headers, response_body

    def json_request(
        self,
        method: str,
        path: str,
        payload: Any,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        status, response_headers, response_body = self.request(
            method, path, body, request_headers
        )
        return status, response_headers, json.loads(response_body)

    def test_analytics_import_requires_token_and_returns_validation_errors(self) -> None:
        status, _, body = self.json_request(
            "POST", "/api/analytics/workflow-runs", valid_payload()
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

        invalid = valid_payload()
        invalid["postings"][0]["gaps"][0]["classification"] = "optional"
        status, _, body = self.json_request(
            "POST",
            "/api/analytics/workflow-runs",
            invalid,
            {"Authorization": "Bearer scoped-test-token"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_import")
        self.assertGreater(body["import_summary"]["rejected"], 0)
        with app.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM workflow_review_runs").fetchone()[0], 0)

    def test_import_and_read_routes_return_json_and_reimport_is_idempotent(self) -> None:
        headers = {"Authorization": "Bearer scoped-test-token"}
        first_status, _, first = self.json_request(
            "POST", "/api/analytics/workflow-runs", valid_payload(), headers
        )
        second_status, _, second = self.json_request(
            "POST", "/api/analytics/workflow-runs", valid_payload(), headers
        )
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first["import_summary"]["run"]["action"], "inserted")
        self.assertEqual(second["import_summary"]["run"]["action"], "unchanged")

        for path in (
            "/api/analytics/skill-gaps?days=30&as_of=2026-07-20",
            "/api/analytics/skill-gaps/python?days=30&as_of=2026-07-20",
            "/api/analytics/workflow-runs",
            "/api/analytics/workflow-runs/2026-07-20",
        ):
            with self.subTest(path=path):
                status, response_headers, response_body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(response_headers["content-type"], "application/json")
                self.assertIsInstance(json.loads(response_body), dict)

    def test_import_payload_body_limit_is_separate_and_enforced(self) -> None:
        status, _, response_body = self.request(
            "POST",
            "/api/analytics/workflow-runs",
            None,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer scoped-test-token",
                "Content-Length": str(MAX_ANALYTICS_BODY_BYTES + 1),
            },
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(response_body)["error"]["code"], "payload_too_large")

    def test_legacy_state_and_attachment_apis_remain_compatible(self) -> None:
        state = {
            "jobs": [
                {
                    "id": "job-1",
                    "company": "Acme",
                    "title": "Backend Engineer",
                    "requisitionId": "REQ-123",
                    "jobUrl": "https://jobs.example.com/acme/123?source=jobagent",
                    "interviews": [{"id": "int-1", "outcome": "Pending"}],
                    "updatedAt": "2026-07-01T00:00:00Z",
                }
            ],
            "taskDays": {
                "2026-07-20": {
                    "task-1": {"done": True, "notes": "keep", "title": "Follow up"}
                }
            },
            "selectedJobId": "job-1",
            "selectedTaskDate": "2026-07-20",
        }
        status, _, saved = self.json_request("PUT", "/api/state", state)
        self.assertEqual(status, 200)
        self.assertEqual(saved["state"]["jobs"][0]["interviews"][0]["id"], "int-1")
        with app.connect_db() as db:
            exact_job_json = db.execute("SELECT data FROM jobs WHERE id = 'job-1'").fetchone()[0]

        boundary = "----jobtracker-test-boundary"
        file_bytes = b"representative resume bytes\x00\xff"
        multipart = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="resume.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        status, _, uploaded = self.request(
            "POST",
            "/api/jobs/job-1/attachments/resume",
            multipart,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(uploaded)["ok"])
        status, _, downloaded = self.request(
            "GET", "/api/jobs/job-1/attachments/resume"
        )
        self.assertEqual(status, 200)
        self.assertEqual(downloaded, file_bytes)

        status, _, imported = self.json_request(
            "POST",
            "/api/analytics/workflow-runs",
            valid_payload(),
            {"Authorization": "Bearer scoped-test-token"},
        )
        self.assertEqual(status, 200)
        with app.connect_db() as db:
            self.assertEqual(db.execute("SELECT data FROM jobs WHERE id = 'job-1'").fetchone()[0], exact_job_json)
            self.assertEqual(
                db.execute("SELECT tracker_application_id FROM reviewed_postings").fetchone()[0],
                "job-1",
            )

        empty_state = {
            "jobs": [],
            "taskDays": state["taskDays"],
            "selectedJobId": "",
            "selectedTaskDate": "2026-07-20",
        }
        status, _, _ = self.json_request("PUT", "/api/state", empty_state)
        self.assertEqual(status, 200)
        with app.connect_db() as db:
            self.assertIsNone(
                db.execute("SELECT tracker_application_id FROM reviewed_postings").fetchone()[0]
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM skill_gap_observations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 0)
        self.assertFalse(any(path.is_file() for path in app.UPLOAD_DIR.iterdir()))

    def test_read_route_errors_are_explicit_json(self) -> None:
        status, _, body = self.request("GET", "/api/analytics/skill-gaps?days=45")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_filter")
        status, _, body = self.request("GET", "/api/analytics/workflow-runs/2020-01-01")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_health_state_unknown_routes_and_import_transport_errors(self) -> None:
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["jobs"], [])
        for method, path in (
            ("GET", "/api/missing"),
            ("PUT", "/api/missing"),
            ("POST", "/api/missing"),
            ("DELETE", "/api/missing"),
        ):
            with self.subTest(method=method):
                status, _, _ = self.request(method, path)
                self.assertEqual(status, 404)

        status, _, body = self.request(
            "POST",
            "/api/analytics/workflow-runs",
            b"not-json",
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer scoped-test-token",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_json")
        status, _, body = self.request(
            "POST",
            "/api/analytics/workflow-runs",
            b"{}",
            {
                "Content-Type": "text/plain",
                "Authorization": "Bearer scoped-test-token",
            },
        )
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body)["error"]["code"], "unsupported_media_type")

    def test_attachment_errors_replacement_and_delete(self) -> None:
        status, _, _ = self.request("POST", "/api/jobs/job-1/attachments/resume", b"raw")
        self.assertEqual(status, 400)
        boundary = "----empty-file"
        missing_file = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"other\"\r\n\r\nnone\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        status, _, _ = self.request(
            "POST",
            "/api/jobs/job-1/attachments/resume",
            missing_file,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request("GET", "/api/jobs/job-1/attachments/resume")
        self.assertEqual(status, 404)

        def upload(content: bytes, name: str) -> Dict[str, Any]:
            upload_boundary = "----replacement"
            multipart = (
                f"--{upload_boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                "Content-Type: text/plain\r\n\r\n"
            ).encode("utf-8") + content + f"\r\n--{upload_boundary}--\r\n".encode("utf-8")
            upload_status, _, upload_body = self.request(
                "POST",
                "/api/jobs/job-1/attachments/resume",
                multipart,
                {"Content-Type": f"multipart/form-data; boundary={upload_boundary}"},
            )
            self.assertEqual(upload_status, 200)
            return json.loads(upload_body)

        first = upload(b"first", "unsafe resume?.txt")
        first_stored = next(path for path in app.UPLOAD_DIR.iterdir() if path.is_file())
        self.assertEqual(first["metadata"]["resumeFileName"], "unsafe-resume-.txt")
        second = upload(b"second", "second.txt")
        self.assertFalse(first_stored.exists())
        self.assertEqual(second["metadata"]["resumeFileSize"], 6)
        status, _, body = self.request("DELETE", "/api/jobs/job-1/attachments/resume")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["metadata"]["resumeFileName"], "")

    def test_chunked_attachment_success_and_validation_failures(self) -> None:
        def put_chunk(upload_id: str, index: int, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            status, _, body = self.request(
                "PUT",
                f"/api/jobs/job-1/attachments/resume/chunks/{upload_id}/{index}",
                encoded,
                {"Content-Type": "application/json"},
            )
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {}
            return status, parsed

        content = b"chunk-onechunk-two"
        first = content[:9]
        second = content[9:]
        status, body = put_chunk(
            "upload-ok",
            0,
            {
                "totalChunks": 2,
                "totalSize": len(content),
                "contentBase64": base64.b64encode(first).decode("ascii"),
                "finalChunk": False,
            },
        )
        self.assertEqual((status, body["received"]), (200, 0))
        status, body = put_chunk(
            "upload-ok",
            1,
            {
                "totalChunks": 2,
                "totalSize": len(content),
                "contentBase64": base64.b64encode(second).decode("ascii"),
                "finalChunk": True,
                "fileName": "chunked.pdf",
                "fileType": "application/pdf",
            },
        )
        self.assertEqual(status, 200)
        status, _, downloaded = self.request("GET", "/api/jobs/job-1/attachments/resume")
        self.assertEqual((status, downloaded), (200, content))

        cases = [
            ("bad-meta", 0, {"totalChunks": "bad", "totalSize": 1, "contentBase64": "YQ=="}),
            ("bad-index", 2, {"totalChunks": 2, "totalSize": 1, "contentBase64": "YQ=="}),
            ("too-large", 0, {"totalChunks": 1, "totalSize": app.MAX_BODY_BYTES + 1, "contentBase64": "YQ=="}),
            ("bad-base64", 0, {"totalChunks": 1, "totalSize": 1, "contentBase64": "%%%"}),
        ]
        for upload_id, index, payload in cases:
            with self.subTest(upload_id=upload_id):
                status, _ = put_chunk(upload_id, index, payload)
                self.assertIn(status, (400, 413))

        status, _ = put_chunk(
            "missing",
            1,
            {
                "totalChunks": 2,
                "totalSize": 1,
                "contentBase64": "YQ==",
                "finalChunk": True,
            },
        )
        self.assertEqual(status, 400)
        status, _ = put_chunk(
            "size-mismatch",
            0,
            {
                "totalChunks": 1,
                "totalSize": 2,
                "contentBase64": "YQ==",
                "finalChunk": True,
            },
        )
        self.assertEqual(status, 400)

        original_max = app.MAX_CHUNK_BYTES
        app.MAX_CHUNK_BYTES = 1
        try:
            status, _ = put_chunk(
                "chunk-too-large",
                0,
                {
                    "totalChunks": 1,
                    "totalSize": 2,
                    "contentBase64": base64.b64encode(b"ab").decode("ascii"),
                },
            )
            self.assertEqual(status, 413)
        finally:
            app.MAX_CHUNK_BYTES = original_max


if __name__ == "__main__":
    unittest.main()
