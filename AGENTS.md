# Codex Project Instructions

## Project Context

When you need repo context, consult these files:

- [README.md](README.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/STRAVA_API.md](docs/STRAVA_API.md)
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md)
- [docs/DATA_MODELS.md](docs/DATA_MODELS.md)
- [docs/features/KOM_QOM_CANDIDATES.md](docs/features/KOM_QOM_CANDIDATES.md)

## Version Control

- Never run `git commit` or `git push`. The developer handles all version control actions manually.

## Documentation Drift Check

Before finishing any task where implementation files changed, check whether documentation may now be out of sync. Use `git diff HEAD --name-only` when available to identify changed files.

Treat these as implementation paths:

- `backend/app/models/`
- `backend/app/routers/`
- `backend/app/schemas/`
- `backend/app/services/`
- `backend/app/strava/`
- `backend/app/tasks.py`
- `backend/app/utils.py`
- `backend/app/config.py`
- `backend/app/main.py`
- `frontend/src/`

If any of those paths changed during the session and no file under `docs/` changed, review the relevant docs before the final response:

- `README.md`: repo layout tree, environment variables, key conventions
- `docs/ARCHITECTURE.md`: database schema, sync strategy, rate-limit description
- `docs/STRAVA_API.md`: endpoints, parameters, rate-limit thresholds, TTLs
- `docs/CONVENTIONS.md`: patterns, migration list
- `docs/DATA_MODELS.md`: table names, columns, Mermaid ER diagram
- `docs/features/KOM_QOM_CANDIDATES.md`: API contract, business logic, filters

Only update documentation that is actually out of sync. If the documentation is still accurate, leave it unchanged and mention in the final response that the docs drift check was completed.
