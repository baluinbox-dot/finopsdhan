"""Registration, login, logout.

Session-based auth (signed cookie via Starlette's SessionMiddleware, added
in app/main.py) rather than JWT — simpler to reason about and revoke,
matching the session-based pattern already used across Balu's FinOps products.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.deps import CurrentUserOptional, DbSession
from app.models import User, UserRole
from app.security import hash_password, verify_password
from app.templating import flash, render

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/register")
def register_form(request: Request, current_user: CurrentUserOptional):
    if current_user:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "auth/register.html")


@router.post("/register")
def register_submit(
    request: Request,
    db: DbSession,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    email = email.strip().lower()

    if password != confirm_password:
        flash(request, "Passwords do not match.", "error")
        return RedirectResponse("/auth/register", status_code=303)

    if len(password) < 8:
        flash(request, "Password must be at least 8 characters.", "error")
        return RedirectResponse("/auth/register", status_code=303)

    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        flash(request, "An account with that email already exists.", "error")
        return RedirectResponse("/auth/register", status_code=303)

    settings = get_settings()
    role = UserRole.SUPERADMIN if email == settings.superadmin_email.strip().lower() else UserRole.USER

    user = User(email=email, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()

    flash(request, "Account created. Please log in.", "success")
    return RedirectResponse("/auth/login", status_code=303)


@router.get("/login")
def login_form(request: Request, current_user: CurrentUserOptional):
    if current_user:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "auth/login.html")


@router.post("/login")
def login_submit(
    request: Request,
    db: DbSession,
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))

    if not user or not verify_password(password, user.password_hash):
        flash(request, "Invalid email or password.", "error")
        return RedirectResponse("/auth/login", status_code=303)

    if not user.is_active:
        flash(request, "This account has been disabled.", "error")
        return RedirectResponse("/auth/login", status_code=303)

    request.session.clear()
    request.session["user_id"] = str(user.id)
    flash(request, f"Welcome back, {user.email}.", "success")
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)
