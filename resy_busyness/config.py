"""Settings. Every knob comes from the environment or `.env`; nothing is hardcoded."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    resy_api_key: str = Field(
        default="", description="Public key shipped in resy.com's web client bundle"
    )
    resy_api_base: str = "https://api.resy.com"
    db_path: str = "data/resy.sqlite"
    location_code: str = "sf"
    center_lat: float = 37.7749
    center_lng: float = -122.4194
    party_size: int = 2
    days: int = 7
    verify_offset_days: int = 21
    per_page: int = 75  # the server caps per_page at 75
    request_delay_s: float = 2.0
    min_days_scored: int = 2
    run_interval_minutes: int = 60  # 0 disables the in-process scheduler
    run_on_start: bool = False
    schedule_party_sizes: list[int] = [2]


settings = Settings()
