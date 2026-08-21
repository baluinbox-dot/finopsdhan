"""Superadmin: review and approve new accounts.

A registered, email-verified account still can't log in until the
superadmin approves it here (see app.routers.auth.login_submit). "Disable"
reuses the existing `is_active` flag (already checked at login), rather than
adding a separate rejection state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.deps import DbSession, SuperadminUser
from app.models import User
from app.routers.auth import _send_approved_email
from app.templating import flash, render, url

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users")
def list_users(request: Request, db: DbSession, current_user: SuperadminUser):
    all_users = db.scalars(select(User).order_by(User.is_approved.asc(), User.created_at.desc())).all()
    return render(
        request,
        "admin/users.html",
        {
            "current_user": current_user,
            "users": all_users,
            "pending_count": sum(1 for u in all_users if not u.is_approved and u.email_verified),
        },
    )


@router.post("/users/{user_id}/approve")
def approve_user(request: Request, db: DbSession, current_user: SuperadminUser, user_id: uuid.UUID):
    user = db.get(User, user_id)
    if user is None:
        flash(request, "No such user.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    if not user.email_verified:
        flash(request, f"{user.email} hasn't verified their email yet — can't approve.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    user.is_approved = True
    user.approved_at = datetime.now(timezone.utc)
    db.commit()
    _send_approved_email(user)

    flash(request, f"{user.email} approved — they can now log in.", "success")
    return RedirectResponse(url("/admin/users"), status_code=303)


@router.post("/users/{user_id}/disable")
def disable_user(request: Request, db: DbSession, current_user: SuperadminUser, user_id: uuid.UUID):
    user = db.get(User, user_id)
    if user is None:
        flash(request, "No such user.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    if user.id == current_user.id:
        flash(request, "You can't disable your own account.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    user.is_active = False
    db.commit()

    flash(request, f"{user.email} disabled — they can no longer log in.", "success")
    return RedirectResponse(url("/admin/users"), status_code=303)


@router.post("/users/{user_id}/enable")
def enable_user(request: Request, db: DbSession, current_user: SuperadminUser, user_id: uuid.UUID):
    user = db.get(User, user_id)
    if user is None:
        flash(request, "No such user.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    user.is_active = True
    db.commit()

    flash(request, f"{user.email} re-enabled.", "success")
    return RedirectResponse(url("/admin/users"), status_code=303)
