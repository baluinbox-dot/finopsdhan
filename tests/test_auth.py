from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import User, UserRole


def test_register_race_condition_shows_friendly_error_not_500(client, db_session, monkeypatch):
    """The existing-email pre-check isn't atomic with the insert — simulate
    a near-simultaneous double submit slipping past it (both requests see
    no existing row before either commits) by making the commit itself
    raise the database's own unique-constraint violation. Must show the
    same friendly flash as the normal duplicate-email case, not crash."""
    original_commit = db_session.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT INTO users ...", {}, Exception("duplicate key value violates unique constraint"))
        return original_commit()

    monkeypatch.setattr(db_session, "commit", flaky_commit)

    resp = client.post(
        "/auth/register",
        data={"email": "raced@example.com", "password": "supersecret1", "confirm_password": "supersecret1"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/register"
    # The failed insert must not leave a half-committed row behind.
    assert db_session.scalar(select(User).where(User.email == "raced@example.com")) is None


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
