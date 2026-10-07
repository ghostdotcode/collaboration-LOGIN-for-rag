import os
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from database import get_db
from models import User

_DEV_SECRET = "fallback-super-secret-key-for-local-dev-only"
JWT_SECRET = os.getenv("JWT_SECRET_KEY", _DEV_SECRET)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# A forgeable-token default is fine on a laptop and a catastrophe in production:
# anyone who has read this repository could mint a valid session. Refuse to boot.
if os.getenv("ENVIRONMENT", "local") == "production" and (
    JWT_SECRET == _DEV_SECRET or len(JWT_SECRET) < 32
):
    raise RuntimeError("JWT_SECRET_KEY must be set to a strong value (>=32 chars) in production")

# auto_error=False so a missing header becomes OUR 401 (with WWW-Authenticate),
# not FastAPI's 403, which clients cannot distinguish from "authenticated but forbidden".
security = HTTPBearer(auto_error=False)

# --- Security & Cryptography Functions ---


def hash_password(password: str) -> str:
    """Hashes the plain text password with a randomized salt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Safely compares the frontend password against the database hash.

    Never raises: a malformed stored hash or an over-long password used to
    propagate as an unhandled 500.
    """
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8")[:72], hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(data: dict) -> str:
    """Generates the JWT token that the frontend will store in LocalStorage."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    to_encode.update({"iat": now, "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)


# --- Route Protector (The Bouncer) ---

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """
    Validates the JWT token from the Authorization header.
    If valid, returns the User object from PostgreSQL.
    If invalid/expired/missing, throws a 401 Unauthorized error.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise credentials_exception

    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user = db.query(User).filter(User.email == email.lower()).first()
    if user is None:
        raise credentials_exception
    return user
