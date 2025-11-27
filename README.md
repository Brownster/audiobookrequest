
AudiobookRequest (fork) – an end‑to‑end pipeline for Audible search, MyAnonamouse downloads, qBittorrent/seedbox handoff, post‑processing, and Audiobookshelf ingestion.

This README reflects the current fork: Prowlarr is removed, MAM + qBittorrent is the main path, and AI recommendations can use OpenAI or Ollama.

<img width="1882" height="845" alt="image" src="https://github.com/user-attachments/assets/8efadf9d-afd8-4065-8a34-b60a2743804b" />

<img width="1882" height="845" alt="image" src="https://github.com/user-attachments/assets/21515fb8-9421-4180-bb63-405b010c984b" />

<img width="1879" height="569" alt="image" src="https://github.com/user-attachments/assets/e0d94379-8a17-4089-adfe-1eff583fa855" />

<img width="1879" height="569" alt="image" src="https://github.com/user-attachments/assets/973cbc61-1ea8-48c6-8fab-019a25e3ab71" />
<img width="1910" height="495" alt="image" src="https://github.com/user-attachments/assets/7868ce5c-a3d1-4340-9ba3-93336704afa9" />

<img width="1887" height="612" alt="image" src="https://github.com/user-attachments/assets/59086fa6-b693-4016-9d2b-363c6fff395c" />


## What it does
- Audible search with region selection; “Check on MAM” buttons from search, homepage carousels, and wishlist.
- Wishlist with pipeline status (pending → downloading → post‑processing → completed) and hourly auto‑rechecks for MAM‑unavailable items.
- MAM integration: search/download via qBittorrent (seedbox-friendly path mapping), auto-resume inactive torrents, and retry from wishlist/downloads.
- Post‑processing: merge/convert/tag, embed cover, clean temp files, and write into `author/title/book.m4b` under `ABR_APP__DOWNLOAD_DIR`.
- Downloads page: live status, Retry, and Remove actions for failed/stuck jobs.
- Audiobookshelf integration: marks existing items as downloaded and triggers scans after successful jobs.
- AI recommendations: pick OpenAI (`gpt-4o-mini` etc.) or Ollama for homepage/AI recs.

## Quick start (Docker)
1. Build or pull an image (example uses GHCR):
   ```bash
   docker run -d --rm \
     -p 9001:8000 \
     -e ABR_APP__PORT=8000 \
     -e ABR_APP__BASE_URL= \
     -e ABR_APP__DOWNLOAD_DIR=/audiobooks \
     -e ABR_APP__CONFIG_DIR=/config \
     -v $(pwd)/config:/config \
     -v $(pwd)/config/database:/config/database \
     -v /path/to/torrents:/downloads \
     -v /path/to/processing:/processing \
     -v /path/to/audiobooks:/audiobooks \
     ghcr.io/brownster/audiobookrequest:latest
   ```
   - `/config` persists the DB/settings.
   - `/config/database` keeps SQLite under /config/database (optional but tidy).
   - `/downloads` should match the qB remote download root.
   - `/processing` (optional) for tmp/intermediate processing if you want to move it off the root FS.
   - `/audiobooks` is where finished m4b files are written (point ABS to this).

2. Run migrations (once per fresh DB):
   ```bash
   docker exec -it <container> alembic upgrade heads
   ```

3. Open http://localhost:9001 and create the first admin user.

## Configure MAM + qBittorrent
1. Settings ▸ MAM: paste your `mam_id` cookie.
2. Settings ▸ qBittorrent: set WebUI URL/user/pass, seed target, and path mappings:
   - `qB Remote Download Path`: what qB reports (e.g., `/` or `/mnt/seedbox/Downloads`).
   - `qB Local Path Prefix`: where that path is mounted on the host (e.g., `/downloads`).
3. Set `ABR_APP__DOWNLOAD_DIR` to your ABS library/watch path (e.g., `/audiobooks`) and restart.
4. Use “Check on MAM” or “Auto download via MAM” from search/wishlist; monitor `/downloads`.

