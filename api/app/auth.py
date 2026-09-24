import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import AdminApiKey, utcnow
from .security import hash_admin_token

settings = get_settings()


@dataclass
class AdminPrincipal:
    name: str
    bootstrap: bool = False


def require_admin(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AdminPrincipal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    token = authorization[7:].strip()
    if hmac.compare_digest(token, settings.admin_token):
        return AdminPrincipal(name="bootstrap", bootstrap=True)

    token_hash = hash_admin_token(token)
    api_key = db.scalar(select(AdminApiKey).where(AdminApiKey.token_hash == token_hash, AdminApiKey.active.is_(True)))
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")

    api_key.last_used_at = utcnow()
    db.commit()
    return AdminPrincipal(name=api_key.name, bootstrap=False)
