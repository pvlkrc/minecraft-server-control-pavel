# CLAUDE.md

Web control panel (API + web page) for a Minecraft Java server. It talks to
the server through RCON. Everything runs in Docker Compose. This is a learning
project for Python, FastAPI, async, Docker, PostgreSQL, Redis and CI.

## Stack

- Python 3.14, FastAPI, `aio-mc-rcon` (import name `aiomcrcon`)
- `pydantic-settings` reads config from environment variables / `.env`
- uv for dependencies (`pyproject.toml` + `uv.lock`, both committed)
- Docker Compose services: `minecraft` (`itzg/minecraft-server`), `api`
  (built from `Dockerfile`)
- Planned: PostgreSQL + SQLAlchemy 2.0 + Alembic, Redis, worker, ruff, mypy,
  pytest, CI (GitHub or Gitea Actions, not decided)

## Layout

`src/minecraft_server_control_pavel/`:
- `config.py`: `Settings` (RCON host/port/password, panel login, `data_dir`)
- `rcon.py`: `rcon(cmd, user)` / `rcon_many(cmds, user)` (several commands
  over one connection), the only place that sends RCON commands. Holds the
  lock, logs every command with the user, strips color codes.
- `restart.py`: restart countdown (chat warnings, then `stop`; Docker restarts
  the container because of `restart: unless-stopped`). State lives in the
  process, so a scheduled restart is lost when the api restarts.
- `server_files.py`: read-only access to the server's `whitelist.json`,
  `banned-players.json`, `ops.json`, `server.properties`, `logs/latest.log`
- `main.py`: validation types, HTTP Basic login, all endpoints under `/api`,
  `GET /` serves the web page
- `static/index.html`: the web page (plain HTML/CSS/JS, no build step),
  including the 2D player map (canvas, positions from `GET /api/map`)

Minecraft 26.x specifics (verified against the server JAR language file):
- game rules are snake_case (`keep_inventory`, `advance_time`, ...), the old
  camelCase names are gone. Allowlist: `BOOL_RULES` / `INT_RULES` in `main.py`.
- RCON sometimes joins multi-line answers without `\n` (e.g. `tick query`).
- `data get entity` returns SNBT; numbers can use E notation (`5.0E-4d`),
  parse with `snbt_numbers`.

Lists are read from the server's JSON files (more reliable than parsing RCON
text). All changes go through RCON. The `api` container mounts `./data`
read-only.

## Commands

- `docker compose up -d --build`: build and start everything
- Panel: http://127.0.0.1:8000, login from `.env` (`PANEL_USER`,
  `PANEL_PASSWORD`). API docs: http://127.0.0.1:8000/docs
- `docker compose logs -f api`: API log, includes every RCON command
- `uv sync`: local `.venv` for the editor. Running the API on the host
  (`uv run fastapi dev ...`) cannot reach RCON any more, because RCON is not
  published.

## Security rules

- RCON is never published from Docker. Only the `api` container reaches it, as
  `minecraft:25575`. Docker bypasses ufw for published ports.
- The panel is published on `127.0.0.1` by default (`PANEL_BIND`). Every route
  except `GET /api/health` needs login.
- Never put user input into an RCON command without validation:
  - player names: `PlayerName` type, `NAME_RE.fullmatch` (3–16 chars,
    `[A-Za-z0-9_]`). Use `fullmatch`, not `match` with `$`, because `$` also
    matches before a trailing newline.
  - free text (reasons, chat): length limit + `clean_text` removes control
    characters and `§`
  - IP addresses: `ipaddress.ip_address`
  - fixed choices (time, weather, difficulty, gamemode, restart delay):
    `Literal` types; game rules: allowlist dicts
- Allowlist of RCON commands: each endpoint sends one fixed command. Never add
  a generic "send any command" endpoint.
- Secrets only in `.env`, which is in `.gitignore` and `.dockerignore`.
- Only one RCON command at a time (the server has a bug with parallel
  commands). `rcon()` holds an `asyncio.Lock`, so the api runs with one
  uvicorn worker. When the worker also uses RCON, move the lock to Redis.
- When testing against the running server, use only names that fail
  validation. Valid names really get banned / opped. Do not test restart
  while players are online.

## Roadmap

1. Repo setup: git, `.gitignore`, `pyproject.toml`, ruff, pytest, first CI.
   Partly done: ruff, pytest and CI are missing.
2. Docker Compose with Minecraft and RCON. Done.
3. Endpoint with online players. Done.
4. Whitelist, bans, IP bans, ops, kick, gamemode, player details, game
   rules, TPS, restart with countdown, world controls, 2D player map, web
   panel. Done.
5. PostgreSQL + Alembic, audit log of all commands (now only in the api log).
6. Worker that polls players every minute and saves history; stats endpoints.
7. Scheduled world backups (`save-off` / `save-all` / `save-on`).
8. Roles (admin, moderator). Now there is one shared login.
9. Integration tests with a fake RCON server; nightly test with a real server.
10. Extras: chat from server log, Discord/Slack bot, Grafana.
