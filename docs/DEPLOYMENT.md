# Production Deployment

This project starts production on one low-cost AWS Lightsail Linux instance. The instance runs Caddy for HTTPS/static files, FastAPI on localhost, SQLite on a persistent disk path, and systemd timers for Strava backfill and SQLite backups.

## Architecture

```
Browser
  |
  | HTTPS https://<domain>
  v
Caddy
  |-- static files: /opt/training-tools/current/frontend/dist
  |-- /api/*, /auth/*, /health -> 127.0.0.1:8000
                                      |
                                      v
                               FastAPI + SQLite
                               /var/lib/training-tools/
```

Use the Lightsail 1 GB Linux plan for the first production deployment. SQLite is acceptable only while OAuth is private-gated with `ALLOWED_ATHLETE_IDS`; migrate to Postgres before opening signup beyond the allowlist.

## One-Time Lightsail Bootstrap

1. Create a Lightsail Linux instance, assign a static IP, and point the production domain `A` record to it.
2. Open only ports `22`, `80`, and `443` in the Lightsail firewall.
3. Install runtime dependencies:

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates sqlite3 rsync caddy
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo install -m 0755 "$HOME/.local/bin/uv" /usr/local/bin/uv
```

4. Create the persistent data directory:

```bash
sudo install -d -o www-data -g www-data -m 0750 /var/lib/training-tools /var/lib/training-tools/backups
```

5. Enable Lightsail automatic snapshots. Keep a manual snapshot before risky infrastructure changes.
6. In the Strava developer console, set the OAuth callback to:

```text
https://<domain>/auth/strava/callback
```

## GitHub Actions Setup

The workflow is `.github/workflows/production-deploy.yml`. It runs on pushes to `main` and via manual dispatch. Use a protected GitHub `production` environment.

Required environment variable:

```text
PRODUCTION_DOMAIN=<domain without https://>
```

Required environment secrets:

```text
LIGHTSAIL_HOST=<static IP or host>
LIGHTSAIL_USER=<ssh user, usually ubuntu>
LIGHTSAIL_SSH_KEY=<private key for deploy SSH>
LIGHTSAIL_KNOWN_HOSTS=<ssh-keyscan output for the host>
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
SECRET_KEY=
BACKFILL_SECRET=
ALLOWED_ATHLETE_IDS=<comma-separated Strava athlete IDs>
```

Optional environment variables:

```text
DEPLOY_PATH=/opt/training-tools
DATA_PATH=/var/lib/training-tools
HOME_LAT=<fallback default, optional>
HOME_LNG=<fallback default, optional>
```

The workflow builds the frontend, tests the backend, uploads a release tarball, writes `/opt/training-tools/.env.production`, installs the systemd/Caddy templates, restarts the API, reloads Caddy, and checks `https://<domain>/health`.

## Runtime Files

Production env should match `deploy/env.production.example`:

```text
DATABASE_URL=sqlite+aiosqlite:////var/lib/training-tools/training_tools.db
RATE_LIMIT_STATE_PATH=/var/lib/training-tools/rate_limit_state.json
FRONTEND_URL=https://<domain>
STRAVA_REDIRECT_URI=https://<domain>/auth/strava/callback
ALLOWED_ATHLETE_IDS=<your Strava athlete ID>
```

Do not leave `ALLOWED_ATHLETE_IDS` empty in SQLite production. Empty means any Strava athlete who can authorize the app may connect.

## Operations

- `training-tools-api.service`: FastAPI backend on `127.0.0.1:8000`.
- `training-tools-backfill.timer`: daily call to `/api/internal/daily-backfill` with `BACKFILL_SECRET`.
- `training-tools-sqlite-backup.timer`: daily SQLite `.backup` into `/var/lib/training-tools/backups`, retaining 14 days by default.
- `Caddyfile`: same-origin HTTPS, static Astro files, reverse proxy for backend routes.

Useful checks:

```bash
systemctl status training-tools-api.service
systemctl list-timers 'training-tools-*'
journalctl -u training-tools-api.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8000/health
```

## Public-User Path

Before removing `ALLOWED_ATHLETE_IDS`, move off SQLite:

- Replace SQLite-specific upserts with dialect-safe SQLAlchemy logic.
- Add `asyncpg` and a managed encrypted Postgres database.
- Move schema changes to Alembic or another production-safe migration flow.
- Re-test OAuth, sync, backfill, and candidate queries against Postgres.
