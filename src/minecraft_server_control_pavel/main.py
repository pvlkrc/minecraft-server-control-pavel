import ipaddress
import logging
import re
import secrets
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import httpx
from pydantic import AfterValidator, BaseModel, Field, model_validator

from . import docker_stats, restart
from .config import settings
from .rcon import rcon, rcon_many
from .server_files import (
    read_json_list,
    read_properties,
    read_spawn,
    tail_log,
    write_properties,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

STATIC_DIR = Path(__file__).parent / "static"

# --- validation -------------------------------------------------------------

NAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
# Control characters (newlines!) and Minecraft color codes are never sent to RCON.
UNSAFE_TEXT_RE = re.compile(r"[\x00-\x1f\x7f§]")


def check_name(value: str) -> str:
    # fullmatch, not match + "$": "$" also matches before a trailing newline.
    if not NAME_RE.fullmatch(value):
        raise ValueError("invalid player name (3-16 chars: A-Z, a-z, 0-9, _)")
    return value


def check_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise ValueError("invalid IP address") from None


def check_ip_or_name(value: str) -> str:
    try:
        return check_ip(value)
    except ValueError:
        pass
    try:
        return check_name(value)
    except ValueError:
        raise ValueError("must be an IP address or an online player name") from None


def clean_text(value: str) -> str:
    return UNSAFE_TEXT_RE.sub(" ", value).strip()


def not_empty(value: str) -> str:
    if not value:
        raise ValueError("must not be empty")
    return value


PlayerName = Annotated[str, AfterValidator(check_name)]
IpAddress = Annotated[str, AfterValidator(check_ip)]
IpOrName = Annotated[str, AfterValidator(check_ip_or_name)]
Reason = Annotated[str, Field(max_length=200), AfterValidator(clean_text)]
Message = Annotated[
    str, Field(max_length=256), AfterValidator(clean_text), AfterValidator(not_empty)
]


class NameBody(BaseModel):
    name: PlayerName


class BanBody(NameBody):
    reason: Reason | None = None


class IpBanBody(BaseModel):
    target: IpOrName
    reason: Reason | None = None


class KickBody(BaseModel):
    reason: Reason | None = None


class GamemodeBody(BaseModel):
    mode: Literal["survival", "creative", "adventure", "spectator"]


class EnabledBody(BaseModel):
    enabled: bool


class MessageBody(BaseModel):
    message: Message


class TimeBody(BaseModel):
    value: Literal["day", "noon", "night", "midnight"]


class WeatherBody(BaseModel):
    value: Literal["clear", "rain", "thunder"]


class DifficultyBody(BaseModel):
    value: Literal["peaceful", "easy", "normal", "hard"]


class GameruleBody(BaseModel):
    value: bool | int


class RestartBody(BaseModel):
    delay: Literal[10, 60, 300, 600]


class CommandResult(BaseModel):
    response: str


# --- game rules (allowlist) ---------------------------------------------------
# Names as used since Minecraft 1.21.11 / 26.x (snake_case).

BOOL_RULES = {
    "keep_inventory": "Keep inventory",
    "mob_griefing": "Mob griefing",
    "pvp": "PvP",
    "advance_time": "Daylight cycle",
    "advance_weather": "Weather cycle",
    "spawn_mobs": "Mob spawning",
    "spawn_phantoms": "Phantoms",
    "fall_damage": "Fall damage",
    "fire_damage": "Fire damage",
    "natural_health_regeneration": "Natural regeneration",
    "immediate_respawn": "Immediate respawn",
    "show_death_messages": "Death messages",
    "show_advancement_messages": "Advancement messages",
    "tnt_explodes": "TNT explodes",
    "locator_bar": "Locator bar",
}

INT_RULES = {
    "players_sleeping_percentage": ("Players sleeping to skip night (%)", 0, 100),
    "respawn_radius": ("Respawn radius", 0, 128),
    "random_tick_speed": ("Random tick speed", 0, 1000),
}

GAMERULE_VALUE_RE = re.compile(r"currently set to (\S+)$")

# --- auth -------------------------------------------------------------------

security = HTTPBasic(realm="Minecraft control panel")


def require_user(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> str:
    user_ok = secrets.compare_digest(
        credentials.username.encode(), settings.panel_user.encode()
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode(), settings.panel_password.encode()
    )
    if not (user_ok and password_ok):
        raise HTTPException(
            401, "Wrong user or password", headers={"WWW-Authenticate": "Basic"}
        )
    return credentials.username


User = Annotated[str, Depends(require_user)]

# --- app --------------------------------------------------------------------

app = FastAPI(title="Minecraft control panel")
api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Like FastAPI's default, but without echoing the input: it may be NaN,
    # which cannot be written as JSON and would turn the 422 into a 500.
    errors = [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Public, for the Docker health check. Does not touch RCON."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False, dependencies=[Depends(require_user)])
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def run(cmd: str, user: str) -> CommandResult:
    return CommandResult(response=await rcon(cmd, user))


# SNBT numbers, e.g. "12.5d", "-3f", "5.0E-4d" (Java uses E notation for tiny values).
SNBT_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")


def snbt_numbers(text: str) -> list[float]:
    return [float(x) for x in SNBT_NUMBER_RE.findall(text)]


def number(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


# Server --------------------------------------------------------------------


@api.get("/server")
async def server_info() -> dict[str, str]:
    return read_properties()


@api.get("/performance")
async def performance(user: User) -> dict[str, object]:
    text = await rcon("tick query", user)
    # RCON sometimes joins the lines without "\n", so cut at the next sentence too.
    status = re.split(r"\n|Target tick rate", text, maxsplit=1)[0].strip()
    target = re.search(r"Target tick rate: ([\d.,]+)", text)
    mspt = re.search(r"Average time per tick: ([\d.,]+)ms", text)
    pct = re.search(r"P50: ([\d.,]+)ms P95: ([\d.,]+)ms P99: ([\d.,]+)ms", text)

    target_tps = number(target.group(1)) if target else None
    avg_mspt = number(mspt.group(1)) if mspt else None
    tps = None
    if target_tps is not None and avg_mspt is not None:
        # The server can't run faster than the target rate.
        tps = target_tps if avg_mspt <= 0 else min(target_tps, 1000 / avg_mspt)

    return {
        "status": status,
        "tps": round(tps, 1) if tps is not None else None,
        "target_tps": target_tps,
        "mspt": avg_mspt,
        "p95_mspt": number(pct.group(2)) if pct else None,
        "raw": text,
    }


@api.get("/restart")
async def restart_status() -> dict[str, int | None]:
    return {"seconds_left": restart.seconds_left()}


@api.post("/restart")
async def restart_schedule(body: RestartBody, user: User) -> CommandResult:
    if not restart.schedule(body.delay, user):
        raise HTTPException(409, "A restart is already scheduled")
    return CommandResult(response=f"Restart in {body.delay} s scheduled")


@api.delete("/restart")
async def restart_cancel(user: User) -> CommandResult:
    if not restart.cancel():
        raise HTTPException(404, "No restart is scheduled")
    await rcon("say Server restart cancelled", user)
    return CommandResult(response="Restart cancelled")


def parse_list(text: str) -> dict[str, Any]:
    head, _, names_text = text.partition(":")
    numbers = re.findall(r"\d+", head)
    if len(numbers) < 2:
        raise HTTPException(502, f"Unexpected response: {text}")

    names_text = names_text.strip()
    names = names_text.split(", ") if names_text else []

    return {"online": int(numbers[0]), "max": int(numbers[1]), "players": names}


@api.get("/players")
async def list_players(user: User) -> dict[str, object]:
    return parse_list(await rcon("list", user))


@api.get("/map")
async def player_map(user: User) -> list[dict[str, object]]:
    """Position and dimension of every online player, for the 2D map."""
    players: list[str] = parse_list(await rcon("list", user))["players"]
    names = [n for n in players if NAME_RE.fullmatch(n)]
    if not names:
        return []
    cmds = []
    for n in names:
        cmds += [f"data get entity {n} Pos", f"data get entity {n} Dimension"]
    responses = await rcon_many(cmds, user)

    result: list[dict[str, object]] = []
    for i, name in enumerate(names):
        pos_text, dim_text = responses[2 * i], responses[2 * i + 1]
        pos_value = pos_text.partition("entity data: ")[2]
        pos = snbt_numbers(pos_value)
        dim = dim_text.partition("entity data: ")[2].strip().strip('"')
        if len(pos) == 3:  # player may have left in the meantime
            x, y, z = (round(v, 1) for v in pos)
            dimension = dim or "minecraft:overworld"
            result.append({"name": name, "x": x, "y": y, "z": z, "dimension": dimension})
    return result


GAMEMODES = ["survival", "creative", "adventure", "spectator"]
DETAIL_PATHS = ["Pos", "Health", "foodLevel", "XpLevel", "Dimension", "playerGameType"]


@api.get("/players/{name}")
async def player_details(name: PlayerName, user: User) -> dict[str, object]:
    responses = await rcon_many(
        [f"data get entity {name} {path}" for path in DETAIL_PATHS], user
    )
    values: dict[str, str] = {}
    for path, text in zip(DETAIL_PATHS, responses):
        if "No entity was found" in text or "No player was found" in text:
            raise HTTPException(404, f"{name} is not online")
        _, sep, value = text.partition("entity data: ")
        if sep:
            values[path] = value.strip()

    def num(path: str) -> float | None:
        found = snbt_numbers(values.get(path, ""))
        return found[0] if found else None

    def whole(path: str) -> int | None:
        value = num(path)
        return int(value) if value is not None else None

    pos = snbt_numbers(values.get("Pos", ""))
    mode = num("playerGameType")
    return {
        "name": name,
        "position": [round(x, 1) for x in pos] if len(pos) == 3 else None,
        "dimension": values.get("Dimension", "").strip('"') or None,
        "health": num("Health"),
        "food": whole("foodLevel"),
        "level": whole("XpLevel"),
        "gamemode": GAMEMODES[int(mode)] if mode is not None and 0 <= mode < 4 else None,
    }


@api.post("/players/{name}/kick")
async def kick(name: PlayerName, body: KickBody, user: User) -> CommandResult:
    cmd = f"kick {name}"
    if body.reason:
        cmd += f" {body.reason}"
    return await run(cmd, user)


@api.put("/players/{name}/gamemode")
async def set_gamemode(name: PlayerName, body: GamemodeBody, user: User) -> CommandResult:
    return await run(f"gamemode {body.mode} {name}", user)


@api.post("/say")
async def say(body: MessageBody, user: User) -> CommandResult:
    return await run(f"say {body.message}", user)


@api.post("/save")
async def save(user: User) -> CommandResult:
    return await run("save-all", user)


@api.put("/time")
async def set_time(body: TimeBody, user: User) -> CommandResult:
    return await run(f"time set {body.value}", user)


@api.put("/weather")
async def set_weather(body: WeatherBody, user: User) -> CommandResult:
    return await run(f"weather {body.value}", user)


@api.put("/difficulty")
async def set_difficulty(body: DifficultyBody, user: User) -> CommandResult:
    return await run(f"difficulty {body.value}", user)


@api.get("/logs")
async def logs(lines: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[str]:
    return tail_log(lines)


# Game rules ----------------------------------------------------------------


@api.get("/gamerules")
async def list_gamerules(user: User) -> list[dict[str, object]]:
    names = list(BOOL_RULES) + list(INT_RULES)
    responses = await rcon_many([f"gamerule {n}" for n in names], user)

    result: list[dict[str, object]] = []
    for name, text in zip(names, responses):
        found = GAMERULE_VALUE_RE.search(text)
        raw = found.group(1) if found else None
        if name in BOOL_RULES:
            value: object = {"true": True, "false": False}.get(raw or "")
            result.append({"name": name, "label": BOOL_RULES[name], "type": "bool", "value": value})
        else:
            label, low, high = INT_RULES[name]
            value = int(raw) if raw and raw.lstrip("-").isdigit() else None
            result.append(
                {"name": name, "label": label, "type": "int", "value": value, "min": low, "max": high}
            )
    return result


@api.put("/gamerules/{name}")
async def set_gamerule(name: str, body: GameruleBody, user: User) -> CommandResult:
    value = body.value
    if name in BOOL_RULES:
        if not isinstance(value, bool):
            raise HTTPException(422, f"{name} needs true or false")
        text = "true" if value else "false"
    elif name in INT_RULES:
        _, low, high = INT_RULES[name]
        if isinstance(value, bool) or not low <= value <= high:
            raise HTTPException(422, f"{name} needs a number from {low} to {high}")
        text = str(value)
    else:
        raise HTTPException(404, "Unknown or not allowed game rule")
    return await run(f"gamerule {name} {text}", user)


# Bans ----------------------------------------------------------------------


@api.get("/bans")
async def list_bans() -> list[dict[str, object]]:
    return read_json_list("banned-players.json")


@api.post("/bans")
async def ban(body: BanBody, user: User) -> CommandResult:
    cmd = f"ban {body.name}"
    if body.reason:
        cmd += f" {body.reason}"
    return await run(cmd, user)


@api.delete("/bans/{name}")
async def unban(name: PlayerName, user: User) -> CommandResult:
    return await run(f"pardon {name}", user)


@api.get("/ip-bans")
async def list_ip_bans() -> list[dict[str, object]]:
    return read_json_list("banned-ips.json")


@api.post("/ip-bans")
async def ban_ip(body: IpBanBody, user: User) -> CommandResult:
    cmd = f"ban-ip {body.target}"
    if body.reason:
        cmd += f" {body.reason}"
    return await run(cmd, user)


@api.delete("/ip-bans/{ip}")
async def unban_ip(ip: IpAddress, user: User) -> CommandResult:
    return await run(f"pardon-ip {ip}", user)


# Whitelist -----------------------------------------------------------------


@api.get("/whitelist")
async def list_whitelist() -> dict[str, object]:
    props = read_properties()
    return {
        "enabled": props.get("white-list") == "true",
        "players": read_json_list("whitelist.json"),
    }


@api.post("/whitelist")
async def whitelist_add(body: NameBody, user: User) -> CommandResult:
    return await run(f"whitelist add {body.name}", user)


@api.delete("/whitelist/{name}")
async def whitelist_remove(name: PlayerName, user: User) -> CommandResult:
    return await run(f"whitelist remove {name}", user)


@api.put("/whitelist/enabled")
async def whitelist_enabled(body: EnabledBody, user: User) -> CommandResult:
    return await run("whitelist on" if body.enabled else "whitelist off", user)


# Operators -----------------------------------------------------------------


@api.get("/ops")
async def list_ops() -> list[dict[str, object]]:
    return read_json_list("ops.json")


@api.post("/ops")
async def op(body: NameBody, user: User) -> CommandResult:
    return await run(f"op {body.name}", user)


@api.delete("/ops/{name}")
async def deop(name: PlayerName, user: User) -> CommandResult:
    return await run(f"deop {name}", user)


# Player tools --------------------------------------------------------------

Dimension = Literal["minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"]
WORLD_LIMIT = 29_999_984


def coordinate(low: float, high: float) -> Any:
    return Field(None, ge=low, le=high, allow_inf_nan=False)


class TellBody(BaseModel):
    message: Message


class TeleportBody(BaseModel):
    target: Literal["player", "spawn", "coords"]
    player: PlayerName | None = None
    x: float | None = coordinate(-WORLD_LIMIT, WORLD_LIMIT)
    y: float | None = coordinate(-64, 320)
    z: float | None = coordinate(-WORLD_LIMIT, WORLD_LIMIT)
    dimension: Dimension = "minecraft:overworld"

    @model_validator(mode="after")
    def check_target(self) -> "TeleportBody":
        if self.target == "player" and self.player is None:
            raise ValueError("player is required")
        if self.target == "coords" and None in (self.x, self.y, self.z):
            raise ValueError("x, y and z are required")
        return self


# Allowlist: item id -> (label, max count per give)
ITEMS: dict[str, tuple[str, int]] = {
    "stone_sword": ("Stone sword", 1),
    "stone_pickaxe": ("Stone pickaxe", 1),
    "stone_axe": ("Stone axe", 1),
    "stone_shovel": ("Stone shovel", 1),
    "iron_sword": ("Iron sword", 1),
    "iron_pickaxe": ("Iron pickaxe", 1),
    "iron_axe": ("Iron axe", 1),
    "iron_shovel": ("Iron shovel", 1),
    "shield": ("Shield", 1),
    "bow": ("Bow", 1),
    "arrow": ("Arrow", 64),
    "iron_helmet": ("Iron helmet", 1),
    "iron_chestplate": ("Iron chestplate", 1),
    "iron_leggings": ("Iron leggings", 1),
    "iron_boots": ("Iron boots", 1),
    "bread": ("Bread", 64),
    "cooked_beef": ("Steak", 64),
    "baked_potato": ("Baked potato", 64),
    "golden_apple": ("Golden apple", 16),
    "oak_log": ("Oak log", 64),
    "oak_planks": ("Oak planks", 64),
    "cobblestone": ("Cobblestone", 64),
    "torch": ("Torch", 64),
    "crafting_table": ("Crafting table", 1),
    "furnace": ("Furnace", 1),
    "chest": ("Chest", 4),
    "white_bed": ("Bed", 1),
    "water_bucket": ("Water bucket", 1),
    "iron_ingot": ("Iron ingot", 64),
    "diamond": ("Diamond", 64),
    "ender_pearl": ("Ender pearl", 16),
    "experience_bottle": ("Bottle o' Enchanting", 64),
    "firework_rocket": ("Firework rocket", 64),
    "elytra": ("Elytra", 1),
}

KITS: dict[str, tuple[str, list[tuple[str, int]]]] = {
    "starter": ("Starter kit", [
        ("stone_sword", 1), ("stone_pickaxe", 1), ("stone_axe", 1), ("stone_shovel", 1),
        ("bread", 16), ("torch", 32), ("oak_log", 16), ("crafting_table", 1), ("white_bed", 1),
    ]),
    "iron": ("Iron kit", [
        ("iron_helmet", 1), ("iron_chestplate", 1), ("iron_leggings", 1), ("iron_boots", 1),
        ("iron_sword", 1), ("iron_pickaxe", 1), ("shield", 1), ("cooked_beef", 32),
    ]),
    "builder": ("Builder kit", [
        ("oak_planks", 64), ("cobblestone", 64), ("torch", 64), ("chest", 2),
        ("crafting_table", 1), ("furnace", 1), ("water_bucket", 1),
    ]),
}


class GiveBody(BaseModel):
    item: str
    count: int = Field(1, ge=1, le=64)


class KitBody(BaseModel):
    kit: str


def fmt(value: float) -> str:
    return f"{value:.2f}"


@api.post("/players/{name}/tell")
async def tell(name: PlayerName, body: TellBody, user: User) -> CommandResult:
    return await run(f"tell {name} {body.message}", user)


@api.post("/players/{name}/teleport")
async def teleport(name: PlayerName, body: TeleportBody, user: User) -> CommandResult:
    if body.target == "player":
        return await run(f"tp {name} {body.player}", user)
    if body.target == "spawn":
        spawn = read_spawn()
        if spawn is None:
            raise HTTPException(500, "World spawn not found in level.dat")
        # +0.5: middle of the block
        x, y, z = spawn["x"] + 0.5, spawn["y"], spawn["z"] + 0.5
        return await run(f"execute in {spawn['dimension']} run tp {name} {fmt(x)} {fmt(y)} {fmt(z)}", user)
    assert body.x is not None and body.y is not None and body.z is not None
    cmd = f"execute in {body.dimension} run tp {name} {fmt(body.x)} {fmt(body.y)} {fmt(body.z)}"
    return await run(cmd, user)


@api.get("/items")
async def items() -> dict[str, object]:
    return {
        "items": [{"id": k, "label": v[0], "max": v[1]} for k, v in ITEMS.items()],
        "kits": [
            {"id": k, "label": label, "items": [f"{n}× {ITEMS[i][0]}" for i, n in content]}
            for k, (label, content) in KITS.items()
        ],
    }


@api.post("/players/{name}/give")
async def give(name: PlayerName, body: GiveBody, user: User) -> CommandResult:
    if body.item not in ITEMS:
        raise HTTPException(422, "Item is not allowed")
    label, max_count = ITEMS[body.item]
    if body.count > max_count:
        raise HTTPException(422, f"{label}: at most {max_count}")
    return await run(f"give {name} minecraft:{body.item} {body.count}", user)


@api.post("/players/{name}/kit")
async def give_kit(name: PlayerName, body: KitBody, user: User) -> CommandResult:
    if body.kit not in KITS:
        raise HTTPException(422, "Unknown kit")
    label, content = KITS[body.kit]
    responses = await rcon_many(
        [f"give {name} minecraft:{item} {count}" for item, count in content], user
    )
    if any("No player was found" in r for r in responses):
        raise HTTPException(404, f"{name} is not online")
    return CommandResult(response=f"Gave {label} to {name}")


# Server settings -------------------------------------------------------------


class PropertiesBody(BaseModel):
    """Editable server.properties keys. difficulty and spawn-protection are not
    here: docker-compose.yml sets them, so the image would overwrite them."""

    motd: Annotated[str, Field(max_length=100), AfterValidator(clean_text)] | None = None
    max_players: int | None = Field(None, ge=1, le=1000)
    view_distance: int | None = Field(None, ge=3, le=32)
    simulation_distance: int | None = Field(None, ge=3, le=32)
    gamemode: Literal["survival", "creative", "adventure", "spectator"] | None = None
    force_gamemode: bool | None = None
    allow_flight: bool | None = None
    player_idle_timeout: int | None = Field(None, ge=0, le=1440)
    pause_when_empty_seconds: int | None = Field(None, ge=0, le=86400)


@api.put("/server/properties")
async def update_properties(body: PropertiesBody, user: User) -> CommandResult:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "Nothing to change")
    updates = {
        key.replace("_", "-"): ("true" if v else "false") if isinstance(v, bool) else str(v)
        for key, v in changes.items()
    }
    write_properties(updates)
    logging.getLogger("properties").info("user=%s changed %s", user, updates)
    return CommandResult(response="Saved. Restart the server to apply the changes.")


@api.get("/seed")
async def seed(user: User) -> dict[str, str | None]:
    found = re.search(r"\[(-?\d+)\]", await rcon("seed", user))
    return {"seed": found.group(1) if found else None}


@api.get("/spawn")
async def spawn() -> dict[str, object] | None:
    return read_spawn()


@api.get("/system")
async def system() -> list[dict[str, object]]:
    try:
        return await docker_stats.container_stats()
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logging.getLogger("system").warning("docker stats failed: %r", e)
        raise HTTPException(502, "Docker stats not available") from e


app.include_router(api)
