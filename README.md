
AudiobookRequest (fork) – an end‑to‑end pipeline for Audible search, MyAnonamouse downloads, qBittorrent/seedbox handoff, post‑processing, and Audiobookshelf ingestion.

This README reflects the current fork: Prowlarr is removed, MAM + qBittorrent is the main path, and AI recommendations can use OpenAI or Ollama.

## Recent Security Improvements ✅

**Version 2.0** includes comprehensive security hardening with 18 critical, high, and medium priority fixes:

- **Path Traversal Protection**: Manual import paths are validated against `ABR_IMPORT_ROOT` to prevent unauthorized file access
- **SQL Injection Prevention**: PostgreSQL credentials properly URL-encoded
- **Resource Management**: HTTP session singleton prevents file descriptor leaks
- **Race Condition Fixes**: Thread-safe singleton pattern and job state locks prevent concurrent modification issues
- **API Timeout Protection**: All external API calls (Audible, Audnexus, MAM) have 30-second timeouts
- **Input Validation**: Torrent file validation, filename sanitization, and safe datetime parsing
- **Comprehensive Test Suite**: 60+ tests including 23 security-focused tests (see `TEST_RESULTS.md`)

For full details, see `tasks.md` in the repository.

## What it does
- Audible search with region selection; “Check on MAM” buttons from search, homepage carousels, and wishlist.
- Wishlist with pipeline status (pending → downloading → post‑processing → completed) and hourly auto‑rechecks for MAM‑unavailable items.
- MAM integration: search/download via qBittorrent (seedbox-friendly path mapping), auto-resume inactive torrents, and retry from wishlist/downloads.
- Post‑processing: merge/convert/tag, embed cover, clean temp files, and write into `author/title/book.m4b` under `ABR_APP__DOWNLOAD_DIR`.
- Downloads page: live status, Retry, and Remove actions for failed/stuck jobs.
- Audiobookshelf integration: marks existing items as downloaded and triggers scans after successful jobs.
- AI recommendations: pick OpenAI (`gpt-4o-mini` etc.) or Ollama for homepage/AI recs.
- Ebook support: toggle MAM search to Ebooks; processed files land in `author/title/book.<ext>` under `ABR_APP__BOOK_DIR`.
- Manual import: ingest existing audiobooks/ebooks from disk, search Audible/Audnexus for metadata, confirm or batch‑match multiple books, and run post‑processing/tagging (with series support) into your final library paths.

## Quick start (Docker)
1. Build or pull an image (example uses GHCR):
   ```bash
   docker run -d --rm \
     -p 9001:8000 \
     -e ABR_APP__PORT=8000 \
     -e ABR_APP__BASE_URL= \
     -e ABR_APP__DOWNLOAD_DIR=/audiobooks \
     -e ABR_APP__CONFIG_DIR=/config \
     -e ABR_IMPORT_ROOT=/downloads \
     -v $(pwd)/config:/config \
     -v $(pwd)/config/database:/config/database \
     -v /path/to/torrents:/downloads \
     -v /path/to/processing:/processing \
     -v /path/to/audiobooks:/audiobooks \
     ghcr.io/brownster/audiobookrequest:latest
   ```
   - `/config` persists the DB/settings.
   - `/config/database` keeps SQLite under /config/database (optional but tidy).
   - `/downloads` should match the qB remote download root (also used for manual import with `ABR_IMPORT_ROOT`).
   - `/processing` (optional) for tmp/intermediate processing if you want to move it off the root FS.
   - `/audiobooks` is where finished m4b files are written (point ABS to this).

2. Run migrations (once per fresh DB):
   ```bash
   docker exec -it <container> alembic upgrade heads
   ```

3. Open http://localhost:9001 and create the first admin user.

## Configure MAM + qBittorrent
1. Settings ▸ MAM: paste your cookies in the format `mam_id=<value>; uid=<value>` (both come from your myanonamouse.net cookies).
2. Settings ▸ qBittorrent: set WebUI URL/user/pass, seed target, and path mappings:
   - `qB Remote Download Path`: what qB reports (e.g., `/` or `/mnt/seedbox/Downloads`).
   - `qB Local Path Prefix`: where that path is mounted on the host (e.g., `/downloads`).
3. Set `ABR_APP__DOWNLOAD_DIR` (audiobooks) and `ABR_APP__BOOK_DIR` (ebooks) to your library paths and restart.
4. Use “Check on MAM” or “Auto download via MAM” from search/wishlist; monitor `/downloads`.

### Mounting a seedbox (sshfs example)
If qB is remote and you post-process locally, mount the seedbox download root:
```bash
sudo apt install sshfs  # or dnf/yum equivalent
mkdir -p /mnt/seedbox
sshfs user@seedbox:/path/to/Downloads /mnt/seedbox -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3
```
Then set qB Remote Path to `/path/to/Downloads` and Local Prefix to `/mnt/seedbox`.

## Audiobookshelf
Settings ▸ Audiobookshelf: enter base URL + API token and choose the library. ABR will mark existing items as downloaded and trigger scans after successful jobs.

## AI (optional)
Settings ▸ AI:
- Provider: `openai` (enter API key, endpoint `https://api.openai.com`, model `gpt-4o-mini`) or `ollama` (local endpoint and model name).
- Use “Test Connection”, then “Generate now” on AI recommendations.

