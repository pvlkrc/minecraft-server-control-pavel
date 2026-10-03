"""Access to the Minecraft server files.

Lists (whitelist, bans, ops) are read from the JSON files the server keeps up
to date, because they are more reliable than parsing RCON text. All game
changes still go through RCON. The only file the api writes is
server.properties (allowed keys only).
"""

import gzip
import json
import re
from collections import deque
from typing import Any

from .config import settings
from .nbt import read_nbt

# Only these keys from server.properties are shown. Never rcon.password or
# management-server-secret.
PUBLIC_PROPERTIES = {
    "motd",
    "max-players",
    "difficulty",
    "gamemode",
    "force-gamemode",
    "allow-flight",
    "white-list",
    "enforce-whitelist",
    "level-name",
    "online-mode",
    "view-distance",
    "simulation-distance",
    "player-idle-timeout",
    "pause-when-empty-seconds",
    "spawn-protection",
}

_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def read_json_list(name: str) -> list[dict[str, Any]]:
    path = settings.data_dir / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _unescape(value: str) -> str:
    value = _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
    return value.replace("\\\\", "\\")


def _escape(value: str) -> str:
    # Java .properties style: backslash and non-ASCII characters are escaped.
    out = []
    for ch in value.replace("\\", "\\\\"):
        if ord(ch) < 128:
            out.append(ch)
        else:
            # Characters outside the BMP become two \u escapes (UTF-16 pair).
            units = ch.encode("utf-16-be")
            for i in range(0, len(units), 2):
                out.append(f"\\u{units[i]:02x}{units[i + 1]:02x}")
    return "".join(out)


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
            result[key] = _unescape(value)
    return result


def write_properties(updates: dict[str, str]) -> None:
    """Change the given keys in server.properties, keep everything else."""
    path = settings.data_dir / "server.properties"
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    for i, line in enumerate(lines):
        key = line.partition("=")[0]
        if not line.startswith("#") and key in remaining:
            lines[i] = f"{key}={_escape(remaining.pop(key))}"
    lines += [f"{key}={_escape(value)}" for key, value in remaining.items()]
    # write_text truncates the same file (same inode), so bind mounts keep working.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_spawn() -> dict[str, Any] | None:
    """World spawn from level.dat (Minecraft 26.x: Data.spawn.pos / dimension)."""
    level = settings.data_dir / read_properties().get("level-name", "world") / "level.dat"
    try:
        root = read_nbt(gzip.decompress(level.read_bytes()))
        spawn = root["Data"]["spawn"]
        x, y, z = spawn["pos"]
        return {"x": x, "y": y, "z": z, "dimension": spawn.get("dimension", "minecraft:overworld")}
    except (FileNotFoundError, KeyError, ValueError, TypeError, OSError):
        return None


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
