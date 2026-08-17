"""FastAPI dependencies: DB session, current user, role guards."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, UserRole

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(request: Request, db: DbSession) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    try:
        user = db.get(User, uuid.UUID(user_id))
    except (ValueError, TypeError):
        return None
    if user is None or not user.is_active:
        return None
    return user


CurrentUserOptional = Annotated[User | None, Depends(get_current_user)]


def require_user(current_user: CurrentUserOptional) -> User:
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")
    return current_user


CurrentUser = Annotated[User, Depends(require_user)]


def require_superadmin(current_user: CurrentUser) -> User:
    if current_user.role != UserRole.SUPERADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superadmin access required")
    return current_user


SuperadminUser = Annotated[User, Depends(require_superadmin)]
