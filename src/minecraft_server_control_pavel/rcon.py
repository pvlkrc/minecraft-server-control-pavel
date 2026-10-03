import asyncio
import logging
import re

from aiomcrcon import Client
from fastapi import HTTPException

from .config import settings

logger = logging.getLogger("rcon")

COLOR_RE = re.compile(r"§.")

# The Minecraft server has a bug with parallel RCON commands, so only one
# command runs at a time. This lock works inside one process only: run the
# api with a single worker.
_lock = asyncio.Lock()


async def rcon_many(cmds: list[str], user: str = "system") -> list[str]:
    """Send several commands over one RCON connection, one after another."""
    responses: list[str] = []
    async with _lock:
        try:
            async with Client(
                settings.rcon_host, settings.rcon_port, settings.rcon_password
            ) as client:
                for cmd in cmds:
                    logger.info("user=%s cmd=%r", user, cmd)
                    response, _ = await client.send_cmd(cmd)
                    responses.append(COLOR_RE.sub("", response).strip())
        except Exception as e:
            logger.warning("RCON failed: %r", e)
            raise HTTPException(502, f"RCON error: {type(e).__name__}") from e
    return responses


async def rcon(cmd: str, user: str = "system") -> str:
    """Send one command over RCON and return the response without color codes."""
    return (await rcon_many([cmd], user))[0]
