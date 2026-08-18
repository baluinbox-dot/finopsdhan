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

    # None (default) ties the session cookie's Secure flag to `is_production`
    # — right for a normal deploy that terminates real TLS. Explicitly set
    # SESSION_COOKIE_SECURE=false to override that when a production
    # deployment is (temporarily or otherwise) served over plain HTTP behind
    # a reverse proxy with no certificate — a Secure cookie is silently
    # dropped by every browser on a non-HTTPS connection, which breaks login
    # entirely (looks like "successfully registered" then immediately
    # "not logged in" on the very next request) with no error surfaced
    # anywhere. Set back to true (or unset) once real TLS is in front of it.
    session_cookie_secure: bool | None = None

    # --- Outbound email (verification links, password reset) ---
    # Empty smtp_host means "not configured" — app.email.send_email logs a
    # warning and no-ops rather than raising, so a missing/broken mail
    # config degrades to "no email sent" instead of crashing the request
    # that triggered it (registration, forgot-password).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def session_cookie_https_only(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production


@lru_cache
def get_settings() -> Settings:
    return Settings()
