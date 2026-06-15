from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    gamma_api_url: str = Field("https://gamma-api.polymarket.com", alias="GAMMA_API_URL")
    rtds_ws_url: str = Field("wss://ws-live-data.polymarket.com", alias="RTDS_WS_URL")
    clob_ws_url: str = Field(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market", alias="CLOB_WS_URL"
    )
    discovery_interval_sec: int = Field(30, alias="DISCOVERY_INTERVAL_SEC")
    log_level: str = Field("INFO", alias="LOG_LEVEL")


settings = Settings()  # type: ignore[call-arg]
