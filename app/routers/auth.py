"""Registration, login, logout, email verification, and password reset.

Session-based auth (signed cookie via Starlette's SessionMiddleware, added
in app/main.py) rather than JWT — simpler to reason about and revoke,
matching the session-based pattern already used across Balu's FinOps products.

New accounts must verify their email (click a tokenized link) before they
can log in. Both login and register are gated by a simple arithmetic
CAPTCHA (no external service/JS dependency — a random "a + b" stored
server-side in the session and popped on submit) to cut down on bot
sign-ups/credential-stuffing attempts.
"""

from __future__ import annotations

import random
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.deps import CurrentUserOptional, DbSession
from app.email import send_email
from app.models import User, UserRole
from app.security import hash_password, verify_password
from app.templating import flash, render, url

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL_VERIFICATION_EXPIRY_HOURS = 24
PASSWORD_RESET_EXPIRY_HOURS = 1


def _new_captcha(request: Request) -> dict:
    """Generate a fresh single-digit addition challenge and stash the
    expected answer in the session (never sent to the client) — popped and
    checked in the matching POST handler. Regenerated on every GET, so a
    failed submit always gets a new question, not a reusable static one."""
    a, b = random.randint(1, 9), random.randint(1, 9)
    request.session["captcha_a"] = a
    request.session["captcha_b"] = b
    return {"captcha_a": a, "captcha_b": b}


def _check_captcha(request: Request, submitted: str) -> bool:
    expected_a = request.session.pop("captcha_a", None)
    expected_b = request.session.pop("captcha_b", None)
    if expected_a is None or expected_b is None:
        return False
    try:
        return int(str(submitted).strip()) == expected_a + expected_b
    except (TypeError, ValueError):
        return False


def _send_verification_email(request: Request, db: DbSession, user: User) -> None:
    token = secrets.token_urlsafe(32)
    user.email_verification_token = token
    user.email_verification_sent_at = datetime.now(timezone.utc)
    db.commit()

    # request.base_url reflects the Host header of the actual incoming
    # request — correct whether this is local dev (http://127.0.0.1:8000/)
    # or behind the VM's Apache proxy (http://136.110.55.82/), since Apache
    # is configured with ProxyPreserveHost On. No separate "public URL"
    # setting needed.
    link = f"{str(request.base_url).rstrip('/')}{url('/auth/verify-email')}?token={token}"
    send_email(
        user.email,
        "Verify your FinOps Dhan Algo account",
        html_body=(
            f"<p>Click to verify your account:</p><p><a href='{link}'>{link}</a></p>"
            f"<p>This link expires in {EMAIL_VERIFICATION_EXPIRY_HOURS} hours.</p>"
        ),
        text_body=(
            f"Verify your account: {link}\n"
            f"This link expires in {EMAIL_VERIFICATION_EXPIRY_HOURS} hours."
        ),
    )


def _send_pending_approval_notice(request: Request, user: User) -> None:
    """Tell the superadmin a newly-verified account is waiting for
    approval. Best-effort — same no-SMTP-configured no-op as every other
    email in this module; the account still shows up in /admin/users
    either way, so a failed/unsent notice never blocks approval."""
    settings = get_settings()
    review_link = f"{str(request.base_url).rstrip('/')}{url('/admin/users')}"
    send_email(
        settings.superadmin_email,
        "New FinOps Dhan Algo account awaiting approval",
        html_body=(
            f"<p>{user.email} has verified their email and is waiting for approval.</p>"
            f"<p><a href='{review_link}'>Review pending accounts</a></p>"
        ),
        text_body=f"{user.email} has verified their email and is waiting for approval.\nReview: {review_link}",
    )


def _send_approved_email(user: User) -> None:
    login_link_note = "You can now log in."
    send_email(
        user.email,
        "Your FinOps Dhan Algo account has been approved",
        html_body=f"<p>Your account has been approved by the admin. {login_link_note}</p>",
        text_body=f"Your account has been approved by the admin. {login_link_note}",
    )


