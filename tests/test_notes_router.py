"""Tests for the personal trading-notes journal (app/routers/notes.py):
one free-text note per user per calendar day, upserted by date, with a
recent-notes list and per-note delete."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.models import TradeNote, User

CAPTCHA_ANSWER = "8"  # conftest.client patches random.randint to always return 4


def _register_and_login(client, db_session, email: str, password: str = "supersecret1"):
    client.get("/auth/logout")
    client.get("/auth/register")
    client.post(
        "/auth/register",
        data={"email": email, "password": password, "confirm_password": password, "captcha_answer": CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    user = db_session.scalar(select(User).where(User.email == email))
    user.email_verified = True
    user.is_approved = True
    db_session.commit()

    client.get("/auth/login")
    client.post(
        "/auth/login",
        data={"email": email, "password": password, "captcha_answer": CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    return user


def test_list_notes_renders_empty_for_a_new_user(client, db_session):
    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/notes")
    assert resp.status_code == 200
    assert "No other notes yet." in resp.text


def test_save_note_creates_a_row_for_the_given_date(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    resp = client.post(
        "/notes", data={"note_date": "2026-09-10", "content": "Nifty range-bound, VIX 13.2."}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/notes?note_date=2026-09-10"

    note = db_session.scalar(
        select(TradeNote).where(TradeNote.user_id == user.id, TradeNote.note_date == date(2026, 9, 10))
    )
    assert note is not None
    assert note.content == "Nifty range-bound, VIX 13.2."


def test_save_note_upserts_the_same_day_instead_of_duplicating(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    client.post("/notes", data={"note_date": "2026-09-10", "content": "First draft."})
    client.post("/notes", data={"note_date": "2026-09-10", "content": "Updated draft."})

    notes = db_session.scalars(
        select(TradeNote).where(TradeNote.user_id == user.id, TradeNote.note_date == date(2026, 9, 10))
    ).all()
    assert len(notes) == 1
    assert notes[0].content == "Updated draft."


def test_saving_blank_content_deletes_an_existing_note(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    client.post("/notes", data={"note_date": "2026-09-10", "content": "Something."})
    client.post("/notes", data={"note_date": "2026-09-10", "content": "   "})

    assert db_session.scalar(
        select(TradeNote).where(TradeNote.user_id == user.id, TradeNote.note_date == date(2026, 9, 10))
    ) is None


def test_saving_blank_content_for_a_new_date_creates_nothing(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    client.post("/notes", data={"note_date": "2026-09-10", "content": ""})
    assert db_session.scalars(select(TradeNote).where(TradeNote.user_id == user.id)).first() is None


def test_list_notes_shows_the_selected_dates_content_and_others_in_recent(client, db_session):
    _register_and_login(client, db_session, "trader@example.com")
    client.post("/notes", data={"note_date": "2026-09-08", "content": "Older note about VIX spike."})
    client.post("/notes", data={"note_date": "2026-09-10", "content": "Latest note about NIFTY."})

    resp = client.get("/notes?note_date=2026-09-10")
    assert resp.status_code == 200
    assert "Latest note about NIFTY." in resp.text
    assert "Older note about VIX spike." in resp.text  # shown in the recent-notes list


def test_notes_are_scoped_per_user(client, db_session):
    user_a = _register_and_login(client, db_session, "a@example.com")
    client.post("/notes", data={"note_date": "2026-09-10", "content": "User A's private note."})

    _register_and_login(client, db_session, "b@example.com")
    resp = client.get("/notes?note_date=2026-09-10")
    assert resp.status_code == 200
    assert "User A's private note." not in resp.text

    # sanity: the note really was saved, just not visible to user B
    note = db_session.scalar(select(TradeNote).where(TradeNote.user_id == user_a.id))
    assert note is not None


def test_delete_note_removes_it(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    client.post("/notes", data={"note_date": "2026-09-08", "content": "To be deleted."})
    note = db_session.scalar(select(TradeNote).where(TradeNote.user_id == user.id))

    resp = client.post(f"/notes/{note.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert db_session.get(TradeNote, note.id) is None


def test_delete_note_rejects_another_users_note(client, db_session):
    owner = _register_and_login(client, db_session, "owner@example.com")
    client.post("/notes", data={"note_date": "2026-09-08", "content": "Owner's note."})
    note = db_session.scalar(select(TradeNote).where(TradeNote.user_id == owner.id))

    _register_and_login(client, db_session, "intruder@example.com")
    resp = client.post(f"/notes/{note.id}/delete", follow_redirects=False)
    assert resp.status_code == 303

    assert db_session.get(TradeNote, note.id) is not None  # untouched
