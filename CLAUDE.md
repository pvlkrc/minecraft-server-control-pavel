# CLAUDE.md

Web API (later also a simple web page) that controls a Minecraft Java server
through RCON. This is a learning project for Python, FastAPI, async, Docker,
PostgreSQL, Redis and CI.

## Stack

- Python 3.14, FastAPI, `aio-mc-rcon` (import name `aiomcrcon`)
- `pydantic-settings` reads config from `.env`
- uv for dependencies (`pyproject.toml` + `uv.lock`, both committed)
- Docker Compose: service `minecraft` (`itzg/minecraft-server`)
- Planned: PostgreSQL + SQLAlchemy 2.0 + Alembic, Redis, ruff, mypy, pytest,
  CI (GitHub or Gitea Actions, not decided)

## Layout

- `src/minecraft_server_control_pavel/main.py`: FastAPI app, settings,
  `rcon()` helper, endpoints
- `docker-compose.yml`: Minecraft server, game port 25565, RCON on
  `127.0.0.1:25575` only
- `.env` (not in git): `RCON_PASSWORD`, `RCON_PORT`. Template: `.env.example`

## Commands

- `uv sync`: install dependencies into `.venv`
- `docker compose up -d`: start the Minecraft server
- `uv run fastapi dev src/minecraft_server_control_pavel/main.py`: run the
  API. Start it from the repo root, because `.env` is found relative to the
  current directory.

## Security rules

- RCON must never be reachable from outside the machine. In Compose, publish
  it only on `127.0.0.1`. Docker bypasses ufw for published ports.
- Never put user input into an RCON command without validation. Player names
  use `NAME_RE.fullmatch` (3–16 chars, `[A-Za-z0-9_]`). Use `fullmatch`, not
  `match` with `$`, because `$` also matches before a trailing newline.
- Use an allowlist of RCON commands.
- Secrets only in `.env`, which is in `.gitignore`.
- Only one RCON command at a time (the server has a bug with parallel
  commands). `rcon()` holds `rcon_lock` (`asyncio.Lock`). This lock works only
  inside one process; when the worker also uses RCON, move the lock to Redis.
- When testing `/ban` against the running server, use only names that fail
  validation. Valid names really get banned.

## Roadmap

1. Repo setup: git, `.gitignore`, `pyproject.toml`, ruff, pytest, first CI.
   Partly done: ruff, pytest and CI are missing.
2. Docker Compose with Minecraft and RCON. Done.
3. Endpoint with online players (`GET /players`). Done.
4. Whitelist and ban management with input validation. In progress:
   `POST /ban` done.
5. PostgreSQL + Alembic, audit log of all commands.
6. Worker that polls players every minute and saves history; stats endpoints.
7. Scheduled world backups (`save-off` / `save-all` / `save-on`).
8. Login and roles (admin, moderator).
9. Integration tests with a fake RCON server; nightly test with a real server.
10. Extras: chat from server log, Discord/Slack bot, Grafana.
