# Email Outreach Platform

Phase 0 establishes the production application foundation only. Product features,
authentication, workspaces, RBAC, provider integrations, and email sending are out
of scope for this scaffold.

## Prerequisites

- Python 3.12
- Node.js 24
- Docker and Docker Compose for the local container stack
- A Supabase PostgreSQL connection string for readiness checks

On this Windows host, use `npm.cmd` rather than `npm` from PowerShell.

## Environment

Copy `.env.example` to `.env` and replace placeholders with local/staging values.
Do not commit `.env` files or real credentials.

## Backend

```powershell
cd backend
python -m pip install -e ".[dev]"
python -m uvicorn app.main:app --reload
python -m pytest
python -m ruff check .
python -m mypy app
```

Backend endpoints:

- `GET /api/v1/health`
- `GET /api/v1/ready`

## Frontend

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
```

## Workers

```powershell
$env:PYTHONPATH=".;backend"
celery -A workers.celery_app:celery_app worker --loglevel=INFO --queues=maintenance
python -m workers.smoke
python -m workers.scheduler
```

## Docker Compose

There is a single `docker-compose.yml` (the production stack; it also works
locally). Use it as-is, with no `-f` flag:

```powershell
docker compose config
docker compose up -d --build
docker compose ps
docker compose down
```

It starts `redis`, `backend`, `frontend`, `scheduler`, `rate-controller` and four
Celery workers (`worker-general`, `worker-events`, `worker-send`, `worker-sync`).
Ports are published on 127.0.0.1 only (`BACKEND_HOST_PORT` / `FRONTEND_HOST_PORT`
in `.env`, default 8000 / 3000). PostgreSQL remains external via `DATABASE_URL`.
See [DEPLOYMENT.md](docs/operations/DEPLOYMENT.md).

## Database

Schema migrations are owned exclusively by `supabase/migrations/`.

```powershell
python scripts/check_migrations.py
```

Do not use Alembic or SQLAlchemy schema auto-creation for this project