## Audiobookshelf
Settings ▸ Audiobookshelf: enter base URL + API token and choose the library. ABR will mark existing items as downloaded and trigger scans after successful jobs.

## AI (optional)
Settings ▸ AI:
- Provider: `openai` (enter API key, endpoint `https://api.openai.com`, model `gpt-4o-mini`) or `ollama` (local endpoint and model name).
- Use “Test Connection”, then “Generate now” on AI recommendations.

## Useful environment variables
- `ABR_APP__PORT` (default 8000)
- `ABR_APP__BASE_URL` (set if serving under a subpath, else leave empty)
- `ABR_APP__DOWNLOAD_DIR` (final audiobook output; bind-mount it)
- `ABR_APP__DEFAULT_REGION` (audible region, e.g., `uk`)
- `ABR_DB__USE_POSTGRES=true` plus `ABR_DB__POSTGRES_*` if using Postgres; otherwise SQLite in `/config`.
- `ABR_APP__INIT_ROOT_USERNAME` / `ABR_APP__INIT_ROOT_PASSWORD` to seed the first admin on fresh installs.

## Development (local)
```bash
uv sync
uv run alembic upgrade heads
UV_CACHE_DIR=.uv_cache uv run fastapi dev --host 127.0.0.1 --port 8000
```
Set envs in `.env.local` as needed.

## Troubleshooting
- No CSS / “unstyled” UI in Docker: ensure `ABR_APP__BASE_URL` is empty when serving at `/`, and map host port to container 8000.
- Post‑processing “path does not exist”: fix qB Remote/Local path mapping and confirm files land under your `/downloads` bind.
- OpenAI 400/empty recs: use exact model IDs (`gpt-4o-mini`), valid API key, and default endpoint `https://api.openai.com`.
- Migrations missing columns: `alembic upgrade heads` inside the container.

Running the application is best done in multiple terminals:

1. Start FastAPI dev mode:

```sh
just dev # or simply 'just d'
# or if you don't have 'just':
uv run fastapi dev
```

Website can be visited at http://localhost:8000.

2. Install daisyUI and start Tailwindcss watcher. Required for any CSS styling.

```sh
just tailwind # or simply 'just tw'

# or if you don't have 'just':
npm i
uv run tailwindcss -i static/tw.css -o static/globals.css --watch
# Alternatively npx can be used to run tailwindcss
npx @tailwindcss/cli@4 -i static/tw.css -o static/globals.css --watch
```

3. _Optional:_ Start browser-sync. This hot reloads the website when the html template or python files are modified:

```sh
browser-sync http://localhost:8000 --files templates/** --files app/**
```

**NOTE**: Website has to be visited at http://localhost:3000 instead.

## Docker Compose

The docker compose can also be used to run the app locally with Postgres and optional Gotify:

```bash
docker compose --profile local up --build
```

Key points:
- Web UI: http://localhost:8012 (mapped from container 8000).
- Data/config: mounted at `./config` -> `/config` in the container.
- Database: Postgres service `psql` preconfigured via env vars (user `abr`/`password`, db `audiobookrequest`).
- Migrations run automatically on start; no extra steps needed.
- Configure MyAnonamouse in-app under `Settings > MAM` (session ID, client, seeding targets).

# Docs

[Hugo](https://gohugo.io) is used to generate the docs page. It can be found in the `/docs` directory.

# Tools

AudioBookRequest builds on top of a some other great tools. A big thanks goes out to these developers.

- [Audimeta](https://github.com/Vito0912/AudiMeta) - Main audiobook metadata provider. Active in development and quick to fix issues.
- [Audnexus](https://github.com/laxamentumtech/audnexus) - Backup audiobook metadata provider.
- [External Audible API](https://audible.readthedocs.io/en/latest/misc/external_api.html) - Audible exposes key API endpoints which are used to, for example, search for books.
