"""Personal trading journal: one free-text note per user per calendar day
-- market view, India VIX, how today's trades went, anything worth
remembering next time a similar setup shows up. Purely a personal
reference; never read by the strategy engine, never affects any trade."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.deps import CurrentUser, DbSession
from app.models import TradeNote
from app.templating import flash, render, url

router = APIRouter(prefix="/notes", tags=["notes"])


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.today()


@router.get("")
def list_notes(request: Request, db: DbSession, current_user: CurrentUser, note_date: str = ""):
    selected_date = _parse_date(note_date) if note_date else date.today()

    current = db.scalar(
        select(TradeNote).where(TradeNote.user_id == current_user.id, TradeNote.note_date == selected_date)
    )
    recent_notes = db.scalars(
        select(TradeNote).where(TradeNote.user_id == current_user.id, TradeNote.note_date != selected_date)
        .order_by(TradeNote.note_date.desc()).limit(30)
    ).all()

    return render(
        request,
        "notes/list.html",
        {
            "current_user": current_user,
            "selected_date": selected_date,
            "today": date.today(),
            "content": current.content if current else "",
            "recent_notes": recent_notes,
        },
    )


@router.post("")
def save_note(
    request: Request, db: DbSession, current_user: CurrentUser, note_date: str = Form(...), content: str = Form(""),
):
    parsed_date = _parse_date(note_date)
    content = content.strip()

    existing = db.scalar(
        select(TradeNote).where(TradeNote.user_id == current_user.id, TradeNote.note_date == parsed_date)
    )
    if existing:
        if content:
            existing.content = content
        else:
            # Saving with the text cleared removes that day's note entirely
            # -- an empty row would just be clutter in the recent list.
            db.delete(existing)
    elif content:
        db.add(TradeNote(user_id=current_user.id, note_date=parsed_date, content=content))
    db.commit()

    flash(request, f"Note for {parsed_date.isoformat()} saved.", "success")
    return RedirectResponse(url(f"/notes?note_date={parsed_date.isoformat()}"), status_code=303)


@router.post("/{note_id}/delete")
def delete_note(request: Request, db: DbSession, current_user: CurrentUser, note_id: uuid.UUID):
    note = db.get(TradeNote, note_id)
    if note is None or note.user_id != current_user.id:
        flash(request, "Note not found.", "error")
        return RedirectResponse(url("/notes"), status_code=303)
    note_date = note.note_date  # capture before delete -- the row (and this attribute access) won't exist after commit
    db.delete(note)
    db.commit()
    flash(request, f"Note for {note_date.isoformat()} deleted.", "success")
    return RedirectResponse(url("/notes"), status_code=303)
