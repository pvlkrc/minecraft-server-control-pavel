from fastapi import FastAPI
from aiomcrcon import Client
from pydantic_settings import BaseSettings, SettingsConfigDict


app = FastAPI()

class Settings(BaseSettings):
    rcon_password: str
    rcon_port: int

    model_config = SettingsConfigDict(env_file=".env")   # step 3: read .env

settings = Settings()


@app.get("/players")
async def list_players():
    client = Client("localhost", settings.rcon_port, settings.rcon_password)
    await client.connect()
    response = await client.send_cmd("list")
    await client.close()

    text = response[0]
    parts = text.split(":", 1)

    # numbers
    words = parts[0].split(" ")
    online = int(words[2])
    max_players = int(words[7])

    # names
    names_text = parts[1].strip()
    if names_text == "":
        names = []
    else:
        names = names_text.split(", ")

    return {
        "online": online,
        "max": max_players,
        "players": names,
    }