# Architecture

`job-application-crm` is intentionally small and upgradeable.

```text
src/main.py        FastAPI app, routes, SQLite helpers, browser UI
tests/test_api.py  API contract tests
data/app.sqlite    Local development database, ignored by git
```

The first version focuses on proving the workflow works locally. AI-specific behavior is implemented as deterministic heuristics where that makes tests reliable, with seams for adding Ollama later.
