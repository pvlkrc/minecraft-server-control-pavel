import asyncio
import re
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from aiomcrcon import Client


class Settings(BaseSettings):
    rcon_port: int
    rcon_password: str

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
app = FastAPI()

NAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
COLOR_RE = re.compile(r"§.")

rcon_lock = asyncio.Lock()


class BanRequest(BaseModel):
    player: str
    reason: str | None = None


async def rcon(cmd: str) -> str:
    async with rcon_lock:
        try:
            async with Client("localhost", settings.rcon_port, settings.rcon_password) as client:
                response, _ = await client.send_cmd(cmd)
        except Exception as e:
            raise HTTPException(502, f"RCON error: {e}")
    return COLOR_RE.sub("", response)


@app.get("/players")
async def list_players():
    text = await rcon("list")

    head, _, names_text = text.partition(":")
    numbers = re.findall(r"\d+", head)
    if len(numbers) < 2:
        raise HTTPException(502, f"Unexpected response: {text}")

    names_text = names_text.strip()
    names = names_text.split(", ") if names_text else []

    return {
        "online": int(numbers[0]),
        "max": int(numbers[1]),
        "players": names,
    }


@app.post("/ban")
async def ban(req: BanRequest):
    if not NAME_RE.fullmatch(req.player):
        raise HTTPException(400, "Invalid player name")

    cmd = f"ban {req.player}"
    if req.reason:
        reason = req.reason.replace("\r", " ").replace("\n", " ").strip()
        cmd += " " + reason[:200]

    response = await rcon(cmd)
    return {"ok": True, "response": response}