"""Validation and persistence for versioned skill-gap workflow imports."""

import datetime as dt
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit, urlunsplit


IMPORT_SCHEMA_VERSION = 1
MAX_ANALYTICS_BODY_BYTES = 2 * 1024 * 1024
MAX_POSTINGS_PER_IMPORT = 500
MAX_GAPS_PER_IMPORT = 5000
MAX_TRAINING_STATUSES_PER_IMPORT = 500

DECISIONS = {"strong_apply", "apply", "moderate_stretch", "do_not_apply"}
WORK_MODELS = {"remote", "hybrid", "onsite", "unspecified"}
PRIMARY_BLOCKERS = {
    "none",
    "skill_gap",
    "location",
    "clearance",
    "compensation",
    "employment_authorization",
    "unrelated",
}
CLASSIFICATIONS = {"required", "preferred"}
RESOLUTION_STATES = {
    "unanswered",
    "professional_evidence",
    "limited_exposure",
    "do_not_claim",
    "posting_false_positive",
}
TRAINING_STATUSES = {
    "not_evaluated",
    "not_pursuing",
    "planned",
    "learning",
    "practicing",
    "evidence_developed",
}
SKILL_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._+\-]{0,79}$")
SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")

ROOT_FIELDS = {
    "schema_version",
    "run_date",
    "source_artifact_name",
    "source_sha256",
    "workflow_profile_version",
    "postings",
    "training_statuses",
}
POSTING_FIELDS = {
    "posting_key",
    "requisition_id",
    "canonical_url",
    "company",
    "title",
    "location",
    "work_model",
    "source_decision",
    "fit_score",
    "strict_workflow_decision",
    "primary_blocker",
    "already_applied",
    "gaps",
}
GAP_FIELDS = {
    "skill_key",
    "display_name",
    "raw_posting_label",
    "classification",
    "hard_blocker",
    "resolution_state",
    "resolution_evidence",
    "active",
}
TRAINING_FIELDS = {
    "skill_key",
    "status",
    "target_date",
    "notes",
    "learning_resources",
}


class AuthenticationError(Exception):
    """Raised when the analytics import token is missing or invalid."""


class ImportValidationError(Exception):
    """Raised with every validation error found before persistence."""

    def __init__(self, errors: Sequence[str], rejected_count: int = 1) -> None:
        self.errors = list(errors)
        self.rejected_count = max(1, rejected_count)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class ValidatedImport:
    schema_version: int
    run_date: str
    source_artifact_name: str
    source_sha256: str
    workflow_profile_version: Optional[str]
    postings: List[Dict[str, Any]]
    training_statuses: List[Dict[str, Any]]


def authenticate_import_token(authorization: str, configured_token: str) -> None:
    """Require an exact Bearer token using a timing-safe comparison."""
    if not configured_token:
        raise AuthenticationError("Analytics import token is not configured")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise AuthenticationError("A Bearer analytics import token is required")
    provided = authorization[len(prefix) :]
    if not provided or not hmac.compare_digest(provided.encode("utf-8"), configured_token.encode("utf-8")):
        raise AuthenticationError("Invalid analytics import token")


def _unknown_fields(value: Mapping[str, Any], allowed: Set[str], path: str, errors: List[str]) -> None:
    for field in sorted(set(value) - allowed):
        errors.append(f"{path}.{field}: unknown field")


def _string(
    value: Any,
    path: str,
    errors: List[str],
    maximum: int,
    required: bool = True,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        errors.append(f"{path}: must be a string")
        return ""
    cleaned = value.strip()
    if required and not cleaned:
        errors.append(f"{path}: must not be empty")
    if len(cleaned) > maximum:
        errors.append(f"{path}: exceeds {maximum} characters")
    return cleaned


def _date(value: Any, path: str, errors: List[str], required: bool = True) -> Optional[str]:
    cleaned = _string(value, path, errors, 10, required)
    if not cleaned and not required:
        return None
    try:
        parsed = dt.date.fromisoformat(cleaned)
    except ValueError:
        errors.append(f"{path}: must be an ISO 8601 date (YYYY-MM-DD)")
        return None
    if parsed.isoformat() != cleaned:
        errors.append(f"{path}: must be an ISO 8601 date (YYYY-MM-DD)")
        return None
    return cleaned


def canonicalize_url(value: Any, path: str, errors: List[str]) -> str:
    cleaned = _string(value, path, errors, 2048)
    if not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError:
        errors.append(f"{path}: malformed URL")
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        errors.append(f"{path}: must be an absolute HTTP or HTTPS URL")
        return ""
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path_value = parsed.path or "/"
    if path_value != "/":
        path_value = path_value.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path_value, parsed.query, ""))