def _send_password_reset_email(request: Request, db: DbSession, user: User) -> None:
    token = secrets.token_urlsafe(32)
    user.password_reset_token = token
    user.password_reset_expires_at = datetime.now(timezone.utc) + timedelta(hours=PASSWORD_RESET_EXPIRY_HOURS)
    db.commit()

    link = f"{str(request.base_url).rstrip('/')}{url('/auth/reset-password')}?token={token}"
    send_email(
        user.email,
        "Reset your FinOps Dhan Algo password",
        html_body=(
            f"<p>Click to reset your password:</p><p><a href='{link}'>{link}</a></p>"
            f"<p>This link expires in {PASSWORD_RESET_EXPIRY_HOURS} hour(s). "
            f"If you didn't request this, you can ignore this email.</p>"
        ),
        text_body=(
            f"Reset your password: {link}\n"
            f"This link expires in {PASSWORD_RESET_EXPIRY_HOURS} hour(s). "
            f"If you didn't request this, you can ignore this email."
        ),
    )


@router.get("/register")
def register_form(request: Request, current_user: CurrentUserOptional):
    if current_user:
        return RedirectResponse(url("/dashboard"), status_code=303)
    context = _new_captcha(request)
    context["dhan_referral_url"] = get_settings().dhan_referral_url
    return render(request, "auth/register.html", context)


