# Job Application CRM

![Python](https://img.shields.io/badge/Python-3.11_%7C_3.12-3776AB?style=flat-square&logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite)

Kanban-style job application tracker with follow-up reminders and interview notes. Move applications through stages (saved, applied, phone screen, interview, offer, rejected) and never lose track of where you stand.

## Features

- Kanban-style pipeline by application stage
- Status tracking with per-application notes
- Company and contact details per entry
- Follow-up reminders based on last activity date
- Interview question generator pulling from common patterns
- Clean local-first design — no signup required

## Tech Stack

- Python 3.11+ / FastAPI / SQLite
- Vanilla HTML/CSS/JS frontend served by the API
- Pytest

## Quick Start

```bash
uv sync
uv run uvicorn src.main:app --reload --port 8105
```

Open: http://localhost:8105

Windows: double-click `run.bat`

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Browser demo UI |
| GET | `/api/health` | Health check |
| GET | `/docs` | Interactive API docs |

## Tests

```bash
uv run pytest -q
```
