from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Values come from environment variables (Docker Compose) or from .env
    # when the app runs on the host. Unknown keys in .env are ignored.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    rcon_host: str = "localhost"
    rcon_port: int = 25575
    rcon_password: str

    panel_user: str = "admin"
    panel_password: str

    # Minecraft server folder (whitelist.json, banned-players.json, logs, ...).
    # Mounted read-only into the api container.
    data_dir: Path = Path("data")

    # Read-only Docker API (docker-socket-proxy service) for CPU / RAM / uptime.
    docker_url: str = "http://docker-proxy:2375"


settings = Settings()