## Useful environment variables

### Core Configuration
- `ABR_APP__PORT` (default 8000)
- `ABR_APP__BASE_URL` (set if serving under a subpath, else leave empty)
- `ABR_APP__DOWNLOAD_DIR` (final audiobook output; bind-mount it)
- `ABR_APP__BOOK_DIR` (final ebook output; bind-mount it)
- `ABR_APP__DEFAULT_REGION` (audible region, e.g., `uk`)
- `ABR_APP__INIT_ROOT_USERNAME` / `ABR_APP__INIT_ROOT_PASSWORD` to seed the first admin on fresh installs

### Security Configuration
- `ABR_IMPORT_ROOT` (required for manual import) – restricts file browsing to this directory to prevent path traversal attacks. Set to your import source directory (e.g., `/mnt/imports` or `/downloads`)

### Database Configuration
- `ABR_DB__USE_POSTGRES=true` to use PostgreSQL instead of SQLite
- `ABR_DB__POSTGRES_HOST` (default: localhost)
- `ABR_DB__POSTGRES_PORT` (default: 5432)
- `ABR_DB__POSTGRES_USER` (credentials are automatically URL-encoded for security)
- `ABR_DB__POSTGRES_PASSWORD` (special characters like `@`, `:`, `/` are safely encoded)
- `ABR_DB__POSTGRES_DB` (default: audiobookrequest)
- `ABR_DB__POSTGRES_SSL_MODE` (default: prefer)

## Development (local)
```bash
uv sync
uv run alembic upgrade heads
UV_CACHE_DIR=.uv_cache uv run fastapi dev --host 127.0.0.1 --port 8000
```
Set envs in `.env.local` as needed.

### Testing
Run the comprehensive test suite including security tests:
```bash
# Run all tests
uv run pytest tests/ -v

# Run security tests only
uv run pytest tests/test_security_fixes.py -v

# Run with coverage
uv run pytest tests/ --cov=app --cov-report=html
```

All 60+ tests should pass. See `TEST_RESULTS.md` for detailed test documentation.

## Troubleshooting

### General Issues
- **No CSS / "unstyled" UI in Docker**: ensure `ABR_APP__BASE_URL` is empty when serving at `/`, and map host port to container 8000.
- **Post‑processing "path does not exist"**: fix qB Remote/Local path mapping and confirm files land under your `/downloads` bind.
- **OpenAI 400/empty recs**: use exact model IDs (`gpt-4o-mini`), valid API key, and default endpoint `https://api.openai.com`.
- **Migrations missing columns**: `alembic upgrade heads` inside the container.

### Security-Related Issues
- **Manual import "Path must be within configured import directories"**: Set `ABR_IMPORT_ROOT` environment variable to allow file browsing (e.g., `-e ABR_IMPORT_ROOT=/downloads`).
- **PostgreSQL connection errors with special characters in password**: Credentials are now automatically URL-encoded. If you're still having issues, check `ABR_DB__POSTGRES_*` settings.
- **Timeout errors on external APIs**: External APIs (Audible, Audnexus, MAM) have 30-second timeouts. If you're experiencing frequent timeouts, check your network connectivity.
- **Session/authentication issues**: Clear browser cookies if experiencing unexpected login problems after upgrade.

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

## Security Best Practices

AudioBookRequest v2.0 includes comprehensive security improvements. Follow these best practices:

### Production Deployment
1. **Set `ABR_IMPORT_ROOT`**: Always configure this when using manual import to prevent path traversal
2. **Use Strong Credentials**: PostgreSQL passwords with special characters are automatically URL-encoded
3. **HTTPS/Reverse Proxy**: Deploy behind nginx/Traefik with HTTPS in production
4. **Regular Updates**: Keep dependencies updated with `uv sync` and rebuild containers
5. **Database Backups**: Regular backups of `/config/database` (SQLite) or PostgreSQL database

### Network Security
- **Firewall Rules**: Restrict access to port 8000/9001 to trusted networks only
- **API Keys**: Rotate OpenAI/Ollama API keys regularly if exposed
- **MAM Cookies**: Keep MyAnonamouse session cookies secure; they're stored encrypted in the database

### Monitoring
- **Check Logs**: Monitor application logs for authentication failures or path traversal attempts
- **Resource Usage**: HTTP sessions are now managed efficiently to prevent file descriptor exhaustion
- **Test Suite**: Run `uv run pytest tests/test_security_fixes.py` after updates to verify security features

For detailed security improvements, see `tasks.md` and `TEST_RESULTS.md`.

# Tools

AudioBookRequest builds on top of a some other great tools. A big thanks goes out to these developers.

- [Audimeta](https://github.com/Vito0912/AudiMeta) – Main audiobook metadata provider.
- [Audnexus](https://github.com/laxamentumtech/audnexus) – Backup audiobook metadata provider.
- [External Audible API](https://audible.readthedocs.io/en/latest/misc/external_api.html) – Audible search and metadata.
- [MyAnonamouse](https://www.myanonamouse.net/) – Primary torrent source for MAM searches.
- [qBittorrent](https://github.com/qbittorrent/qBittorrent) – Torrent client used for downloads/seeding.
- [FFmpeg](https://ffmpeg.org/) – Post-processing (merge/convert/tag/cover embed).