@router.post("/register")
def register_submit(
    request: Request,
    db: DbSession,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    captcha_answer: str = Form(...),
):
    email = email.strip().lower()

    if not _check_captcha(request, captcha_answer):
        flash(request, "Incorrect answer to the verification question — please try again.", "error")
        return RedirectResponse(url("/auth/register"), status_code=303)

    if password != confirm_password:
        flash(request, "Passwords do not match.", "error")
        return RedirectResponse(url("/auth/register"), status_code=303)

    if len(password) < 8:
        flash(request, "Password must be at least 8 characters.", "error")
        return RedirectResponse(url("/auth/register"), status_code=303)

    if len(password.encode("utf-8")) > 72:
        flash(request, "Password must be 72 characters or fewer.", "error")
        return RedirectResponse(url("/auth/register"), status_code=303)

    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        flash(request, "An account with that email already exists.", "error")
        return RedirectResponse(url("/auth/register"), status_code=303)

    settings = get_settings()
    role = UserRole.SUPERADMIN if email == settings.superadmin_email.strip().lower() else UserRole.USER

    # Every other new account needs a superadmin to approve it (see
    # login_submit below and app.routers.admin) before it can log in — the
    # superadmin's own account is exempt, since there'd be nobody else to
    # approve it.
    is_superadmin = role == UserRole.SUPERADMIN
    user = User(
        email=email,
        password_hash=hash_password(password),
        role=role,
        email_verified=False,
        is_approved=is_superadmin,
        approved_at=datetime.now(timezone.utc) if is_superadmin else None,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # The pre-check above isn't atomic with this insert — a
        # near-simultaneous double submit (e.g. a double-click, or a retry
        # after a slow/failed first attempt) can pass the "does it exist?"
        # check twice before either commits, and the second one hits the
        # database's own unique constraint instead. Without this, that
        # crashes with an unhandled 500 rather than the same friendly
        # message the pre-check already gives for the non-race case.
        db.rollback()
        flash(request, "An account with that email already exists.", "error")
        return RedirectResponse(url("/auth/register"), status_code=303)

    _send_verification_email(request, db, user)

    flash(request, "Account created. Check your email for a verification link before logging in.", "success")
    return RedirectResponse(url("/auth/login"), status_code=303)


@router.get("/verify-email")
def verify_email(request: Request, db: DbSession, token: str = ""):
    if not token:
        flash(request, "Invalid verification link.", "error")
        return RedirectResponse(url("/auth/login"), status_code=303)

    user = db.scalar(select(User).where(User.email_verification_token == token))
    if user is None:
        flash(request, "This verification link is invalid or has already been used.", "error")
        return RedirectResponse(url("/auth/login"), status_code=303)

    sent_at = user.email_verification_sent_at
    if sent_at is not None:
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - sent_at > timedelta(hours=EMAIL_VERIFICATION_EXPIRY_HOURS):
            flash(request, "This verification link has expired. Request a new one below.", "error")
            return RedirectResponse(f"{url('/auth/resend-verification')}?email={user.email}", status_code=303)

    user.email_verified = True
    user.email_verification_token = None
    user.email_verification_sent_at = None
    db.commit()

    if user.is_approved:
        flash(request, "Email verified — you can now log in.", "success")
    else:
        _send_pending_approval_notice(request, user)
        flash(
            request,
            "Email verified. Your account now awaits approval by the admin — "
            "you'll be able to log in once it's approved.",
            "success",
        )
    return RedirectResponse(url("/auth/login"), status_code=303)


@router.get("/resend-verification")
def resend_verification_form(request: Request, email: str = ""):
    return render(request, "auth/resend_verification.html", {"email": email})


@router.post("/resend-verification")
def resend_verification_submit(request: Request, db: DbSession, email: str = Form(...)):
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    # Same message regardless of whether the account exists or is already
    # verified — never confirm/deny account existence to an anonymous caller.
    if user is not None and not user.email_verified:
        _send_verification_email(request, db, user)
    flash(request, "If that email has a pending account, a new verification link has been sent.", "info")
    return RedirectResponse(url("/auth/login"), status_code=303)


@router.get("/login")
def login_form(request: Request, current_user: CurrentUserOptional):
    if current_user:
        return RedirectResponse(url("/dashboard"), status_code=303)
    return render(request, "auth/login.html", _new_captcha(request))


@router.post("/login")
def login_submit(
    request: Request,
    db: DbSession,
    email: str = Form(...),
    password: str = Form(...),
    captcha_answer: str = Form(...),
):
    email = email.strip().lower()

    if not _check_captcha(request, captcha_answer):
        flash(request, "Incorrect answer to the verification question — please try again.", "error")
        return RedirectResponse(url("/auth/login"), status_code=303)

    user = db.scalar(select(User).where(User.email == email))

    if not user or not verify_password(password, user.password_hash):
        flash(request, "Invalid email or password.", "error")
        return RedirectResponse(url("/auth/login"), status_code=303)

    if not user.is_active:
        flash(request, "This account has been disabled.", "error")
        return RedirectResponse(url("/auth/login"), status_code=303)

    if not user.email_verified:
        flash(request, "Please verify your email before logging in — check your inbox, or resend the link below.", "error")
        return RedirectResponse(f"{url('/auth/resend-verification')}?email={user.email}", status_code=303)

    if not user.is_approved:
        flash(request, "Your account is awaiting approval by the admin. You'll be able to log in once it's approved.", "error")
        return RedirectResponse(url("/auth/login"), status_code=303)

    request.session.clear()
    request.session["user_id"] = str(user.id)
    flash(request, f"Welcome back, {user.email}.", "success")
    return RedirectResponse(url("/dashboard"), status_code=303)


@router.get("/forgot-password")
def forgot_password_form(request: Request):
    return render(request, "auth/forgot_password.html")


@router.post("/forgot-password")
def forgot_password_submit(request: Request, db: DbSession, email: str = Form(...)):
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        _send_password_reset_email(request, db, user)
    # Same message either way — don't leak whether an email is registered.
    flash(request, "If that email has an account, a password reset link has been sent.", "info")
    return RedirectResponse(url("/auth/login"), status_code=303)


@router.get("/reset-password")
def reset_password_form(request: Request, token: str = ""):
    return render(request, "auth/reset_password.html", {"token": token})


@router.post("/reset-password")
def reset_password_submit(
    request: Request,
    db: DbSession,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = db.scalar(select(User).where(User.password_reset_token == token))
    if user is None or user.password_reset_expires_at is None:
        flash(request, "This password reset link is invalid or has already been used.", "error")
        return RedirectResponse(url("/auth/forgot-password"), status_code=303)

    expires_at = user.password_reset_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        flash(request, "This password reset link has expired. Request a new one below.", "error")
        return RedirectResponse(url("/auth/forgot-password"), status_code=303)

    back_to_form = f"{url('/auth/reset-password')}?token={token}"

    if password != confirm_password:
        flash(request, "Passwords do not match.", "error")
        return RedirectResponse(back_to_form, status_code=303)

    if len(password) < 8:
        flash(request, "Password must be at least 8 characters.", "error")
        return RedirectResponse(back_to_form, status_code=303)

    if len(password.encode("utf-8")) > 72:
        flash(request, "Password must be 72 characters or fewer.", "error")
        return RedirectResponse(back_to_form, status_code=303)

    user.password_hash = hash_password(password)
    user.password_reset_token = None
    user.password_reset_expires_at = None
    db.commit()

    flash(request, "Password reset. Please log in with your new password.", "success")
    return RedirectResponse(url("/auth/login"), status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url("/auth/login"), status_code=303)
