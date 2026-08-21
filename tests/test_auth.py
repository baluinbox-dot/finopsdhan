from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import User, UserRole

# conftest.client patches random.randint to always return 4, so every
# rendered CAPTCHA is "What is 4 + 4?" — this is the one correct answer.
CAPTCHA_ANSWER = "8"


def _register(client, email: str, password: str = "supersecret1", captcha: str = CAPTCHA_ANSWER):
    # The CAPTCHA challenge is only generated (and stashed in the session)
    # by the GET — posting straight to the endpoint without it first means
    # there's nothing to check the answer against, so the submit always
    # fails the CAPTCHA regardless of what's sent.
    client.get("/auth/register")
    return client.post(
        "/auth/register",
        data={"email": email, "password": password, "confirm_password": password, "captcha_answer": captcha},
        follow_redirects=False,
    )


def _login(client, email: str, password: str = "supersecret1", captcha: str = CAPTCHA_ANSWER):
    client.get("/auth/login")
    return client.post(
        "/auth/login",
        data={"email": email, "password": password, "captcha_answer": captcha},
        follow_redirects=False,
    )


def _verify(db_session, email: str) -> None:
    """Simulate clicking the emailed verification link, without needing
    SMTP configured — the token is on the user row regardless of whether
    the email actually sent (app.email.send_email no-ops without SMTP
    config in tests, but the token is generated/stored before that call)."""
    user = db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.email_verification_token is not None
    user.email_verified = True
    user.email_verification_token = None
    db_session.commit()


def _approve(db_session, email: str) -> None:
    """Simulate the superadmin clicking Approve in /admin/users — a verified
    regular-user account still can't log in until this happens."""
    user = db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    user.is_approved = True
    db_session.commit()