def _optional_url(value: Any, path: str, errors: List[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: must be a non-empty URL string")
        return ""
    return canonicalize_url(value, path, errors)


def _enum(value: Any, path: str, allowed: Set[str], errors: List[str]) -> str:
    cleaned = _string(value, path, errors, 80)
    if cleaned and cleaned not in allowed:
        errors.append(f"{path}: unsupported value '{cleaned}'")
    return cleaned


def _bool(value: Any, path: str, errors: List[str]) -> bool:
    if not isinstance(value, bool):
        errors.append(f"{path}: must be a boolean")
        return False
    return value


def _fit_score(value: Any, path: str, errors: List[str]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{path}: must be a number from 0 through 100")
        return 0.0
    score = float(value)
    if score < 0 or score > 100:
        errors.append(f"{path}: must be a number from 0 through 100")
    return score


def _validate_gap(value: Any, path: str, errors: List[str]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return {}
    _unknown_fields(value, GAP_FIELDS, path, errors)
    skill_key = _string(value.get("skill_key"), f"{path}.skill_key", errors, 80)
    if skill_key and not SKILL_KEY_PATTERN.fullmatch(skill_key):
        errors.append(f"{path}.skill_key: must be a lowercase canonical skill key")
    result = {
        "skill_key": skill_key,
        "display_name": _string(value.get("display_name"), f"{path}.display_name", errors, 160),
        "raw_posting_label": _string(
            value.get("raw_posting_label"), f"{path}.raw_posting_label", errors, 500
        ),
        "classification": _enum(
            value.get("classification"), f"{path}.classification", CLASSIFICATIONS, errors
        ),
        "hard_blocker": _bool(value.get("hard_blocker"), f"{path}.hard_blocker", errors),
        "resolution_state": _enum(
            value.get("resolution_state"), f"{path}.resolution_state", RESOLUTION_STATES, errors
        ),
        "resolution_evidence": None,
        "active": _bool(value.get("active"), f"{path}.active", errors),
    }
    evidence = value.get("resolution_evidence")
    if evidence is not None:
        result["resolution_evidence"] = _string(
            evidence, f"{path}.resolution_evidence", errors, 1000, required=False
        ) or None
    if result["resolution_state"] == "posting_false_positive" and result["active"]:
        errors.append(f"{path}: posting_false_positive observations must be inactive")
    return result


def _validate_posting(value: Any, path: str, errors: List[str]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return {}
    _unknown_fields(value, POSTING_FIELDS, path, errors)
    gaps_value = value.get("gaps")
    if not isinstance(gaps_value, list):
        errors.append(f"{path}.gaps: must be an array")
        gaps_value = []
    gaps = [_validate_gap(item, f"{path}.gaps.{index}", errors) for index, item in enumerate(gaps_value)]
    seen_gaps: Set[Tuple[str, str]] = set()
    for index, item in enumerate(gaps):
        identity = (str(item.get("skill_key", "")), str(item.get("classification", "")))
        if identity in seen_gaps and all(identity):
            errors.append(f"{path}.gaps.{index}: duplicate skill/classification in posting")
        seen_gaps.add(identity)

    requisition_id = _string(
        value.get("requisition_id", ""), f"{path}.requisition_id", errors, 128, required=False
    )
    location = _string(value.get("location", ""), f"{path}.location", errors, 300, required=False)
    result = {
        "posting_key": _string(value.get("posting_key"), f"{path}.posting_key", errors, 200),
        "requisition_id": requisition_id or None,
        "canonical_url": canonicalize_url(value.get("canonical_url"), f"{path}.canonical_url", errors),
        "company": _string(value.get("company"), f"{path}.company", errors, 200),
        "title": _string(value.get("title"), f"{path}.title", errors, 240),
        "location": location or None,
        "work_model": _enum(value.get("work_model"), f"{path}.work_model", WORK_MODELS, errors),
        "source_decision": _enum(
            value.get("source_decision"), f"{path}.source_decision", DECISIONS, errors
        ),
        "fit_score": _fit_score(value.get("fit_score"), f"{path}.fit_score", errors),
        "strict_workflow_decision": _enum(
            value.get("strict_workflow_decision"),
            f"{path}.strict_workflow_decision",
            DECISIONS,
            errors,
        ),
        "primary_blocker": _enum(
            value.get("primary_blocker", "none"), f"{path}.primary_blocker", PRIMARY_BLOCKERS, errors
        ),
        "already_applied": _bool(value.get("already_applied"), f"{path}.already_applied", errors),
        "gaps": gaps,
    }
    return result


def _validate_training(value: Any, path: str, errors: List[str]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return {}
    _unknown_fields(value, TRAINING_FIELDS, path, errors)
    skill_key = _string(value.get("skill_key"), f"{path}.skill_key", errors, 80)
    if skill_key and not SKILL_KEY_PATTERN.fullmatch(skill_key):
        errors.append(f"{path}.skill_key: must be a lowercase canonical skill key")
    resources_value = value.get("learning_resources", [])
    if not isinstance(resources_value, list):
        errors.append(f"{path}.learning_resources: must be an array")
        resources_value = []
    if len(resources_value) > 50:
        errors.append(f"{path}.learning_resources: exceeds 50 resources")
    resources = [
        _optional_url(item, f"{path}.learning_resources.{index}", errors)
        for index, item in enumerate(resources_value)
    ]
    notes_value = value.get("notes")
    notes = None
    if notes_value is not None:
        notes = _string(notes_value, f"{path}.notes", errors, 4000, required=False) or None
    return {
        "skill_key": skill_key,
        "status": _enum(value.get("status"), f"{path}.status", TRAINING_STATUSES, errors),
        "target_date": _date(value.get("target_date"), f"{path}.target_date", errors, required=False),
        "notes": notes,
        "learning_resources": resources,
    }


def validate_import_payload(payload: Any) -> ValidatedImport:
    errors: List[str] = []
    if not isinstance(payload, dict):
        raise ImportValidationError(["payload: must be an object"])
    _unknown_fields(payload, ROOT_FIELDS, "payload", errors)
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        errors.append("payload.schema_version: must be an integer")
        schema_version = 0
    elif schema_version != IMPORT_SCHEMA_VERSION:
        errors.append(f"payload.schema_version: unsupported version {schema_version}")

    run_date = _date(payload.get("run_date"), "payload.run_date", errors) or ""
    artifact = _string(payload.get("source_artifact_name"), "payload.source_artifact_name", errors, 255)
    sha256 = _string(payload.get("source_sha256"), "payload.source_sha256", errors, 64)
    if sha256 and not SHA256_PATTERN.fullmatch(sha256):
        errors.append("payload.source_sha256: must be a 64-character hexadecimal SHA-256")
    profile_value = payload.get("workflow_profile_version")
    profile = None
    if profile_value is not None:
        profile = _string(
            profile_value, "payload.workflow_profile_version", errors, 160, required=False
        ) or None

    postings_value = payload.get("postings")
    if not isinstance(postings_value, list):
        errors.append("payload.postings: must be an array")
        postings_value = []
    if len(postings_value) > MAX_POSTINGS_PER_IMPORT:
        errors.append(f"payload.postings: exceeds limit of {MAX_POSTINGS_PER_IMPORT}")
        postings_value = []
    postings = [
        _validate_posting(item, f"payload.postings.{index}", errors)
        for index, item in enumerate(postings_value)
    ]
    total_gaps = sum(len(item.get("gaps", [])) for item in postings)
    if total_gaps > MAX_GAPS_PER_IMPORT:
        errors.append(f"payload postings contain {total_gaps} gaps; limit is {MAX_GAPS_PER_IMPORT}")

    training_value = payload.get("training_statuses", [])
    if not isinstance(training_value, list):
        errors.append("payload.training_statuses: must be an array")
        training_value = []
    if len(training_value) > MAX_TRAINING_STATUSES_PER_IMPORT:
        errors.append(
            f"payload.training_statuses: exceeds limit of {MAX_TRAINING_STATUSES_PER_IMPORT}"
        )
        training_value = []
    training = [
        _validate_training(item, f"payload.training_statuses.{index}", errors)
        for index, item in enumerate(training_value)
    ]

    requisitions: Set[str] = set()
    no_req_urls: Set[str] = set()
    for index, item in enumerate(postings):
        requisition = item.get("requisition_id")
        url = str(item.get("canonical_url", ""))
        if requisition:
            if requisition in requisitions:
                errors.append(f"payload.postings.{index}: duplicate requisition_id '{requisition}'")
            requisitions.add(requisition)
        elif url:
            if url in no_req_urls:
                errors.append(f"payload.postings.{index}: duplicate canonical_url without requisition_id")
            no_req_urls.add(url)

    training_keys: Set[str] = set()
    for index, item in enumerate(training):
        key = str(item.get("skill_key", ""))
        if key in training_keys and key:
            errors.append(f"payload.training_statuses.{index}: duplicate skill_key '{key}'")
        training_keys.add(key)

    if errors:
        raise ImportValidationError(errors, max(1, len(errors)))
    return ValidatedImport(
        int(schema_version), run_date, artifact, sha256.lower(), profile, postings, training
    )


def _new_counter(include_removed: bool = False) -> Dict[str, int]:
    result = {"inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0}
    if include_removed:
        result["removed"] = 0
    return result


def _row_changed(row: sqlite3.Row, values: Mapping[str, Any]) -> bool:
    return any(row[key] != value for key, value in values.items())


def _load_jobs(db: sqlite3.Connection) -> List[Tuple[str, Dict[str, Any]]]:
    jobs: List[Tuple[str, Dict[str, Any]]] = []
    for row in db.execute("SELECT id, data FROM jobs ORDER BY id").fetchall():
        try:
            value = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            jobs.append((str(row["id"]), value))
    return jobs


def _job_requisition(job: Mapping[str, Any]) -> str:
    for key in ("requisitionId", "requisition_id", "jobRequisitionId"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _comparable_url(value: Any) -> str:
    errors: List[str] = []
    result = canonicalize_url(value, "job.jobUrl", errors)
    return result if not errors else ""


def _link_application(posting: Mapping[str, Any], jobs: Sequence[Tuple[str, Dict[str, Any]]]) -> Optional[str]:
    requisition = str(posting.get("requisition_id") or "")
    url = str(posting["canonical_url"])
    company = str(posting["company"]).casefold()
    title = str(posting["title"]).casefold()
    if requisition:
        matches = [job_id for job_id, job in jobs if _job_requisition(job) == requisition]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None

    url_matches = [
        job_id
        for job_id, job in jobs
        if _comparable_url(job.get("jobUrl")) == url
        and (not requisition or not _job_requisition(job) or _job_requisition(job) == requisition)
    ]
    if len(url_matches) == 1:
        return url_matches[0]
    if len(url_matches) > 1:
        return None
    if requisition:
        return None

    fallback = [
        job_id
        for job_id, job in jobs
        if not _job_requisition(job)
        and str(job.get("company") or "").strip().casefold() == company
        and str(job.get("title") or "").strip().casefold() == title
    ]
    return fallback[0] if len(fallback) == 1 else None


def _find_posting(db: sqlite3.Connection, posting: Mapping[str, Any]) -> Optional[sqlite3.Row]:
    requisition = posting.get("requisition_id")
    if requisition:
        row = db.execute(
            "SELECT * FROM reviewed_postings WHERE requisition_id = ?", (requisition,)
        ).fetchone()
        if row:
            return row
        url_rows = db.execute(
            "SELECT * FROM reviewed_postings WHERE canonical_url = ? AND requisition_id IS NULL",
            (posting["canonical_url"],),
        ).fetchall()
        return url_rows[0] if len(url_rows) == 1 else None
    rows = db.execute(
        "SELECT * FROM reviewed_postings WHERE canonical_url = ? ORDER BY first_observed, id",
        (posting["canonical_url"],),
    ).fetchall()
    if len(rows) > 1:
        raise ImportValidationError(
            [f"canonical_url is ambiguous across requisitions: {posting['canonical_url']}"]
        )
    return rows[0] if rows else None


def _upsert_posting(
    db: sqlite3.Connection,
    posting: Mapping[str, Any],
    run_date: str,
    jobs: Sequence[Tuple[str, Dict[str, Any]]],
) -> Tuple[str, str]:
    existing = _find_posting(db, posting)
    linked_id = _link_application(posting, jobs)
    posting_id = str(existing["id"]) if existing else str(uuid.uuid4())
    first_observed = min(str(existing["first_observed"]), run_date) if existing else run_date
    last_observed = max(str(existing["last_observed"]), run_date) if existing else run_date
    requisition_id = posting.get("requisition_id") or (existing["requisition_id"] if existing else None)
    values: Dict[str, Any] = {
        "posting_key": posting["posting_key"],
        "requisition_id": requisition_id,
        "canonical_url": posting["canonical_url"],
        "company": posting["company"],
        "title": posting["title"],
        "location": posting["location"],
        "work_model": posting["work_model"],
        "source_decision": posting["source_decision"],
        "fit_score": posting["fit_score"],
        "strict_workflow_decision": posting["strict_workflow_decision"],
        "primary_blocker": posting["primary_blocker"],
        "first_observed": first_observed,
        "last_observed": last_observed,
        "tracker_application_id": linked_id,
        "already_applied": 1 if posting["already_applied"] else 0,
    }
    if not existing:
        columns = ", ".join(("id", *values.keys()))
        placeholders = ", ".join("?" for _ in range(len(values) + 1))
        db.execute(
            f"INSERT INTO reviewed_postings ({columns}) VALUES ({placeholders})",
            (posting_id, *values.values()),
        )
        return posting_id, "inserted"
    if not _row_changed(existing, values):
        return posting_id, "unchanged"
    assignments = ", ".join(f"{key} = ?" for key in values)
    db.execute(
        f"UPDATE reviewed_postings SET {assignments} WHERE id = ?",
        (*values.values(), posting_id),
    )
    return posting_id, "updated"


def _upsert_observation(
    db: sqlite3.Connection,
    run_id: str,
    run_date: str,
    posting_id: str,
    gap: Mapping[str, Any],
) -> Tuple[str, str]:
    existing = db.execute(
        """
        SELECT * FROM skill_gap_observations
        WHERE posting_id = ? AND skill_key = ? AND gap_classification = ?
          AND source_review_run_id = ?
        """,
        (posting_id, gap["skill_key"], gap["classification"], run_id),
    ).fetchone()
    prior_first = db.execute(
        """
        SELECT MIN(first_observed) FROM skill_gap_observations
        WHERE posting_id = ? AND skill_key = ? AND gap_classification = ?
        """,
        (posting_id, gap["skill_key"], gap["classification"]),
    ).fetchone()[0]
    observation_id = str(existing["id"]) if existing else str(uuid.uuid4())
    values: Dict[str, Any] = {
        "posting_id": posting_id,
        "skill_key": gap["skill_key"],
        "display_name": gap["display_name"],
        "raw_posting_label": gap["raw_posting_label"],
        "gap_classification": gap["classification"],
        "hard_blocker": 1 if gap["hard_blocker"] else 0,
        "resolution_state": gap["resolution_state"],
        "resolution_evidence": gap["resolution_evidence"],
        "active": 1 if gap["active"] else 0,
        "first_observed": min(str(prior_first), run_date) if prior_first else run_date,
        "last_observed": run_date,
        "source_review_run_id": run_id,
    }
    if not existing:
        columns = ", ".join(("id", *values.keys()))
        placeholders = ", ".join("?" for _ in range(len(values) + 1))
        db.execute(
            f"INSERT INTO skill_gap_observations ({columns}) VALUES ({placeholders})",
            (observation_id, *values.values()),
        )
        return observation_id, "inserted"
    if not _row_changed(existing, values):
        return observation_id, "unchanged"
    assignments = ", ".join(f"{key} = ?" for key in values)
    db.execute(
        f"UPDATE skill_gap_observations SET {assignments} WHERE id = ?",
        (*values.values(), observation_id),
    )
    return observation_id, "updated"


def _upsert_posting_appearance(
    db: sqlite3.Connection,
    run_id: str,
    posting_id: str,
    posting: Mapping[str, Any],
) -> str:
    existing = db.execute(
        "SELECT * FROM reviewed_posting_runs WHERE review_run_id = ? AND posting_id = ?",
        (run_id, posting_id),
    ).fetchone()
    values: Dict[str, Any] = {
        "source_decision": posting["source_decision"],
        "fit_score": posting["fit_score"],
        "strict_workflow_decision": posting["strict_workflow_decision"],
        "primary_blocker": posting["primary_blocker"],
        "already_applied": 1 if posting["already_applied"] else 0,
    }
    if not existing:
        db.execute(
            """
            INSERT INTO reviewed_posting_runs (
                review_run_id, posting_id, source_decision, fit_score,
                strict_workflow_decision, primary_blocker, already_applied
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, posting_id, *values.values()),
        )
        return "inserted"
    if not _row_changed(existing, values):
        return "unchanged"
    db.execute(
        """
        UPDATE reviewed_posting_runs
        SET source_decision = ?, fit_score = ?, strict_workflow_decision = ?,
            primary_blocker = ?, already_applied = ?
        WHERE review_run_id = ? AND posting_id = ?
        """,
        (*values.values(), run_id, posting_id),
    )
    return "updated"


def _upsert_training(
    db: sqlite3.Connection, training: Mapping[str, Any], imported_at: str
) -> str:
    existing = db.execute(
        "SELECT * FROM skill_training_status WHERE skill_key = ?", (training["skill_key"],)
    ).fetchone()
    comparable = {
        "status": training["status"],
        "target_date": training["target_date"],
        "notes": training["notes"],
        "learning_resources": json.dumps(training["learning_resources"], separators=(",", ":")),
    }
    if not existing:
        db.execute(
            """
            INSERT INTO skill_training_status (
                skill_key, status, target_date, notes, learning_resources, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (training["skill_key"], *comparable.values(), imported_at),
        )
        return "inserted"
    if not _row_changed(existing, comparable):
        return "unchanged"
    db.execute(
        """
        UPDATE skill_training_status
        SET status = ?, target_date = ?, notes = ?, learning_resources = ?, updated_at = ?
        WHERE skill_key = ?
        """,
        (*comparable.values(), imported_at, training["skill_key"]),
    )
    return "updated"


def import_workflow_run(
    db: sqlite3.Connection, payload: Any, imported_at: str
) -> Dict[str, Any]:
    """Validate and atomically insert or replace one dated workflow review batch."""
    validated = validate_import_payload(payload)
    if db.row_factory is None:
        db.row_factory = sqlite3.Row
    postings_count = _new_counter()
    appearances_count = _new_counter(include_removed=True)
    observations_count = _new_counter(include_removed=True)
    training_count = _new_counter()
    db.execute("SAVEPOINT workflow_import")
    try:
        existing_run = db.execute(
            "SELECT * FROM workflow_review_runs WHERE run_date = ?", (validated.run_date,)
        ).fetchone()
        run_id = str(existing_run["id"]) if existing_run else str(uuid.uuid4())
        run_values: Dict[str, Any] = {
            "source_artifact_name": validated.source_artifact_name,
            "source_sha256": validated.source_sha256,
            "import_schema_version": validated.schema_version,
            "workflow_profile_version": validated.workflow_profile_version,
            "reviewed_posting_count": len(validated.postings),
        }
        if existing_run is None:
            db.execute(
                """
                INSERT INTO workflow_review_runs (
                    id, run_date, imported_at, source_artifact_name, source_sha256,
                    import_schema_version, workflow_profile_version, reviewed_posting_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    validated.run_date,
                    imported_at,
                    *run_values.values(),
                ),
            )
            run_action = "inserted"
        elif _row_changed(existing_run, run_values):
            db.execute(
                """
                UPDATE workflow_review_runs
                SET imported_at = ?, source_artifact_name = ?, source_sha256 = ?,
                    import_schema_version = ?, workflow_profile_version = ?, reviewed_posting_count = ?
                WHERE id = ?
                """,
                (imported_at, *run_values.values(), run_id),
            )
            run_action = "updated"
        else:
            run_action = "unchanged"

        jobs = _load_jobs(db)
        retained_observations: Set[str] = set()
        retained_postings: Set[str] = set()
        for posting_value in validated.postings:
            posting_id, action = _upsert_posting(
                db, posting_value, validated.run_date, jobs
            )
            postings_count[action] += 1
            retained_postings.add(posting_id)
            appearance_action = _upsert_posting_appearance(db, run_id, posting_id, posting_value)
            appearances_count[appearance_action] += 1
            for gap_value in posting_value["gaps"]:
                observation_id, observation_action = _upsert_observation(
                    db, run_id, validated.run_date, posting_id, gap_value
                )
                retained_observations.add(observation_id)
                observations_count[observation_action] += 1

        existing_ids = {
            str(row[0])
            for row in db.execute(
                "SELECT id FROM skill_gap_observations WHERE source_review_run_id = ?", (run_id,)
            ).fetchall()
        }
        stale_ids = existing_ids - retained_observations
        for stale_id in stale_ids:
            db.execute("DELETE FROM skill_gap_observations WHERE id = ?", (stale_id,))
        observations_count["removed"] = len(stale_ids)

        prior_posting_ids = {
            str(row[0])
            for row in db.execute(
                "SELECT posting_id FROM reviewed_posting_runs WHERE review_run_id = ?", (run_id,)
            ).fetchall()
        }
        stale_posting_ids = prior_posting_ids - retained_postings
        for stale_posting_id in stale_posting_ids:
            db.execute(
                "DELETE FROM reviewed_posting_runs WHERE review_run_id = ? AND posting_id = ?",
                (run_id, stale_posting_id),
            )
        appearances_count["removed"] = len(stale_posting_ids)

        for training_value in validated.training_statuses:
            training_action = _upsert_training(db, training_value, imported_at)
            training_count[training_action] += 1

        db.execute("RELEASE SAVEPOINT workflow_import")
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT workflow_import")
        db.execute("RELEASE SAVEPOINT workflow_import")
        raise

    totals = {
        key: postings_count.get(key, 0)
        + appearances_count.get(key, 0)
        + observations_count.get(key, 0)
        + training_count.get(key, 0)
        for key in ("inserted", "updated", "unchanged", "rejected")
    }
    totals["removed"] = observations_count["removed"] + appearances_count["removed"]
    return {
        "run": {"id": run_id, "run_date": validated.run_date, "action": run_action},
        "postings": postings_count,
        "posting_appearances": appearances_count,
        "observations": observations_count,
        "training_statuses": training_count,
        "totals": totals,
    }


def _read_date(value: str, field: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO 8601 date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO 8601 date (YYYY-MM-DD)")
    return parsed


def _validate_read_filters(
    db: sqlite3.Connection, raw_filters: Mapping[str, str]
) -> Dict[str, Any]:
    allowed = {
        "days",
        "as_of",
        "decision",
        "classification",
        "state",
        "company",
        "title",
        "training_status",
    }
    unknown = set(raw_filters) - allowed
    if unknown:
        raise ValueError(f"Unknown analytics filters: {sorted(unknown)}")
    try:
        days = int(raw_filters.get("days", "30"))
    except (TypeError, ValueError) as exc:
        raise ValueError("days must be one of 30, 60, or 90") from exc
    if days not in {30, 60, 90}:
        raise ValueError("days must be one of 30, 60, or 90")
    decision = raw_filters.get("decision")
    if decision is not None and decision not in DECISIONS:
        raise ValueError(f"Unsupported workflow decision: {decision}")
    classification = raw_filters.get("classification")
    if classification is not None and classification not in CLASSIFICATIONS:
        raise ValueError(f"Unsupported gap classification: {classification}")
    state = raw_filters.get("state", "active")
    if state not in {"active", "resolved", "all"}:
        raise ValueError("state must be active, resolved, or all")
    training_status = raw_filters.get("training_status")
    if training_status is not None and training_status not in TRAINING_STATUSES:
        raise ValueError(f"Unsupported training status: {training_status}")
    as_of_value = raw_filters.get("as_of")
    if as_of_value is None:
        row = db.execute("SELECT MAX(run_date) FROM workflow_review_runs").fetchone()
        as_of_value = str(row[0]) if row and row[0] else dt.date.today().isoformat()
    as_of = _read_date(as_of_value, "as_of")
    return {
        "days": days,
        "as_of": as_of,
        "decision": decision,
        "classification": classification,
        "state": state,
        "company": (raw_filters.get("company") or "").strip().casefold(),
        "title": (raw_filters.get("title") or "").strip().casefold(),
        "training_status": training_status,
    }


def _training_by_skill(db: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for row in db.execute("SELECT * FROM skill_training_status ORDER BY skill_key").fetchall():
        try:
            resources = json.loads(row["learning_resources"])
        except (json.JSONDecodeError, TypeError):
            resources = []
        result[str(row["skill_key"])] = {
            "status": row["status"],
            "target_date": row["target_date"],
            "notes": row["notes"],
            "learning_resources": resources if isinstance(resources, list) else [],
            "updated_at": row["updated_at"],
        }
    return result


def _observation_rows(
    db: sqlite3.Connection, start: dt.date, end: dt.date
) -> List[Dict[str, Any]]:
    rows = db.execute(
        """
        SELECT
            o.*, r.run_date, p.requisition_id, p.canonical_url, p.company, p.title,
            p.location, p.work_model, p.tracker_application_id,
            pr.source_decision AS appearance_source_decision,
            pr.fit_score AS appearance_fit_score,
            pr.strict_workflow_decision AS appearance_workflow_decision,
            pr.primary_blocker AS appearance_primary_blocker,
            pr.already_applied AS appearance_already_applied
        FROM skill_gap_observations o
        JOIN workflow_review_runs r ON r.id = o.source_review_run_id
        JOIN reviewed_postings p ON p.id = o.posting_id
        JOIN reviewed_posting_runs pr
          ON pr.review_run_id = o.source_review_run_id AND pr.posting_id = o.posting_id
        WHERE r.run_date BETWEEN ? AND ?
        ORDER BY r.run_date, o.id
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def _filtered_latest_rows(
    rows: Sequence[Dict[str, Any]], filters: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    matching: List[Dict[str, Any]] = []
    for row in rows:
        if filters["decision"] and row["appearance_workflow_decision"] != filters["decision"]:
            continue
        if filters["classification"] and row["gap_classification"] != filters["classification"]:
            continue
        if filters["company"] and filters["company"] not in str(row["company"]).casefold():
            continue
        if filters["title"] and filters["title"] not in str(row["title"]).casefold():
            continue
        matching.append(row)

    latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    earliest: Dict[Tuple[str, str, str], str] = {}
    for row in matching:
        identity = (str(row["posting_id"]), str(row["skill_key"]), str(row["gap_classification"]))
        earliest[identity] = min(earliest.get(identity, str(row["run_date"])), str(row["run_date"]))
        prior = latest.get(identity)
        if prior is None or (str(row["run_date"]), str(row["id"])) > (
            str(prior["run_date"]),
            str(prior["id"]),
        ):
            latest[identity] = row

    for identity, row in latest.items():
        row["period_first_observed"] = earliest[identity]

    state = filters["state"]
    selected: List[Dict[str, Any]] = []
    for row in latest.values():
        false_positive = row["resolution_state"] == "posting_false_positive"
        if state == "active" and (not row["active"] or false_positive):
            continue
        if state == "resolved" and (row["active"] or false_positive):
            continue
        selected.append(row)
    return selected


def _unique_posting_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        posting_id = str(row["posting_id"])
        prior = latest.get(posting_id)
        if prior is None or (str(row["run_date"]), str(row["id"])) > (
            str(prior["run_date"]),
            str(prior["id"]),
        ):
            latest[posting_id] = row
    return latest


def _priority(
    current_rows: Sequence[Dict[str, Any]], previous_rows: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    eligible_decisions = {"strong_apply": 12.0, "apply": 10.0, "moderate_stretch": 6.0}
    excluded_blockers = {"location", "clearance", "unrelated"}
    current_postings = _unique_posting_rows(current_rows)
    previous_postings = _unique_posting_rows(previous_rows)
    eligible = {
        posting_id: row
        for posting_id, row in current_postings.items()
        if row["appearance_workflow_decision"] in eligible_decisions
        and row["appearance_primary_blocker"] not in excluded_blockers
    }
    previous_eligible = {
        posting_id: row
        for posting_id, row in previous_postings.items()
        if row["appearance_workflow_decision"] in eligible_decisions
        and row["appearance_primary_blocker"] not in excluded_blockers
    }
    eligible_rows = [row for row in current_rows if str(row["posting_id"]) in eligible]
    required = len({str(row["posting_id"]) for row in eligible_rows if row["gap_classification"] == "required"})
    preferred = len({str(row["posting_id"]) for row in eligible_rows if row["gap_classification"] == "preferred"})
    hard_blockers = len({str(row["posting_id"]) for row in eligible_rows if row["hard_blocker"]})
    decision_points = sum(
        eligible_decisions[str(row["appearance_workflow_decision"])] for row in eligible.values()
    )
    average_fit = (
        sum(float(row["appearance_fit_score"]) for row in eligible.values()) / len(eligible)
        if eligible
        else 0.0
    )
    delta = len(eligible) - len(previous_eligible)
    components = {
        "decision_points": round(decision_points, 2),
        "required_points": round(required * 4.0, 2),
        "preferred_points": round(preferred * 2.0, 2),
        "hard_blocker_points": round(hard_blockers * 3.0, 2),
        "fit_points": round(average_fit / 10.0, 2),
        "trend_points": round(max(-5, min(5, delta)) * 2.0, 2),
    }
    score = round(max(0.0, min(100.0, sum(components.values()))), 2)
    decision_counts = {
        decision: sum(
            1 for row in eligible.values() if row["appearance_workflow_decision"] == decision
        )
        for decision in ("strong_apply", "apply", "moderate_stretch")
    }
    return {
        "score": score,
        "eligible_affected_requisitions": len(eligible),
        "excluded_requisitions": len(current_postings) - len(eligible),
        "decision_counts": decision_counts,
        "required_requisitions": required,
        "preferred_requisitions": preferred,
        "hard_blocker_requisitions": hard_blockers,
        "components": components,
        "formula_version": "v1",
    }


def _skill_metrics(
    skill_key: str,
    current_rows: Sequence[Dict[str, Any]],
    previous_rows: Sequence[Dict[str, Any]],
    training: Mapping[str, Dict[str, Any]],
) -> Dict[str, Any]:
    current_postings = _unique_posting_rows(current_rows)
    previous_postings = _unique_posting_rows(previous_rows)
    latest_row = max(current_rows, key=lambda row: (str(row["run_date"]), str(row["id"])))
    required = len({str(row["posting_id"]) for row in current_rows if row["gap_classification"] == "required"})
    preferred = len({str(row["posting_id"]) for row in current_rows if row["gap_classification"] == "preferred"})
    active = len(
        {
            str(row["posting_id"])
            for row in current_rows
            if row["active"] and row["resolution_state"] != "posting_false_positive"
        }
    )
    resolved = len(
        {
            str(row["posting_id"])
            for row in current_rows
            if not row["active"] and row["resolution_state"] != "posting_false_positive"
        }
    )
    false_positive = len(
        {str(row["posting_id"]) for row in current_rows if row["resolution_state"] == "posting_false_positive"}
    )
    average_fit = sum(float(row["appearance_fit_score"]) for row in current_postings.values()) / len(
        current_postings
    )
    high_fit = len(
        {
            posting_id
            for posting_id, row in current_postings.items()
            if float(row["appearance_fit_score"]) >= 80
            and row["appearance_workflow_decision"] in {"strong_apply", "apply"}
        }
    )
    previous_count = len(previous_postings)
    current_count = len(current_postings)
    absolute_change = current_count - previous_count
    percent_change = round(absolute_change / previous_count * 100.0, 2) if previous_count else None
    training_value = training.get(
        skill_key,
        {
            "status": "not_evaluated",
            "target_date": None,
            "notes": None,
            "learning_resources": [],
            "updated_at": None,
        },
    )
    return {
        "skill_key": skill_key,
        "display_name": latest_row["display_name"],
        "unique_affected_requisitions": current_count,
        "required_count": required,
        "preferred_count": preferred,
        "active_count": active,
        "resolved_count": resolved,
        "false_positive_count": false_positive,
        "high_fit_affected_roles": high_fit,
        "average_fit_score": round(average_fit, 2),
        "first_observed": min(str(row["period_first_observed"]) for row in current_rows),
        "last_observed": max(str(row["run_date"]) for row in current_rows),
        "trend": {
            "previous_unique_requisitions": previous_count,
            "absolute_change": absolute_change,
            "percent_change": percent_change,
        },
        "training": dict(training_value),
        "training_priority": _priority(current_rows, previous_rows),
    }


def _analytics_context(
    db: sqlite3.Connection, raw_filters: Mapping[str, str]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    filters = _validate_read_filters(db, raw_filters)
    end = filters["as_of"]
    start = end - dt.timedelta(days=filters["days"] - 1)
    previous_end = start - dt.timedelta(days=1)
    previous_start = previous_end - dt.timedelta(days=filters["days"] - 1)
    current = _filtered_latest_rows(_observation_rows(db, start, end), filters)
    previous = _filtered_latest_rows(_observation_rows(db, previous_start, previous_end), filters)
    training = _training_by_skill(db)
    filters["start"] = start
    filters["previous_start"] = previous_start
    filters["previous_end"] = previous_end
    return filters, current, previous, training


def get_skill_gap_summary(
    db: sqlite3.Connection, raw_filters: Mapping[str, str]
) -> Dict[str, Any]:
    filters, current, previous, training = _analytics_context(db, raw_filters)
    by_skill: Dict[str, List[Dict[str, Any]]] = {}
    previous_by_skill: Dict[str, List[Dict[str, Any]]] = {}
    for row in current:
        by_skill.setdefault(str(row["skill_key"]), []).append(row)
    for row in previous:
        previous_by_skill.setdefault(str(row["skill_key"]), []).append(row)
    metrics = [
        _skill_metrics(skill, rows, previous_by_skill.get(skill, []), training)
        for skill, rows in by_skill.items()
    ]
    training_filter = filters["training_status"]
    if training_filter:
        metrics = [item for item in metrics if item["training"]["status"] == training_filter]
    metrics.sort(
        key=lambda item: (
            -float(item["training_priority"]["score"]),
            -int(item["unique_affected_requisitions"]),
            str(item["skill_key"]),
        )
    )
    visible_skills = {str(item["skill_key"]) for item in metrics}
    visible_rows = [row for row in current if str(row["skill_key"]) in visible_skills]
    visible_postings = _unique_posting_rows(visible_rows)
    active_gaps = len(
        {
            (str(row["posting_id"]), str(row["skill_key"]))
            for row in visible_rows
            if row["active"] and row["resolution_state"] != "posting_false_positive"
        }
    )
    high_fit_opportunities = len(
        {
            posting_id
            for posting_id, row in visible_postings.items()
            if float(row["appearance_fit_score"]) >= 80
            and row["appearance_workflow_decision"] in {"strong_apply", "apply"}
        }
    )
    top_priority = None
    if metrics:
        top_skill = metrics[0]
        top_priority = {
            "skill_key": top_skill["skill_key"],
            "display_name": top_skill["display_name"],
            "score": top_skill["training_priority"]["score"],
        }
    return {
        "period": {
            "days": filters["days"],
            "start": filters["start"].isoformat(),
            "end": filters["as_of"].isoformat(),
            "previous_start": filters["previous_start"].isoformat(),
            "previous_end": filters["previous_end"].isoformat(),
        },
        "filters": {
            key: raw_filters.get(key)
            for key in ("decision", "classification", "state", "company", "title", "training_status")
            if raw_filters.get(key) is not None
        },
        "totals": {
            "active_gaps": active_gaps,
            "affected_jobs": len(visible_postings),
            "high_fit_opportunities": high_fit_opportunities,
            "top_training_priority": top_priority,
        },
        "skill_count": len(metrics),
        "skills": metrics,
    }


def _application_outcome(
    application_id: Optional[str], jobs: Mapping[str, Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not application_id or application_id not in jobs:
        return None
    job = jobs[application_id]
    interviews = job.get("interviews")
    outcomes = []
    if isinstance(interviews, list):
        outcomes = [
            str(item.get("outcome")).strip()
            for item in interviews
            if isinstance(item, dict) and str(item.get("outcome") or "").strip()
        ]
    return {
        "status": str(job.get("status") or "").strip() or "Unspecified",
        "interview_outcomes": outcomes,
    }


def get_skill_gap_detail(
    db: sqlite3.Connection, skill_key: str, raw_filters: Mapping[str, str]
) -> Dict[str, Any]:
    if not SKILL_KEY_PATTERN.fullmatch(skill_key):
        raise ValueError("skill_key must be a lowercase canonical skill key")
    filters, current, previous, training = _analytics_context(db, raw_filters)
    current_skill = [row for row in current if row["skill_key"] == skill_key]
    previous_skill = [row for row in previous if row["skill_key"] == skill_key]
    if not current_skill:
        raise KeyError(skill_key)
    metric = _skill_metrics(skill_key, current_skill, previous_skill, training)
    jobs = {job_id: value for job_id, value in _load_jobs(db)}
    posting_rows = _unique_posting_rows(current_skill)
    affected: List[Dict[str, Any]] = []
    statuses: Dict[str, int] = {}
    interview_outcomes: Dict[str, int] = {}
    for posting_id, row in sorted(posting_rows.items(), key=lambda item: str(item[1]["requisition_id"] or item[0])):
        application_id = row["tracker_application_id"]
        outcome = _application_outcome(application_id, jobs)
        if outcome:
            status = str(outcome["status"])
            statuses[status] = statuses.get(status, 0) + 1
            for value in outcome["interview_outcomes"]:
                interview_outcomes[value] = interview_outcomes.get(value, 0) + 1
        observations = [item for item in current_skill if item["posting_id"] == posting_id]
        affected.append(
            {
                "posting_id": posting_id,
                "requisition_id": row["requisition_id"],
                "canonical_url": row["canonical_url"],
                "company": row["company"],
                "title": row["title"],
                "location": row["location"],
                "work_model": row["work_model"],
                "workflow_decision": row["appearance_workflow_decision"],
                "fit_score": row["appearance_fit_score"],
                "primary_blocker": row["appearance_primary_blocker"],
                "tracker_application_id": application_id,
                "application_outcome": outcome,
                "classifications": sorted({item["gap_classification"] for item in observations}),
                "hard_blocker": any(bool(item["hard_blocker"]) for item in observations),
                "resolution_states": sorted({item["resolution_state"] for item in observations}),
                "active": any(
                    bool(item["active"]) and item["resolution_state"] != "posting_false_positive"
                    for item in observations
                ),
                "first_observed": min(str(item["period_first_observed"]) for item in observations),
                "last_observed": max(str(item["run_date"]) for item in observations),
            }
        )
    return {
        **metric,
        "period": {
            "days": filters["days"],
            "start": filters["start"].isoformat(),
            "end": filters["as_of"].isoformat(),
        },
        "affected_postings": affected,
        "application_outcomes": {
            "statuses": statuses,
            "interview_outcomes": interview_outcomes,
        },
    }


def list_workflow_runs(db: sqlite3.Connection) -> Dict[str, Any]:
    rows = db.execute(
        """
        SELECT r.*,
            (SELECT COUNT(*) FROM skill_gap_observations o WHERE o.source_review_run_id = r.id)
                AS gap_observation_count,
            (SELECT COUNT(*) FROM skill_gap_observations o
             WHERE o.source_review_run_id = r.id AND o.active = 1
               AND o.resolution_state <> 'posting_false_positive') AS active_gap_count
        FROM workflow_review_runs r
        ORDER BY r.run_date DESC
        """
    ).fetchall()
    return {"runs": [dict(row) for row in rows], "count": len(rows)}


def get_workflow_run(db: sqlite3.Connection, run_date: str) -> Dict[str, Any]:
    _read_date(run_date, "run_date")
    run = db.execute("SELECT * FROM workflow_review_runs WHERE run_date = ?", (run_date,)).fetchone()
    if not run:
        raise KeyError(run_date)
    appearance_rows = db.execute(
        """
        SELECT pr.*, p.posting_key, p.requisition_id, p.canonical_url, p.company,
            p.title, p.location, p.work_model, p.tracker_application_id
        FROM reviewed_posting_runs pr
        JOIN reviewed_postings p ON p.id = pr.posting_id
        WHERE pr.review_run_id = ?
        ORDER BY p.company, p.title, p.id
        """,
        (run["id"],),
    ).fetchall()
    postings: List[Dict[str, Any]] = []
    for appearance in appearance_rows:
        gap_rows = db.execute(
            """
            SELECT skill_key, display_name, raw_posting_label, gap_classification AS classification,
                hard_blocker, resolution_state, resolution_evidence, active, first_observed, last_observed
            FROM skill_gap_observations
            WHERE source_review_run_id = ? AND posting_id = ?
            ORDER BY skill_key, gap_classification
            """,
            (run["id"], appearance["posting_id"]),
        ).fetchall()
        value = dict(appearance)
        value["gaps"] = [dict(row) for row in gap_rows]
        postings.append(value)
    result = dict(run)
    result["postings"] = postings
    return result
