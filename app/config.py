"""Config loaded from environment."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:////data/dmrstream.db"
    secret_key: str = "dev-only-change-me"

    icecast_internal: str = "http://icecast:8000"
    icecast_public_url: str = "http://localhost:8000"
    icecast_mount: str = "/dmr.opus"

    brandmeister_api_key: str = ""
    app_callsign: str = "DMR"
    app_description: str = "Live DMR audio stream and talkgroup activity."

    talkgroups: str = "2350,2351,2352,2353,235,3100,23520,23526,23531,23562,235175"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def current_tgs(self) -> list[int]:
        return [int(t.strip()) for t in self.talkgroups.split(",") if t.strip()]


settings = Settings()
