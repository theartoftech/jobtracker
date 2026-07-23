# Skill-gap analytics backend

This is the backend-only, read-only first analytics release. It adds persistence, a jobagent-owned import path, and dashboard read APIs. It does not add an analytics UI or a gap-resolution editor. The jobagent remains the source of truth for gap classifications and resolutions. In particular, `evidence_developed` means training evidence exists; it is not professional experience and never clears an active gap automatically.

## Safety invariants

- Migrations are additive. The `jobs`, `attachments`, `task_entries`, and `settings` tables are neither rebuilt nor rewritten.
- Job JSON, including embedded interviews, remains byte-for-byte unchanged during migration and analytics imports.
- `/api/state` is not used for analytics. The dedicated import route never calls `write_state`.
- No analytics foreign key cascades to jobs or attachments.
- `reviewed_postings.tracker_application_id` references `jobs.id ON DELETE SET NULL`. A narrow `AFTER DELETE ON jobs` trigger performs the same nulling for the previous server version, which did not enable SQLite foreign-key enforcement.
- Application deletion does not delete posting, run, gap, or training history.
- The previous application version can use the upgraded database and ignore the new tables.
- An unset import token disables imports; read APIs remain available behind the tracker's existing Nginx authentication.

## Schema and migrations

`schema_migrations(version, name, applied_at)` records each explicit migration exactly once. Startup applies pending migrations in ascending version order and fails on an unknown applied version. Version 1 creates:

| Table | Responsibility |
| --- | --- |
| `workflow_review_runs` | One stable row per `run_date`, source artifact/SHA, contract version, optional workflow profile version, import time, and reviewed count. |
| `reviewed_postings` | Deduplicated posting identity and current descriptive fields, first/last observation dates, optional application link, and already-applied state. |
| `reviewed_posting_runs` | Historical decision, fit score, blocker, and already-applied snapshot for every posting in every run, including postings with no gaps. |
| `skill_gap_observations` | Per-run posting/skill/classification observations, raw label, blocker flag, resolution/evidence, active state, and first/last dates. |
| `skill_training_status` | Jobagent-supplied status, target date, notes, resource URLs, and update timestamp by canonical skill key. |

The migration uses no destructive DDL. Running it repeatedly produces no additional rows or schema changes. SQLite foreign keys are enabled on current server connections.

## Import authentication and limits

Configure a dedicated random secret:

```bash
export JOBTRACKER_ANALYTICS_IMPORT_TOKEN='replace-with-a-long-random-token'
```

Send it only as `Authorization: Bearer <token>`. The server uses `hmac.compare_digest` for the credential comparison. This token is separate from browser/basic-auth credentials. The route is disabled when the environment variable is absent or empty.

Nginx exempts only the two exact import paths (`/api/analytics/workflow-runs` and its `/jobtracker`-prefixed form) from Basic auth because Basic and Bearer credentials cannot share one `Authorization` header. The Python endpoint still requires the scoped Bearer token. All analytics read paths remain under Basic auth. For a future Kubernetes deployment, create the optional secret before enabling jobagent imports:

```bash
kubectl -n jobtracker create secret generic jobtracker-analytics-import \
  --from-literal=token="$JOBTRACKER_ANALYTICS_IMPORT_TOKEN"
```

Limits for contract v1:

- Request body: 2 MiB
- Reviewed postings: 500 per run
- Gap observations: 5,000 across the run
- Training-status records: 500
- Learning resources: 50 per skill

## Version 1 JSON contract

The machine-readable contract is [`workflow-run-import-v1.schema.json`](workflow-run-import-v1.schema.json). All objects reject unknown fields. The tracker validates the complete payload before opening the import savepoint and never silently skips a malformed item.

