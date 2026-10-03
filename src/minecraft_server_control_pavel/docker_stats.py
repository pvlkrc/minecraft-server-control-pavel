"""CPU / RAM / uptime of this Compose project's containers.

Talks to the Docker API through docker-socket-proxy, which only allows
read-only GET requests on containers. The api never gets the real Docker
socket.
"""

import asyncio
import re
import socket
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import settings

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def _started_at(value: str) -> datetime | None:
    # Docker gives nanoseconds ("...:05.123456789Z"); Python wants max 6 digits.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _cpu_percent(stats: dict[str, Any]) -> float | None:
    cpu, pre = stats.get("cpu_stats", {}), stats.get("precpu_stats", {})
    try:
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        system_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
    except KeyError:
        return None
    if system_delta <= 0:
        return None
    cpus = cpu.get("online_cpus") or 1
    return round(cpu_delta / system_delta * cpus * 100, 1)


def _memory(stats: dict[str, Any]) -> tuple[int | None, int | None]:
    mem = stats.get("memory_stats", {})
    usage = mem.get("usage")
    if usage is None:
        return None, None
    # Same as `docker stats`: do not count the file cache.
    cache = mem.get("stats", {}).get("inactive_file", 0)
    return usage - cache, mem.get("limit")


async def _container_info(client: httpx.AsyncClient, container: dict[str, Any]) -> dict[str, Any]:
    cid = container["Id"]
    inspect, stats = await asyncio.gather(
        client.get(f"/containers/{cid}/json"),
        client.get(f"/containers/{cid}/stats", params={"stream": "false"}),
    )
    state = inspect.json().get("State", {})
    started = _started_at(state.get("StartedAt", ""))
    data = stats.json() if stats.status_code == 200 else {}
    used, limit = _memory(data)
    return {
        "service": container.get("Labels", {}).get(SERVICE_LABEL, cid[:12]),
        "status": state.get("Health", {}).get("Status") or state.get("Status"),
        "uptime_seconds": int((datetime.now(UTC) - started).total_seconds()) if started else None,
        "cpu_percent": _cpu_percent(data),
        "memory_bytes": used,
        "memory_limit_bytes": limit,
    }


async def container_stats() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=settings.docker_url, timeout=10) as client:
        # Our own container id is the hostname; its labels tell the project name.
        me = await client.get(f"/containers/{socket.gethostname()}/json")
        me.raise_for_status()
        project = me.json()["Config"]["Labels"][PROJECT_LABEL]
        listing = await client.get(
            "/containers/json",
            params={"filters": f'{{"label": ["{PROJECT_LABEL}={project}"]}}'},
        )
        listing.raise_for_status()
        containers = listing.json()
        result = await asyncio.gather(*(_container_info(client, c) for c in containers))
    return sorted(result, key=lambda c: c["service"])
