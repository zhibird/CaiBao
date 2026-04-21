# CaiBao Local Dev Runbook

## Current Default

The current local development default in `.env` is SQLite.

If you want the fastest local startup, use:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Health check:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/v1/health
```

## Mode 1: SQLite Dev

Use this mode when you want quick local development and do not need PostgreSQL.

Recommended `.env` values:

```env
DB_LEGACY_INIT_ENABLED=true
DATABASE_URL=sqlite:///./CaiBao.db
```

Start commands:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Stop with `Ctrl+C`.

## Mode 2: PostgreSQL Dev

Use this mode when you want to develop against PostgreSQL locally.

Recommended `.env` values:

```env
DB_LEGACY_INIT_ENABLED=false
DATABASE_URL=postgresql+psycopg://caibao:caibao@localhost:5432/caibao
```

### Step 1: Start PostgreSQL

Start only the database container:

```powershell
docker compose up -d postgres
```

Check status:

```powershell
docker compose ps
docker compose logs -f postgres
```

### Step 2: Run Migrations

```powershell
.\.venv\Scripts\Activate.ps1
alembic upgrade head
```

### Step 3: Start the App

```powershell
uvicorn app.main:app --reload --port 8000
```

Health check:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/v1/health
```

## Mode 3: Full Docker

Use this mode when you want PostgreSQL, migrations, and API all started together in containers.

```powershell
docker compose up --build -d
```

Check logs:

```powershell
docker compose logs -f caibao-api
docker compose logs -f postgres
```

Health check:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/v1/health
```

## Common Commands

Start existing PostgreSQL container again:

```powershell
docker compose start postgres
```

Stop PostgreSQL container:

```powershell
docker compose stop postgres
```

Stop all project containers:

```powershell
docker compose down
```

Remove containers and volumes:

```powershell
docker compose down -v
```

## Recommendation

For daily feature development, use SQLite mode.

Switch to PostgreSQL mode when you need:

- database-specific behavior
- migration verification
- production-like local testing