```json
{
  "schema_version": 1,
  "run_date": "2026-07-20",
  "source_artifact_name": "strict-review-2026-07-20.json",
  "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "workflow_profile_version": "resume-strict-v4",
  "postings": [
    {
      "posting_key": "acme-backend-req-123",
      "requisition_id": "REQ-123",
      "canonical_url": "https://jobs.example.com/acme/123",
      "company": "Acme",
      "title": "Backend Engineer",
      "location": "Remote, US",
      "work_model": "remote",
      "source_decision": "strong_apply",
      "fit_score": 91.5,
      "strict_workflow_decision": "apply",
      "primary_blocker": "skill_gap",
      "already_applied": false,
      "gaps": [
        {
          "skill_key": "python",
          "display_name": "Python",
          "raw_posting_label": "Professional Python backend experience",
          "classification": "required",
          "hard_blocker": true,
          "resolution_state": "limited_exposure",
          "resolution_evidence": "Used Python for internal automation, not production backend services.",
          "active": true
        }
      ]
    }
  ],
  "training_statuses": [
    {
      "skill_key": "python",
      "status": "learning",
      "target_date": "2026-09-01",
      "notes": "Backend course and service project",
      "learning_resources": ["https://training.example.com/python"]
    }
  ]
}
```

Canonical skill keys are supplied by the jobagent and match `^[a-z0-9][a-z0-9._+-]{0,79}$`; the tracker does not infer skills from descriptions.

Enums:

- Decisions: `strong_apply`, `apply`, `moderate_stretch`, `do_not_apply`
- Work models: `remote`, `hybrid`, `onsite`, `unspecified`
- Gap classifications: `required`, `preferred`
- Resolutions: `unanswered`, `professional_evidence`, `limited_exposure`, `do_not_claim`, `posting_false_positive`
- Training: `not_evaluated`, `not_pursuing`, `planned`, `learning`, `practicing`, `evidence_developed`
- Primary blockers: `none`, `skill_gap`, `location`, `clearance`, `compensation`, `employment_authorization`, `unrelated`

A false-positive observation must be inactive. Dates are strict `YYYY-MM-DD`. URLs must be absolute HTTP(S) URLs. SHA-256 values are exactly 64 hexadecimal characters.

Nullable/optional posting fields are `requisition_id` and `location`; omitted `primary_blocker` defaults to `none`. `workflow_profile_version` is optional. Within a training record, `target_date`, `notes`, and `learning_resources` are optional and default to `null`, `null`, and an empty array respectively.

## Import behavior

```bash
curl -sS -X POST http://127.0.0.1:8090/api/analytics/workflow-runs \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${JOBTRACKER_ANALYTICS_IMPORT_TOKEN}" \
  --data-binary @review-run.json
```

The entire validated batch is processed in one SQLite transaction/savepoint. Any validation or database error rolls back the run, posting appearances, observations, and training updates.

`run_date` is the idempotency key. An exact replay returns `unchanged` actions. A revised artifact for the same date updates the run and posting snapshots, inserts/updates the submitted observations, and removes stale observations/appearances from that dated run only. It never removes stable postings or history from other dates. Responses include inserted, updated, unchanged, rejected, and same-day-removed counts for each record class plus totals.

Posting identity is resolved in this order:

1. Exact requisition ID.
2. Canonical URL when no requisition match exists; a requisition-bearing import may upgrade one earlier URL-only posting.
3. Different non-empty requisition IDs always remain separate, even when their URLs are identical.
4. A URL-only import that matches several requisitions is rejected as ambiguous and rolled back.

First-observed dates only move earlier and last-observed dates only move later.

## Application linkage

Imports inspect, but never rewrite, existing job JSON. A posting is linked conservatively:

1. Exact requisition ID (`requisitionId`, `requisition_id`, or `jobRequisitionId`).
2. Canonical `jobUrl`, provided it does not conflict with a different requisition ID.
3. Case-insensitive company/title only when neither side has a requisition ID.

Ambiguous matches remain unlinked. Status and interview outcomes are read from the linked job only when building skill detail. Status is never transferred between different requisitions.

## Read APIs

- `GET /api/analytics/skill-gaps`
- `GET /api/analytics/skill-gaps/{skill_key}`
- `GET /api/analytics/workflow-runs`
- `GET /api/analytics/workflow-runs/{run_date}`

