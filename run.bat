@echo off
uv sync
uv run uvicorn src.main:app --reload --port 8105
