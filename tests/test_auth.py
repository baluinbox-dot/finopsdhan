from __future__ import annotations

from sqlalchemy import select

from app.models import User, UserRole


def test_register_superadmin_and_regular_user(client, db_session):
    resp = client.post(
        "/auth/register",
        data={"email": "baluinbox@gmail.com", "password": "supersecret1", "confirm_password": "supersecret1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.post(
        "/auth/register",
        data={"email": "someone@example.com", "password": "supersecret1", "confirm_password": "supersecret1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    superadmin = db_session.scalar(select(User).where(User.email == "baluinbox@gmail.com"))
    regular = db_session.scalar(select(User).where(User.email == "someone@example.com"))

    assert superadmin.role == UserRole.SUPERADMIN
    assert regular.role == UserRole.USER


def test_register_password_mismatch_does_not_create_user(client, db_session):
    resp = client.post(
        "/auth/register",
        data={"email": "mismatch@example.com", "password": "supersecret1", "confirm_password": "different1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db_session.scalar(select(User).where(User.email == "mismatch@example.com")) is None


def test_login_success_and_wrong_password(client, db_session):
    client.post(
        "/auth/register",
        data={"email": "loginuser@example.com", "password": "correcthorse1", "confirm_password": "correcthorse1"},
        follow_redirects=False,
    )

    bad = client.post(
        "/auth/login",
        data={"email": "loginuser@example.com", "password": "wrongpassword"},
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert bad.headers["location"] == "/auth/login"

    good = client.post(
        "/auth/login",
        data={"email": "loginuser@example.com", "password": "correcthorse1"},
        follow_redirects=False,
    )
    assert good.status_code == 303
    assert good.headers["location"] == "/dashboard"

    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 200


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 401
