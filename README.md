# Job Tracker

A self-hosted job application tracker with a daily checklist and skill-gap analytics.

Nginx provides password protection and serves the frontend alongside a Python API backed by SQLite. Applications, task notes, and attachment metadata live in SQLite; uploaded resume and cover-letter copies live in the uploads directory. Each installation owns its data and credentials.

The repository contains generic defaults only. Personal settings are supplied at deployment through a private JSON file or a Kubernetes Secret. Without settings, the app uses an empty checklist, the browser's timezone, and no external calendar.

The Skill Gaps tab is a read-only analytics dashboard backed by jobagent workflow imports. Gap classifications, resolutions, and training status remain jobagent-owned. See [Skill-gap analytics backend](docs/skill-gap-analytics-backend.md) for the import contract, APIs, and backup/rollback procedure. Imports are disabled until an owner supplies `JOBTRACKER_ANALYTICS_IMPORT_TOKEN`.

## Run on a Laptop

Install Docker with Docker Compose and OpenSSL. Run these commands from the project directory in a POSIX shell (macOS/Linux, or WSL on Windows).

Create a local login named `tracker`; OpenSSL prompts for the password. Choose another username by changing `tracker` below. Run this once; repeating it replaces the login file.

```sh
mkdir -p .local
chmod 700 .local
(umask 077; printf 'tracker:' > .local/jobtracker.htpasswd)
openssl passwd -apr1 >> .local/jobtracker.htpasswd
chmod 644 .local/jobtracker.htpasswd
```

The private `.local` directory protects the file on your laptop; the read-only mount must be readable by Nginx inside the container.

Create your private runtime settings from the generic example, then edit `.local/runtime.json` with your own values:

```sh
cp deployment/runtime.example.json .local/runtime.json
chmod 600 .local/runtime.json
```

Start the complete application, including the API proxy:

```sh
docker compose up --build -d
```

Open <http://127.0.0.1:8765/> and sign in with the credentials you created. The port is bound only to laptop loopback. The login file must exist before startup; no default password is shipped. Login files, databases, uploads, and local configuration are excluded from Git and the image build context.

```sh
docker compose down
```

Stopping the app preserves its named data volume. `docker compose down -v` deletes that volume and its data.

For an API-only development server:

```sh
JOBTRACKER_DATA_DIR=.local/data python3 -m server.app
```

The API binds to `127.0.0.1:8090`. A separate `python3 -m http.server` does not proxy `/api`; use Compose for the full application.

## Deployment Settings

Set these fields in your private runtime JSON, never in tracked source files:

| Field | Meaning | Default |
| --- | --- | --- |
| `title` | Browser title and application heading | `Job Tracker` |
| `timeZone` | IANA timezone, such as `Europe/London` | Empty uses the browser timezone |
| `calendarFeedUrl` | Private HTTPS iCalendar feed; kept on the server | Empty disables sync |
| `calendarOpenUrl` | Optional HTTPS link for the calendar Open button | Empty hides the button |
| `defaultTasks` | Daily task templates with `id`, `title`, `start`, `end`, and optional `detail` | Empty checklist |

Task IDs must be unique and stable so existing task notes and edits continue to match. Times use `HH:MM`, with the end later than the start on the same day. For example, a generic task is `{"id":"follow-up","title":"Follow up","start":"09:00","end":"09:30"}`. Stored task edits override deployment defaults; removing a template does not delete stored entries.

Configuration is validated at container startup. Invalid settings stop startup before the database is opened. The private calendar feed URL never appears in the browser configuration. Calendar dates and busy-block times use the configured timezone. The current calendar parser supports UTC timestamps and date-only events; it does not expand recurring events or interpret arbitrary `TZID` timestamps.

Compose reads `.local/runtime.json` through a read-only mount. After changing it, run `docker compose restart`. To change deployment paths or the local port, set these variables in an ignored `.env` file or your shell, then run `docker compose up -d`:

| Variable | Default |
| --- | --- |
| `JOBTRACKER_RUNTIME_FILE` | `./.local/runtime.json` |
| `JOBTRACKER_AUTH_FILE` | `./.local/jobtracker.htpasswd` |
| `JOBTRACKER_DATA_PATH` | The managed `jobtracker-data` volume; use an absolute host path for a bind mount |
| `JOBTRACKER_PORT` | `8765`, bound to loopback |

For other container runtimes, mount a private JSON file and set `JOBTRACKER_CONFIG_FILE` to its container path, or supply `JOBTRACKER_CONFIG_JSON` through the runtime's secret mechanism. Do not set both. No image rebuild is needed for personal settings. The remote deployment source archive excludes `.local`, `.env`, databases, uploads, and backups.

## Deploy to Kubernetes

Create the login and runtime files as above. With `kubectl` targeting your intended cluster, create the namespace if needed, then load the runtime file into a Secret:

```sh
kubectl create namespace jobtracker --dry-run=client -o yaml | kubectl apply -f -
kubectl -n jobtracker create secret generic jobtracker-runtime \
  --from-file=config.json=.local/runtime.json --dry-run=client -o yaml | kubectl apply -f -
```

Supply the login file to the deployment script:

```sh
export JOBTRACKER_HTPASSWD="$(cat .local/jobtracker.htpasswd)"
./deployment/scripts/deploy-jobtracker-remote-k8s.sh deploy --host server.example.com --ssh-user deploy
unset JOBTRACKER_HTPASSWD
```

Replace the example host and SSH username with your own. Later deployments can reuse the existing Kubernetes authentication secret. The default namespace is `jobtracker` and the host-facing port is `8086`. Configure your own domain/reverse proxy if remote access is needed.

Use `--data-dir /srv/jobtracker-data` to choose a node storage location at deployment. Without an override, the script preserves the existing deployment's hostPath; a new installation defaults to `/var/lib/jobtracker`. The container always mounts it at `/data`. Changing the path does not migrate existing data.

The manifest reads the optional `jobtracker-runtime` Secret at startup. After updating that Secret, redeploy or run `kubectl -n jobtracker rollout restart deployment/jobtracker` to load the new settings. For another namespace, create both Secrets there and pass `--namespace` to the deployment script. Domains and TLS belong in your deployment's reverse-proxy/ingress configuration.

## Persistent Data and Backups

In the container, the important paths are `/data/jobtracker.db` and `/data/uploads/`. The Compose named volume holds both; copy neither into the source tree for sharing.

Back up the local database using SQLite's backup API, then copy it out:

```sh
mkdir -p backups
docker compose exec --workdir /app jobtracker python3 -m server.backup /data/jobtracker.db /tmp/jobtracker-backup.db
docker compose cp jobtracker:/tmp/jobtracker-backup.db backups/jobtracker.db
docker compose cp jobtracker:/data/uploads backups/uploads
```

Pause writes during backup if you need the database and uploaded files to represent the same point in time. Keep backups private.
The backup helper refuses to overwrite an existing destination; use a new filename for subsequent backups.

## Tests

```sh
python3 -m unittest discover -s tests
npm test
```

Server coverage can be checked with `coverage run --source=server -m unittest discover -s tests` followed by `coverage report --fail-under=90`.
