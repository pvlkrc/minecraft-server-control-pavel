"""Read-only access to the Minecraft server files.

Lists (whitelist, bans, ops) are read from the JSON files the server keeps up
to date, because they are more reliable than parsing RCON text. All changes
still go through RCON.
"""

import json
from collections import deque
from typing import Any

from .config import settings

# Only these keys from server.properties are shown. Never rcon.password.
PUBLIC_PROPERTIES = {
    "motd",
    "max-players",
    "difficulty",
    "gamemode",
    "white-list",
    "enforce-whitelist",
    "level-name",
    "pvp",
    "online-mode",
    "view-distance",
}


def read_json_list(name: str) -> list[dict[str, Any]]:
    path = settings.data_dir / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def read_properties() -> dict[str, str]:
    path = settings.data_dir / "server.properties"
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in PUBLIC_PROPERTIES:
            result[key] = value
    return result


def tail_log(lines: int) -> list[str]:
    path = settings.data_dir / "logs" / "latest.log"
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            # The panel polls over RCON every few seconds and each connection
            # logs two "RCON Client" lines; hide them so the log stays readable.
            useful = (line.rstrip("\n") for line in f if "RCON" not in line)
            return list(deque(useful, maxlen=lines))
    except FileNotFoundError:
        return []
