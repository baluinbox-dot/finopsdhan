from __future__ import annotations

from sqlalchemy import select

from app.models import User
from tests.test_auth import _login, _register, _verify


def _login_as_superadmin(client, db_session):
    """The superadmin account is auto-approved at registration, so it only
    needs email verification before it can log in and reach /admin/users."""
    _register(client, "baluinbox@gmail.com")
    _verify(db_session, "baluinbox@gmail.com")
    resp = _login(client, "baluinbox@gmail.com")
    assert resp.headers["location"] == "/dashboard"


def test_non_superadmin_cannot_reach_admin_users(client, db_session):
    _register(client, "regular@example.com")
    _verify(db_session, "regular@example.com")
    user = db_session.scalar(select(User).where(User.email == "regular@example.com"))
    user.is_approved = True
    db_session.commit()
    _login(client, "regular@example.com")

    resp = client.get("/admin/users", follow_redirects=False)
    assert resp.status_code == 403


def test_admin_users_requires_login(client):
    resp = client.get("/admin/users", follow_redirects=False)
    assert resp.status_code == 401


def test_superadmin_can_approve_a_pending_user(client, db_session):
    # Register + verify the applicant first — /auth/register redirects
    # away (to /dashboard) for an already-logged-in session, so this must
    # happen before the superadmin logs in on this same client.
    _register(client, "newbie@example.com")
    _verify(db_session, "newbie@example.com")
    newbie = db_session.scalar(select(User).where(User.email == "newbie@example.com"))
    assert newbie.is_approved is False

    _login_as_superadmin(client, db_session)

    listing = client.get("/admin/users")
    assert listing.status_code == 200
    assert "newbie@example.com" in listing.text

    resp = client.post(f"/admin/users/{newbie.id}/approve", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users"

    db_session.refresh(newbie)
    assert newbie.is_approved is True
    assert newbie.approved_at is not None


def test_approving_unverified_user_is_rejected(client, db_session):
    _register(client, "unverified@example.com")
    unverified = db_session.scalar(select(User).where(User.email == "unverified@example.com"))
    assert unverified.email_verified is False

    _login_as_superadmin(client, db_session)

    resp = client.post(f"/admin/users/{unverified.id}/approve", follow_redirects=False)
    assert resp.status_code == 303

    db_session.refresh(unverified)
    assert unverified.is_approved is False


def test_approved_user_can_then_log_in(client, db_session):
    _register(client, "newbie2@example.com")
    _verify(db_session, "newbie2@example.com")
    newbie = db_session.scalar(select(User).where(User.email == "newbie2@example.com"))

    _login_as_superadmin(client, db_session)
    client.post(f"/admin/users/{newbie.id}/approve", follow_redirects=False)

    # Log out the superadmin, then the newly-approved user logs in.
    client.get("/auth/logout")
    resp = _login(client, "newbie2@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_superadmin_can_disable_and_reenable_a_user(client, db_session):
    _register(client, "tobedisabled@example.com")
    _verify(db_session, "tobedisabled@example.com")
    user = db_session.scalar(select(User).where(User.email == "tobedisabled@example.com"))

    _login_as_superadmin(client, db_session)
    client.post(f"/admin/users/{user.id}/approve", follow_redirects=False)

    resp = client.post(f"/admin/users/{user.id}/disable", follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(user)
    assert user.is_active is False

    client.get("/auth/logout")
    resp = _login(client, "tobedisabled@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 401

    _login(client, "baluinbox@gmail.com")
    resp = client.post(f"/admin/users/{user.id}/enable", follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(user)
    assert user.is_active is True


def test_superadmin_cannot_disable_own_account(client, db_session):
    _login_as_superadmin(client, db_session)
    superadmin = db_session.scalar(select(User).where(User.email == "baluinbox@gmail.com"))

    resp = client.post(f"/admin/users/{superadmin.id}/disable", follow_redirects=False)
    assert resp.status_code == 303

    db_session.refresh(superadmin)
    assert superadmin.is_active is True
