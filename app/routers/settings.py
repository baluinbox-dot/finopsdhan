"""Settings page: connect / test / disconnect a user's Dhan account.

Dhan requires the calling server's IP to be static-whitelisted on the
user's own account before order placement, modification, cancellation,
super orders, or forever orders will work. We can't automate that step —
it happens on web.dhan.co — so this page surfaces our server's IP
prominently and tells the user to add it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.dhan.client import validate_dhan_credentials
from app.deps import CurrentUser, DbSession
from app.models import DhanCredential
from app.security import encrypt_secret
from app.templating import flash, render, url

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/dhan")
def dhan_settings(request: Request, current_user: CurrentUser):
    settings = get_settings()
    return render(
        request,
        "settings/dhan.html",
        {
            "current_user": current_user,
            "credential": current_user.dhan_credential,
            "server_static_ip": settings.server_static_ip or "(not yet provisioned — running locally)",
        },
    )


@router.post("/dhan/connect")
def dhan_connect(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    client_id: str = Form(...),
    access_token: str = Form(...),
):
    client_id = client_id.strip()
    access_token = access_token.strip()

    try:
        profile = validate_dhan_credentials(client_id, access_token)
    except Exception as exc:  # noqa: BLE001 — surface any SDK/network error to the user
        flash(request, f"Could not verify these Dhan credentials: {exc}", "error")
        return RedirectResponse(url("/settings/dhan"), status_code=303)

    credential = current_user.dhan_credential
    if credential is None:
        credential = DhanCredential(user_id=current_user.id, client_id=client_id, access_token_encrypted="")
        db.add(credential)

    credential.client_id = client_id
    credential.access_token_encrypted = encrypt_secret(access_token)
    credential.token_saved_at = datetime.now(timezone.utc)
    credential.last_validated_at = datetime.now(timezone.utc)
    credential.last_profile_snapshot = {
        "dataPlan": profile.get("dataPlan"),
        "dataValidity": profile.get("dataValidity"),
        "tokenValidity": profile.get("tokenValidity"),
        "activeSegment": profile.get("activeSegment"),
        "ddpi": profile.get("ddpi"),
        "mtf": profile.get("mtf"),
    }
    credential.is_active = True
    db.commit()

    flash(request, "Dhan account connected successfully.", "success")
    return RedirectResponse(url("/settings/dhan"), status_code=303)


@router.post("/dhan/test")
def dhan_test(request: Request, db: DbSession, current_user: CurrentUser):
    credential = current_user.dhan_credential
    if credential is None:
        flash(request, "No Dhan connection to test yet.", "error")
        return RedirectResponse(url("/settings/dhan"), status_code=303)

    from app.security import decrypt_secret

    try:
        access_token = decrypt_secret(credential.access_token_encrypted)
        profile = validate_dhan_credentials(credential.client_id, access_token)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Connection test failed: {exc}", "error")
        return RedirectResponse(url("/settings/dhan"), status_code=303)

    credential.last_validated_at = datetime.now(timezone.utc)
    credential.last_profile_snapshot = {
        "dataPlan": profile.get("dataPlan"),
        "dataValidity": profile.get("dataValidity"),
        "tokenValidity": profile.get("tokenValidity"),
        "activeSegment": profile.get("activeSegment"),
        "ddpi": profile.get("ddpi"),
        "mtf": profile.get("mtf"),
    }
    db.commit()

    flash(request, "Connection is valid.", "success")
    return RedirectResponse(url("/settings/dhan"), status_code=303)


@router.post("/dhan/disconnect")
def dhan_disconnect(request: Request, db: DbSession, current_user: CurrentUser):
    credential = current_user.dhan_credential
    if credential is not None:
        db.delete(credential)
        db.commit()
    flash(request, "Dhan account disconnected.", "success")
    return RedirectResponse(url("/settings/dhan"), status_code=303)
