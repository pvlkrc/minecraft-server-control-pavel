"""Scheduled server restart with countdown messages.

The api sends "stop" over RCON. The minecraft container then exits and Docker
starts it again because of `restart: unless-stopped` in docker-compose.yml.
"""

import asyncio
import logging
import time

from .rcon import rcon

logger = logging.getLogger("restart")

# Moments (seconds before the restart) when players get a warning.
WARN_AT = [600, 300, 120, 60, 30, 10, 5, 4, 3, 2, 1]

_task: asyncio.Task[None] | None = None
_restart_at: float | None = None


def _human(seconds: int) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes > 1 else ''}"
    return f"{seconds} second{'s' if seconds > 1 else ''}"


async def _countdown(delay: int, user: str) -> None:
    global _task, _restart_at
    try:
        remaining = delay
        for point in [delay] + [p for p in WARN_AT if p < delay]:
            await asyncio.sleep(remaining - point)
            remaining = point
            await rcon(f"say Server restart in {_human(point)}", user)
        await asyncio.sleep(remaining)
        await rcon("say Server is restarting now", user)
        await rcon("stop", user)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("restart countdown failed")
    finally:
        # cancel() may already have cleared the state for a new schedule.
        if _task is asyncio.current_task():
            _task = None
            _restart_at = None


def schedule(delay: int, user: str) -> bool:
    """Start the countdown. Returns False when a restart is already scheduled."""
    global _task, _restart_at
    if _task is not None:
        return False
    _restart_at = time.time() + delay
    _task = asyncio.create_task(_countdown(delay, user))
    return True


def cancel() -> bool:
    global _task, _restart_at
    if _task is None:
        return False
    _task.cancel()
    _task = None
    _restart_at = None
    return True


def seconds_left() -> int | None:
    if _restart_at is None:
        return None
    return max(0, round(_restart_at - time.time()))
