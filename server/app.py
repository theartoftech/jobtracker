#!/usr/bin/env python3
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import uuid
import base64
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


DATA_DIR = Path(os.environ.get("JOBTRACKER_DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
CHUNK_UPLOAD_DIR = UPLOAD_DIR / ".chunks"
DB_PATH = Path(os.environ.get("JOBTRACKER_DB_PATH", str(DATA_DIR / "jobtracker.db")))
MAX_BODY_BYTES = 50 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
ATTACHMENT_TYPES = {"resume", "coverLetter"}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CHUNK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_entries (
                date_key TEXT NOT NULL,
                task_id TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date_key, task_id)
            );
            CREATE TABLE IF NOT EXISTS attachments (
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
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        ensure_task_entry_columns(db)


def ensure_task_entry_columns(db):
    existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(task_entries)").fetchall()}
    migrations = {
        "title": "ALTER TABLE task_entries ADD COLUMN title TEXT NOT NULL DEFAULT ''",
        "detail": "ALTER TABLE task_entries ADD COLUMN detail TEXT NOT NULL DEFAULT ''",
        "start_time": "ALTER TABLE task_entries ADD COLUMN start_time TEXT NOT NULL DEFAULT ''",
        "end_time": "ALTER TABLE task_entries ADD COLUMN end_time TEXT NOT NULL DEFAULT ''",
        "custom": "ALTER TABLE task_entries ADD COLUMN custom INTEGER NOT NULL DEFAULT 0",
        "deleted": "ALTER TABLE task_entries ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
        "sort_order": "ALTER TABLE task_entries ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
    }
    for column, statement in migrations.items():
        if column not in existing_columns:
            db.execute(statement)


def connect_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def clean_string(value):
    return str(value or "").strip()


def safe_filename(filename):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", clean_string(filename)).strip(".-")
    return cleaned or "attachment"


def attachment_id(job_id, attachment_type):
    return f"{job_id}:{attachment_type}"


def safe_token(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", clean_string(value)).strip(".-")


def metadata_fields(attachment_type, row):
    prefix = "coverLetter" if attachment_type == "coverLetter" else "resume"
    return {
        f"{prefix}FileName": row["filename"] if row else "",
        f"{prefix}FileSize": int(row["size"]) if row else 0,
        f"{prefix}FileType": row["mime_type"] if row else "",
        f"{prefix}FileUpdatedAt": row["updated_at"] if row else "",
    }


def attach_metadata(db, job):
    job_id = clean_string(job.get("id"))
    if not job_id:
        return job
    rows = db.execute(
        "SELECT * FROM attachments WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    for row in rows:
        job.update(metadata_fields(row["attachment_type"], row))
    return job


def read_state():
    with connect_db() as db:
        jobs = []
        for row in db.execute("SELECT data FROM jobs ORDER BY updated_at DESC").fetchall():
            try:
                job = json.loads(row["data"])
            except json.JSONDecodeError:
                continue
            jobs.append(attach_metadata(db, job))

        task_days = {}
        for row in db.execute("SELECT * FROM task_entries ORDER BY date_key, task_id").fetchall():
            task_days.setdefault(row["date_key"], {})[row["task_id"]] = {
                "done": bool(row["done"]),
                "notes": row["notes"],
                "title": row["title"],
                "detail": row["detail"],
                "start": row["start_time"],
                "end": row["end_time"],
                "custom": bool(row["custom"]),
                "deleted": bool(row["deleted"]),
                "sortOrder": int(row["sort_order"]),
            }

        selected = db.execute(
            "SELECT value FROM settings WHERE key = 'selectedTaskDate'"
        ).fetchone()
        selected_job = db.execute(
            "SELECT value FROM settings WHERE key = 'selectedJobId'"
        ).fetchone()

    return {
        "version": 2,
        "jobs": jobs,
        "taskDays": task_days,
        "selectedJobId": selected_job["value"] if selected_job else "",
        "selectedTaskDate": selected["value"] if selected else "",
    }


def write_state(payload):
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    task_days = payload.get("taskDays") if isinstance(payload.get("taskDays"), dict) else {}
    selected_task_date = clean_string(payload.get("selectedTaskDate"))
    selected_job_id = clean_string(payload.get("selectedJobId"))
    timestamp = now_iso()

    with connect_db() as db:
        db.execute("BEGIN")
        incoming_job_ids = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = clean_string(job.get("id")) or str(uuid.uuid4())
            job["id"] = job_id
            job["updatedAt"] = clean_string(job.get("updatedAt")) or timestamp
            incoming_job_ids.add(job_id)
            db.execute(
                """
                INSERT INTO jobs (id, data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
                """,
                (job_id, json.dumps(job, separators=(",", ":")), job["updatedAt"]),
            )

        existing_ids = {row["id"] for row in db.execute("SELECT id FROM jobs").fetchall()}
        for removed_id in existing_ids - incoming_job_ids:
            remove_job_files(db, removed_id)
            db.execute("DELETE FROM attachments WHERE job_id = ?", (removed_id,))
            db.execute("DELETE FROM jobs WHERE id = ?", (removed_id,))

        db.execute("DELETE FROM task_entries")
        for date_key, tasks in task_days.items():
            if not isinstance(tasks, dict):
                continue
            for task_id, entry in tasks.items():
                if not isinstance(entry, dict):
                    continue
                db.execute(
                    """
                    INSERT INTO task_entries (
                        date_key, task_id, done, notes, title, detail, start_time, end_time, custom, deleted, sort_order, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_string(date_key),
                        clean_string(task_id),
                        1 if entry.get("done") else 0,
                        clean_string(entry.get("notes")),
                        clean_string(entry.get("title")),
                        clean_string(entry.get("detail")),
                        clean_string(entry.get("start")),
                        clean_string(entry.get("end")),
                        1 if entry.get("custom") else 0,
                        1 if entry.get("deleted") else 0,
                        int(entry.get("sortOrder") or 0),
                        timestamp,
                    ),
                )

        db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('selectedTaskDate', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (selected_task_date,),
        )
        db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('selectedJobId', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (selected_job_id,),
        )
        db.commit()


def remove_job_files(db, job_id):
    rows = db.execute("SELECT stored_name FROM attachments WHERE job_id = ?", (job_id,)).fetchall()
    for row in rows:
        remove_upload(row["stored_name"])


def remove_upload(stored_name):
    if not stored_name:
        return
    path = UPLOAD_DIR / stored_name
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        pass


def parse_attachment_path(path):
    match = re.fullmatch(r"/api/jobs/([^/]+)/attachments/([^/]+)", path)
    if not match:
        return None, None
    job_id = unquote(match.group(1))
    attachment_type = unquote(match.group(2))
    if attachment_type not in ATTACHMENT_TYPES:
        return None, None
    return job_id, attachment_type


def parse_attachment_chunk_path(path):
    match = re.fullmatch(r"/api/jobs/([^/]+)/attachments/([^/]+)/chunks/([^/]+)/([0-9]+)", path)
    if not match:
        return None, None, None, None
    job_id = unquote(match.group(1))
    attachment_type = unquote(match.group(2))
    if attachment_type not in ATTACHMENT_TYPES:
        return None, None, None, None
    upload_id = safe_token(unquote(match.group(3)))
    if not upload_id:
        return None, None, None, None
    return job_id, attachment_type, upload_id, int(match.group(4))


def save_attachment_record(db, job_id, attachment_type, original_name, stored_name, mime_type, size, timestamp):
    old_row = db.execute(
        "SELECT stored_name FROM attachments WHERE job_id = ? AND attachment_type = ?",
        (job_id, attachment_type),
    ).fetchone()
    if old_row:
        remove_upload(old_row["stored_name"])
    db.execute(
        """
        INSERT INTO attachments (id, job_id, attachment_type, filename, stored_name, mime_type, size, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, attachment_type) DO UPDATE SET
            filename = excluded.filename,
            stored_name = excluded.stored_name,
            mime_type = excluded.mime_type,
            size = excluded.size,
            updated_at = excluded.updated_at
        """,
        (
            attachment_id(job_id, attachment_type),
            job_id,
            attachment_type,
            original_name,
            stored_name,
            mime_type,
            size,
            timestamp,
        ),
    )
    return db.execute(
        "SELECT * FROM attachments WHERE job_id = ? AND attachment_type = ?",
        (job_id, attachment_type),
    ).fetchone()


class JobTrackerHandler(BaseHTTPRequestHandler):
    server_version = "JobTrackerAPI/1.0"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"ok": True})
            return
        if path == "/api/state":
            self.send_json(read_state())
            return
        job_id, attachment_type = parse_attachment_path(path)
        if job_id and attachment_type:
            self.send_attachment(job_id, attachment_type)
            return
        self.send_error(404, "Not found")

    def do_PUT(self):
        path = urlparse(self.path).path
        job_id, attachment_type, upload_id, chunk_index = parse_attachment_chunk_path(path)
        if job_id and attachment_type:
            self.receive_attachment_chunk(job_id, attachment_type, upload_id, chunk_index)
            return
        if path != "/api/state":
            self.send_error(404, "Not found")
            return
        payload = self.read_json_body()
        if payload is None:
            return
        write_state(payload)
        self.send_json({"ok": True, "state": read_state()})

    def do_POST(self):
        path = urlparse(self.path).path
        job_id, attachment_type = parse_attachment_path(path)
        if not job_id or not attachment_type:
            self.send_error(404, "Not found")
            return
        self.receive_attachment(job_id, attachment_type)

    def do_DELETE(self):
        path = urlparse(self.path).path
        job_id, attachment_type = parse_attachment_path(path)
        if not job_id or not attachment_type:
            self.send_error(404, "Not found")
            return
        with connect_db() as db:
            row = db.execute(
                "SELECT * FROM attachments WHERE job_id = ? AND attachment_type = ?",
                (job_id, attachment_type),
            ).fetchone()
            if row:
                remove_upload(row["stored_name"])
            db.execute(
                "DELETE FROM attachments WHERE job_id = ? AND attachment_type = ?",
                (job_id, attachment_type),
            )
        self.send_json({"ok": True, "metadata": metadata_fields(attachment_type, None)})

    def receive_attachment(self, job_id, attachment_type):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_error(400, "Expected multipart/form-data")
            return
        body = self.read_body()
        if body is None:
            return
        message = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )
        file_part = None
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") == "file":
                file_part = part
                break
        if file_part is None or not file_part.get_filename():
            self.send_error(400, "Missing file")
            return

        original_name = safe_filename(file_part.get_filename())
        stored_name = f"{uuid.uuid4().hex}-{attachment_type}-{original_name}"
        target = UPLOAD_DIR / stored_name
        target.write_bytes(file_part.get_payload(decode=True) or b"")
        size = target.stat().st_size
        mime_type = file_part.get_content_type() or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        timestamp = now_iso()

        with connect_db() as db:
            row = save_attachment_record(db, job_id, attachment_type, original_name, stored_name, mime_type, size, timestamp)

        self.send_json({"ok": True, "metadata": metadata_fields(attachment_type, row)})

    def receive_attachment_chunk(self, job_id, attachment_type, upload_id, chunk_index):
        payload = self.read_json_body()
        if payload is None:
            return
        try:
            total_chunks = int(payload.get("totalChunks") or 0)
            total_size = int(payload.get("totalSize") or 0)
        except (TypeError, ValueError):
            self.send_error(400, "Invalid chunk metadata")
            return
        if total_chunks < 1 or chunk_index < 0 or chunk_index >= total_chunks:
            self.send_error(400, "Invalid chunk index")
            return
        if total_size > MAX_BODY_BYTES:
            self.send_error(413, "Attachment too large")
            return

        try:
            body = base64.b64decode(clean_string(payload.get("contentBase64")), validate=True)
        except (ValueError, base64.binascii.Error):
            self.send_error(400, "Invalid chunk content")
            return
        if len(body) > MAX_CHUNK_BYTES:
            self.send_error(413, "Chunk too large")
            return

        upload_dir = CHUNK_UPLOAD_DIR / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / f"{chunk_index:08d}.part").write_bytes(body)

        final_chunk = bool(payload.get("finalChunk"))
        if not final_chunk:
            self.send_json({"ok": True, "received": chunk_index})
            return

        part_paths = [upload_dir / f"{index:08d}.part" for index in range(total_chunks)]
        missing = [index for index, path in enumerate(part_paths) if not path.exists()]
        if missing:
            self.send_error(400, f"Missing chunks: {','.join(map(str, missing[:10]))}")
            return

        original_name = safe_filename(payload.get("fileName"))
        mime_type = clean_string(payload.get("fileType")) or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        stored_name = f"{uuid.uuid4().hex}-{attachment_type}-{original_name}"
        target = UPLOAD_DIR / stored_name
        try:
            with target.open("wb") as output:
                for path in part_paths:
                    with path.open("rb") as source:
                        shutil.copyfileobj(source, output)
            size = target.stat().st_size
            if total_size and size != total_size:
                remove_upload(stored_name)
                self.send_error(400, "Assembled attachment size mismatch")
                return
            timestamp = now_iso()
            with connect_db() as db:
                row = save_attachment_record(db, job_id, attachment_type, original_name, stored_name, mime_type, size, timestamp)
            shutil.rmtree(upload_dir, ignore_errors=True)
        except OSError:
            remove_upload(stored_name)
            self.send_error(500, "Attachment could not be assembled")
            return

        self.send_json({"ok": True, "metadata": metadata_fields(attachment_type, row)})

    def send_attachment(self, job_id, attachment_type):
        with connect_db() as db:
            row = db.execute(
                "SELECT * FROM attachments WHERE job_id = ? AND attachment_type = ?",
                (job_id, attachment_type),
            ).fetchone()
        if not row:
            self.send_error(404, "Attachment not found")
            return
        path = UPLOAD_DIR / row["stored_name"]
        if not path.exists():
            self.send_error(404, "Attachment file not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", row["mime_type"])
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f"attachment; filename=\"{row['filename']}\"")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def read_json_body(self):
        body = self.read_body()
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return None

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY_BYTES:
            self.send_error(413, "Request body too large")
            return None
        return self.rfile.read(length)

    def send_json(self, payload, status=200):
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}", flush=True)


def main():
    ensure_storage()
    port = int(os.environ.get("JOBTRACKER_API_PORT", "8090"))
    server = ThreadingHTTPServer(("127.0.0.1", port), JobTrackerHandler)
    print(f"Job tracker API listening on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