Summary/detail filters are `days=30|60|90`, `decision`, `classification=required|preferred`, `state=active|resolved|all`, `company`, `title`, and `training_status`. `as_of=YYYY-MM-DD` is also available for deterministic historical queries; otherwise the newest imported run date anchors the window. Company and title filters are case-insensitive substrings. Unknown or repeated filters are errors.

Counts deduplicate stable postings/requisitions within a period, so repeated daily sightings do not inflate totals. Required and preferred counts are independently deduplicated. Active totals exclude `posting_false_positive`. High-fit roles are unique postings with fit score at least 80 and decision `strong_apply` or `apply`. Trend is the unique-posting count in the selected window minus the count in the immediately preceding equal-length window; percent is `null` when the preceding count is zero.

Skill detail returns affected posting links, classifications, fit/decision/blocker context, optional tracker application IDs, application status, interview outcomes, and aggregated outcome counts.

## Training-priority calculation (v1)

Priority uses only unique postings whose strict decision is `strong_apply`, `apply`, or `moderate_stretch`. It excludes postings whose primary blocker is `location`, `clearance`, or `unrelated`. Those postings remain visible in affected counts but cannot dominate recommendations.

For each skill:

```text
decision_points = 12 * strong_apply + 10 * apply + 6 * moderate_stretch
required_points = 4 * eligible required requisitions
preferred_points = 2 * eligible preferred requisitions
hard_blocker_points = 3 * eligible hard-blocker requisitions
fit_points = average eligible fit score / 10
trend_points = 2 * clamp(eligible current count - eligible preceding count, -5, 5)
score = clamp(sum(all components), 0, 100)
```

The response includes every component, eligible/excluded counts, decision counts, and formula version so the score is explainable. Training status does not change gap resolution or activity.

## Backup, copied-database verification, restore, and rollback

Do not copy a live `jobtracker.db` file directly while WAL mode is active. Use SQLite's online backup API, which includes committed WAL pages:

```bash
mkdir -p /safe/path/jobtracker-backups
python3 -m server.backup /data/jobtracker.db /safe/path/jobtracker-backups/jobtracker-before-analytics.db
tar -czf /safe/path/jobtracker-backups/jobtracker-uploads.tgz -C /data uploads
shasum -a 256 /data/uploads/* > /safe/path/jobtracker-backups/uploads.sha256
```

The utility refuses missing sources, an in-place destination, and existing destination files. It runs `PRAGMA integrity_check` on the backup and reports JSON only after `ok`.

Before any future production deployment, operate on the backup/copy only:

```bash
tmpdir="$(mktemp -d)"
cp /safe/path/jobtracker-backups/jobtracker-before-analytics.db "$tmpdir/jobtracker.db"
JOBTRACKER_DATA_DIR="$tmpdir" JOBTRACKER_DB_PATH="$tmpdir/jobtracker.db" \
  python3 -c 'from server.app import ensure_storage; ensure_storage()'
sqlite3 "$tmpdir/jobtracker.db" 'PRAGMA integrity_check; SELECT * FROM schema_migrations;'
```

Then compare core row dumps and upload SHA-256 manifests before/after. The automated preservation suite performs exact row comparisons for jobs (including JSON/interviews), tasks, attachments, and settings, plus attachment-byte hashes.

Restore requires an explicit maintenance window: stop writers, save the failed database and WAL/SHM files for diagnosis, place the verified backup at the configured database path, restore the uploads archive if needed, and run `PRAGMA integrity_check` before starting the service. Never restore by mixing a database with unrelated WAL/SHM files.

Application rollback needs no database downgrade: the prior binary ignores the analytics tables. Do not drop analytics tables during rollback. If a later, explicitly authorized cleanup is desired, first make and verify another online backup.

## Future jobagent integration

The jobagent will emit one complete contract-v1 artifact per review date, calculate its SHA-256, and POST it with the scoped token after its strict resume workflow finishes. It owns canonical skill keys, decisions, resolutions, and training states. Retries are safe because `run_date` is idempotent. Contract changes require a new supported `schema_version`; unknown versions are rejected.
