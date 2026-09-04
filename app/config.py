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

    # Shown on the registration page — new users are asked to open their
    # Dhan trading account through this link (required before they can
    # connect Dhan credentials in Settings and run strategies). Purely
    # informational on our side; we have no way to verify the referral was
    # actually used, so this is a prompt, not an enforced gate.
    dhan_referral_url: str = "https://join.dhan.co/?invite=YZWFE83099"

    strategy_poll_interval_seconds: int = 30
    allow_live_trading: bool = False

    # The dhanhq SDK's own default is 60s per HTTP call (see
    # DhanHTTP.HTTP_DEFAULT_TIME_OUT) — far too long for a background
    # scheduler tick to be stuck on one slow/hanging call. Applied in
    # app.dhan.client.get_user_dhan_client.
    dhan_http_timeout_seconds: int = 20

    # How many UserStrategy evaluations the scheduler's tick runs
    # concurrently (see app.engine.scheduler). Each active strategy makes
    # blocking Dhan HTTP calls; running them one at a time in a single
    # thread means one slow/hung user's call delays every other user's
    # strategy check behind it in the same tick. Kept modest by default so
    # a resource-constrained deploy (small VM) isn't overwhelmed.
    strategy_poll_max_workers: int = 4

    server_static_ip: str = ""

    # Full public URL shown in outbound content meant to be read outside the
    # app itself (currently just the daily strategy-summary email's closing
    # line) -- deliberately separate from base_path/server_static_ip, which
    # are both internal deployment plumbing, not something to guess a
    # user-facing link from. Empty means "don't show a link" rather than
    # falling back to guessing at server_static_ip, since that's an IP
    # address other services share (see the VM deployment notes), not
    # necessarily what should be advertised publicly.
    public_app_url: str = ""

    # "HH:MM" (IST) the daily strategy-summary email fires at -- see
    # app.engine.daily_summary. Default is 50 minutes after every intraday
    # strategy's own 14:45 default square-off, so same-day positions have
    # actually closed (and their exit orders settled) before the email is
    # built.
    daily_summary_time: str = "15:35"

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
