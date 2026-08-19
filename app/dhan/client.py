"""Per-user DhanHQ client construction.

Unlike the single-user `get_client()` pattern in the dhanhq-skills reference
repo (env vars / config.json), this app is multi-tenant: each user's
Client ID + Access Token live encrypted in the `dhan_credentials` table and
a fresh SDK client is built per request/evaluation from that row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dhanhq import DhanContext, DhanLogin, dhanhq
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dhan.helpers import format_dhan_error
from app.models import DhanCredential, User
from app.security import decrypt_secret


class DhanNotConnectedError(RuntimeError):
    """Raised when a user has no active Dhan connection."""


@dataclass
class UserDhanClient:
    client: "dhanhq"
    context: DhanContext
    client_id: str


def get_user_dhan_client(db: Session, user: User) -> UserDhanClient:
    credential = user.dhan_credential
    if credential is None or not credential.is_active:
        raise DhanNotConnectedError(
            "No active Dhan connection for this user. Connect an account on the Settings page first."
        )

    access_token = decrypt_secret(credential.access_token_encrypted)
    context = DhanContext(credential.client_id, access_token)
    client = dhanhq(context)
    # SDK default is 60s per HTTP call — one slow/hung request would stall
    # a whole scheduler tick. Shorten it for every call made through this
    # client (quotes, orders, option chain, ...).
    client.dhan_http.timeout = get_settings().dhan_http_timeout_seconds
    return UserDhanClient(client=client, context=context, client_id=credential.client_id)


def validate_dhan_credentials(client_id: str, access_token: str) -> dict[str, Any]:
    """Call Dhan's profile endpoint to confirm the given credentials work.

    Returns the profile dict on success. Raises on failure — callers should
    show the error to the user rather than silently saving bad credentials.
    """
    login = DhanLogin(client_id)
    response = login.user_profile(access_token)

    # Some SDK versions return the profile dict directly; others wrap it in
    # the standard {"status", "remarks", "data"} envelope. Handle both.
    if isinstance(response, dict) and "status" in response and "data" in response:
        if response["status"] != "success":
            raise ValueError(format_dhan_error(response.get("remarks")) or "Dhan rejected these credentials.")
        return response["data"]
    if isinstance(response, dict):
        return response
    raise ValueError("Unexpected response from Dhan while validating credentials.")
