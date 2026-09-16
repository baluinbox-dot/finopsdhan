"""Admin-triggered historical data download (app.routers.admin's
/admin/data-download): superadmin-only gate, the one-at-a-time lock
against DataDownloadRun, stale-row reaping, and the log tail shown on the
page. subprocess.Popen is always mocked here -- these tests must never
actually spawn scripts/backtest/download_historical_data.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app.models import DataDownloadRun, User
from tests.test_auth import _login, _register, _verify


def _login_as_superadmin(client, db_session):
    _register(client, "baluinbox@gmail.com")
    _verify(db_session, "baluinbox@gmail.com")
    resp = _login(client, "baluinbox@gmail.com")
    assert resp.headers["location"] == "/dashboard"
    return db_session.scalar(select(User).where(User.email == "baluinbox@gmail.com"))


def _login_regular_user(client, db_session, email="trader@example.com"):
    _register(client, email)
    _verify(db_session, email)
    user = db_session.scalar(select(User).where(User.email == email))
    user.is_approved = True
    db_session.commit()
    _login(client, email)
    return user


def _mock_popen(pid: int = 12345):
    proc = MagicMock()
    proc.pid = pid
    return patch("app.routers.admin.subprocess.Popen", return_value=proc)


def test_non_superadmin_cannot_reach_data_download(client, db_session):
    _login_regular_user(client, db_session)

    resp = client.get("/admin/data-download", follow_redirects=False)
    assert resp.status_code == 403

    resp = client.post("/admin/data-download/start", follow_redirects=False)
    assert resp.status_code == 403


def test_data_download_requires_login(client):
    resp = client.get("/admin/data-download", follow_redirects=False)
    assert resp.status_code == 401


def test_superadmin_can_start_a_download(client, db_session):
    _login_as_superadmin(client, db_session)

    with _mock_popen(pid=54321):
        resp = client.post("/admin/data-download/start", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/data-download"

    run = db_session.scalar(select(DataDownloadRun))
    assert run is not None
    assert run.status == "running"
    assert run.pid == 54321

    page = client.get("/admin/data-download")
    assert page.status_code == 200
    assert "Running" in page.text


def test_cannot_start_a_second_download_while_one_is_running(client, db_session):
    _login_as_superadmin(client, db_session)

    with _mock_popen(pid=1):
        client.post("/admin/data-download/start", follow_redirects=False)

    with patch("app.routers.admin._is_pid_alive", return_value=True):
        with _mock_popen(pid=2):
            resp = client.post("/admin/data-download/start", follow_redirects=False)

    assert resp.status_code == 303
    runs = db_session.scalars(select(DataDownloadRun)).all()
    assert len(runs) == 1  # second submission was blocked, not a new row


def test_stale_running_row_is_reaped_and_allows_a_new_start(client, db_session):
    """A row stuck at "running" whose pid is no longer alive (the OS
    OOM-killed it, or the VM rebooted, before it could write its own
    "failed" status) must not hold the lock forever."""
    user = _login_as_superadmin(client, db_session)

    stale = DataDownloadRun(
        started_by_user_id=user.id, status="running", pid=999999,
        created_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    db_session.add(stale)
    db_session.commit()

    with patch("app.routers.admin._is_pid_alive", return_value=False):
        page = client.get("/admin/data-download")
        assert page.status_code == 200

        with _mock_popen(pid=777):
            resp = client.post("/admin/data-download/start", follow_redirects=False)
    assert resp.status_code == 303

    db_session.refresh(stale)
    assert stale.status == "failed"
    assert stale.finished_at is not None

    new_run = db_session.scalar(select(DataDownloadRun).where(DataDownloadRun.pid == 777))
    assert new_run is not None
    assert new_run.status == "running"


def test_data_download_page_shows_recent_runs_and_log_tail(client, db_session, tmp_path):
    user = _login_as_superadmin(client, db_session)

    done = DataDownloadRun(
        started_by_user_id=user.id, status="completed",
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(done)
    db_session.commit()
    db_session.refresh(done)

    with patch("app.routers.admin.DOWNLOAD_LOG_DIR", tmp_path):
        log_path = tmp_path / f"{done.id}.log"
        log_path.write_text("2026-09-16 00:00:00 INFO OK NIFTY spot ...\n2026-09-16 00:02:44 INFO ALL DONE\n")

        page = client.get("/admin/data-download")
        assert page.status_code == 200
        assert "Completed" in page.text
        assert "ALL DONE" in page.text
