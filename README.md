# Job Tracker

Standalone self-hosted job application tracker.

The app is a private Nginx site with server-side basic authentication and a small local Python API backed by SQLite. Application data, daily task notes/checks, and attachment metadata are stored in SQLite. Uploaded resume and cover-letter copies are stored under the persistent uploads directory and can be downloaded from the application record.

The first skill-gap analytics release is backend/read-only. See [Skill-gap analytics backend](docs/skill-gap-analytics-backend.md) for the additive schema, versioned jobagent import contract, read APIs, scoring formula, and safe backup/rollback procedure.

## Run Locally

```bash
JOBTRACKER_DATA_DIR=/tmp/jobtracker-data python3 -m server.app
python3 -m http.server 8765 --directory src
```

Open `http://127.0.0.1:8765/`. The static-only local server is useful for UI checks, but API-backed saves require the container or an Nginx proxy to `/api`.

## Deploy

Create a private userid/password hash:

```bash
read -r -s "JOBTRACKER_PASSWORD?Job tracker password: "
printf "\n"
export JOBTRACKER_HTPASSWD="tracker:$(openssl passwd -apr1 "$JOBTRACKER_PASSWORD")"
unset JOBTRACKER_PASSWORD
```

Deploy to the k3s server:

```bash
./deployment/scripts/deploy-jobtracker-remote-k8s.sh deploy --host server.example.com --ssh-user deploy
```

After the first deploy creates the Kubernetes secret, later deploys can reuse it:

```bash
./deployment/scripts/deploy-jobtracker-remote-k8s.sh deploy --host server.example.com --ssh-user deploy
```

Default runtime:

| Surface | Namespace | Host Port |
| --- | --- | ---: |
| Job tracker | `jobtracker` | `8086` |

Route either `jobtracker.example.com` to host port `8086`, or configure the public reverse proxy/Cloudflare route for `example.com/jobtracker*` to this standalone service.

## Persistent Data

The Kubernetes deployment mounts this host directory into the container as `/data`:

```text
/var/lib/jobtracker
```

Important files:

```text
/var/lib/jobtracker/jobtracker.db
/var/lib/jobtracker/uploads/
```

Back up the tracker from the server:

```bash
mkdir -p ~/jobtracker-backups
python3 -m server.backup /var/lib/jobtracker/jobtracker.db ~/jobtracker-backups/jobtracker-$(date +%Y%m%d%H%M%S).db
tar -czf ~/jobtracker-backups/jobtracker-uploads-$(date +%Y%m%d%H%M%S).tgz -C /var/lib/jobtracker uploads
```
