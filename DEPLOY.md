# Public Deployment

GitHub Pages can only serve the static page in `docs/`. The working optimiser is a
Python/FastAPI app, so it needs an app host.

## Render

1. Open <https://render.com>.
2. Create a new **Web Service** from `Ray51773/Waynesworld`.
3. Render should detect `render.yaml`.
4. Deploy.

The first start loads public FPL data and the 2025/26 history used by the model, so
it can take a little longer than a normal restart.

## Generic Docker Hosts

Any host that supports Docker can use the included `Dockerfile`.

Required environment variables:

```text
PORT=8000
FPL_DATA_DIR=/app/data
FPL_MANAGER_ID=0
```

## Public-Use Caveat

This version has no user accounts. Manual squad entries are stored in one shared
DuckDB database, so a public deployment is best treated as a demo until user
accounts or per-browser storage are added.