def test_register_creates_unverified_account_login_blocked_until_verified(client, db_session):
    resp = _register(client, "someone@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    user = db_session.scalar(select(User).where(User.email == "someone@example.com"))
    assert user is not None
    assert user.email_verified is False
    assert user.email_verification_token is not None

    # Correct credentials, but not verified yet — login must be blocked.
    resp = _login(client, "someone@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/resend-verification?email=someone@example.com"

    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 401  # never actually got a session


def test_verify_email_then_login_succeeds(client, db_session):
    _register(client, "verifyme@example.com")
    user = db_session.scalar(select(User).where(User.email == "verifyme@example.com"))
    token = user.email_verification_token

    resp = client.get(f"/auth/verify-email?token={token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    db_session.refresh(user)
    assert user.email_verified is True
    assert user.email_verification_token is None
    assert user.is_approved is False  # verified alone isn't enough — still needs admin approval

    # Verified but not yet approved — login still blocked.
    resp = _login(client, "verifyme@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 401

    _approve(db_session, "verifyme@example.com")

    resp = _login(client, "verifyme@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"

    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 200


def test_verify_email_bad_token_does_not_verify_anything(client, db_session):
    _register(client, "someone@example.com")
    resp = client.get("/auth/verify-email?token=not-a-real-token", follow_redirects=False)
    assert resp.status_code == 303
    user = db_session.scalar(select(User).where(User.email == "someone@example.com"))
    assert user.email_verified is False


def test_verify_email_expired_token_redirects_to_resend(client, db_session):
    _register(client, "someone@example.com")
    user = db_session.scalar(select(User).where(User.email == "someone@example.com"))
    user.email_verification_sent_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db_session.commit()
    token = user.email_verification_token

    resp = client.get(f"/auth/verify-email?token={token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/resend-verification?email=someone@example.com"

    db_session.refresh(user)
    assert user.email_verified is False


def test_resend_verification_issues_a_new_token(client, db_session):
    _register(client, "someone@example.com")
    user = db_session.scalar(select(User).where(User.email == "someone@example.com"))
    old_token = user.email_verification_token

    resp = client.post("/auth/resend-verification", data={"email": "someone@example.com"}, follow_redirects=False)
    assert resp.status_code == 303

    db_session.refresh(user)
    assert user.email_verification_token is not None
    assert user.email_verification_token != old_token


def test_resend_verification_does_not_leak_account_existence(client):
    # Same redirect/behavior whether or not the email is registered.
    resp_unknown = client.post("/auth/resend-verification", data={"email": "nobody@example.com"}, follow_redirects=False)
    assert resp_unknown.status_code == 303
    assert resp_unknown.headers["location"] == "/auth/login"


def test_register_wrong_captcha_blocked(client, db_session):
    resp = _register(client, "someone@example.com", captcha="999")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/register"
    assert db_session.scalar(select(User).where(User.email == "someone@example.com")) is None


def test_login_wrong_captcha_blocked_even_with_correct_password(client, db_session):
    _register(client, "someone@example.com")
    _verify(db_session, "someone@example.com")

    resp = _login(client, "someone@example.com", captcha="999")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 401


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

    resp = _register(client, "raced@example.com")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/register"
    # The failed insert must not leave a half-committed row behind.
    assert db_session.scalar(select(User).where(User.email == "raced@example.com")) is None


def test_register_superadmin_and_regular_user(client, db_session):
    resp = _register(client, "baluinbox@gmail.com")
    assert resp.status_code == 303

    resp = _register(client, "someone@example.com")
    assert resp.status_code == 303

    superadmin = db_session.scalar(select(User).where(User.email == "baluinbox@gmail.com"))
    regular = db_session.scalar(select(User).where(User.email == "someone@example.com"))

    assert superadmin.role == UserRole.SUPERADMIN
    assert regular.role == UserRole.USER

    # The superadmin's own account is auto-approved (nobody else to approve
    # it); every other new account starts unapproved and needs the
    # superadmin to approve it in /admin/users before it can log in.
    assert superadmin.is_approved is True
    assert regular.is_approved is False


def test_register_password_mismatch_does_not_create_user(client, db_session):
    client.get("/auth/register")
    resp = client.post(
        "/auth/register",
        data={"email": "mismatch@example.com", "password": "supersecret1", "confirm_password": "different1", "captcha_answer": CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db_session.scalar(select(User).where(User.email == "mismatch@example.com")) is None


def test_login_success_and_wrong_password(client, db_session):
    _register(client, "loginuser@example.com", password="correcthorse1")
    _verify(db_session, "loginuser@example.com")
    _approve(db_session, "loginuser@example.com")

    bad = _login(client, "loginuser@example.com", password="wrongpassword")
    assert bad.status_code == 303
    assert bad.headers["location"] == "/auth/login"

    good = _login(client, "loginuser@example.com", password="correcthorse1")
    assert good.status_code == 303
    assert good.headers["location"] == "/dashboard"

    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 200


def test_forgot_password_then_reset_then_login(client, db_session):
    _register(client, "resetme@example.com", password="oldpassword1")
    _verify(db_session, "resetme@example.com")
    _approve(db_session, "resetme@example.com")

    resp = client.post("/auth/forgot-password", data={"email": "resetme@example.com"}, follow_redirects=False)
    assert resp.status_code == 303

    user = db_session.scalar(select(User).where(User.email == "resetme@example.com"))
    token = user.password_reset_token
    assert token is not None

    resp = client.post(
        "/auth/reset-password",
        data={"token": token, "password": "newpassword1", "confirm_password": "newpassword1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    db_session.refresh(user)
    assert user.password_reset_token is None

    # Old password no longer works, new one does.
    assert _login(client, "resetme@example.com", password="oldpassword1").headers["location"] == "/auth/login"
    assert _login(client, "resetme@example.com", password="newpassword1").headers["location"] == "/dashboard"


def test_forgot_password_does_not_leak_account_existence(client, db_session):
    resp = client.post("/auth/forgot-password", data={"email": "nobody@example.com"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_reset_password_expired_token_rejected(client, db_session):
    _register(client, "resetme@example.com")
    _verify(db_session, "resetme@example.com")
    _approve(db_session, "resetme@example.com")
    client.post("/auth/forgot-password", data={"email": "resetme@example.com"}, follow_redirects=False)

    user = db_session.scalar(select(User).where(User.email == "resetme@example.com"))
    user.password_reset_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    token = user.password_reset_token

    resp = client.post(
        "/auth/reset-password",
        data={"token": token, "password": "newpassword1", "confirm_password": "newpassword1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/forgot-password"

    # Original password must still work — the expired token must not have reset it.
    assert _login(client, "resetme@example.com").headers["location"] == "/dashboard"


def test_reset_password_bad_token_rejected(client, db_session):
    resp = client.post(
        "/auth/reset-password",
        data={"token": "not-a-real-token", "password": "newpassword1", "confirm_password": "newpassword1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/forgot-password"


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 401


def test_superadmin_login_bypasses_approval_gate(client, db_session):
    """The superadmin account is auto-approved at registration — email
    verification alone is enough to log in, no separate approval step."""
    _register(client, "baluinbox@gmail.com")
    _verify(db_session, "baluinbox@gmail.com")

    resp = _login(client, "baluinbox@gmail.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_verified_but_unapproved_login_shows_pending_message(client, db_session):
    _register(client, "pending@example.com")
    _verify(db_session, "pending@example.com")

    resp = _login(client, "pending@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    user = db_session.scalar(select(User).where(User.email == "pending@example.com"))
    assert user.is_approved is False
