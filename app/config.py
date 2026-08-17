"""Centralized app settings, loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://finops:finops@localhost:5432/finopstrading"

    session_secret_key: str = "dev-only-insecure-key-change-me"
    credentials_encryption_key: str = ""

    superadmin_email: str = "baluinbox@gmail.com"
    env: str = "development"

    strategy_poll_interval_seconds: int = 30
    allow_live_trading: bool = False

    server_static_ip: str = ""

    # Empty locally (app served at the domain root, e.g. http://127.0.0.1:8000/).
    # Set to e.g. "/finopsdhan" when hosted behind a reverse proxy under a
    # path prefix, alongside other products on the same domain/IP. Must not
    # have a trailing slash.
    base_path: str = ""

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
