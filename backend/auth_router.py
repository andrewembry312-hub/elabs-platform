"""
E-Labs Auth Router
------------------
FastAPI APIRouter providing:
  POST  /api/auth/login     — issue JWT access token + HttpOnly refresh cookie
  POST  /api/auth/register  — admin-only user creation
  POST  /api/auth/refresh   — exchange refresh cookie for new access token
  POST  /api/auth/logout    — clear refresh cookie
  GET   /api/auth/me        — return current user info (requires valid access token)

Storage: SQLite (same pattern as rest of app.py).
Passwords: bcrypt via passlib.
Tokens: python-jose (HS256 JWT).

Dependencies (add to requirements if not present):
  passlib[bcrypt]
  python-jose[cryptography]
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Response, Cookie, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

try:
    from passlib.context import CryptContext
except ImportError:
    raise RuntimeError("passlib[bcrypt] is required: pip install passlib[bcrypt]")

try:
    from jose import JWTError, jwt
except ImportError:
    raise RuntimeError("python-jose is required: pip install python-jose[cryptography]")

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# Config (override via environment variables)
# ──────────────────────────────────────────
_SECRET_KEY = os.environ.get("ELABS_JWT_SECRET", "CHANGE_ME_before_production_use_32chars+")
_ALGORITHM  = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES  = int(os.environ.get("ELABS_ACCESS_TOKEN_MINUTES", "15"))
_REFRESH_TOKEN_EXPIRE_DAYS    = int(os.environ.get("ELABS_REFRESH_TOKEN_DAYS", "7"))
_REFRESH_COOKIE_NAME          = "elabs_refresh"
_ADMIN_BOOTSTRAP_USERNAME     = os.environ.get("ELABS_ADMIN_USER", "admin")
_ADMIN_BOOTSTRAP_PASSWORD     = os.environ.get("ELABS_ADMIN_PASS", "")  # empty = disabled

# DB path: same directory as app.py
_DB_PATH = Path(__file__).parent / "elabs_users.db"

# ──────────────────────────────────────────
# Password context
# ──────────────────────────────────────────
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ──────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────
def _get_db():
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    """Create the users table if it doesn't exist and optionally bootstrap an admin account."""
    conn = _get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                username       TEXT NOT NULL UNIQUE,
                email          TEXT,
                hashed_password TEXT NOT NULL,
                role           TEXT NOT NULL DEFAULT 'client',
                created_at     TEXT NOT NULL,
                last_login     TEXT
            )
        """)
        conn.commit()

        # Bootstrap admin if env vars provided and no admin exists yet
        if _ADMIN_BOOTSTRAP_PASSWORD:
            row = conn.execute(
                "SELECT id FROM users WHERE role = 'admin' LIMIT 1"
            ).fetchone()
            if not row:
                _create_user_db(
                    conn,
                    username=_ADMIN_BOOTSTRAP_USERNAME,
                    email="",
                    password=_ADMIN_BOOTSTRAP_PASSWORD,
                    role="admin",
                )
                logger.info("Auth: bootstrapped admin account '%s'", _ADMIN_BOOTSTRAP_USERNAME)
    finally:
        conn.close()


def _create_user_db(conn, *, username: str, email: str, password: str, role: str):
    hashed = _pwd_ctx.hash(password)
    conn.execute(
        "INSERT INTO users (username, email, hashed_password, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (username, email, hashed, role, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    conn = _get_db()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()


def _get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    conn = _get_db()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()


def _update_last_login(user_id: int):
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────
# Token helpers
# ──────────────────────────────────────────
def _create_access_token(user_id: int, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=_ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "role": role, "exp": expire, "type": "access"}
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def _create_refresh_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=_REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {"sub": str(user_id), "exp": expire, "type": "refresh"}
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def _decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        return jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ──────────────────────────────────────────
# Current-user dependency
# ──────────────────────────────────────────
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
):
    """Extract and validate the access token from the Authorization header."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = _decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    user = _get_user_by_id(int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_admin(current_user=Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


# ──────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    email: str = ""
    password: str
    role: str = "client"  # admin | client | demo


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    created_at: str
    last_login: Optional[str] = None


# ──────────────────────────────────────────
# Router
# ──────────────────────────────────────────
router = APIRouter(tags=["auth"])

# Ensure DB is ready when this module is imported
_init_db()


@router.post("/login")
def login(body: LoginRequest, response: Response):
    """Authenticate and return JWT access token. Sets an HttpOnly refresh cookie."""
    user = _get_user_by_username(body.username)
    if not user or not _pwd_ctx.verify(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    _update_last_login(user["id"])
    access_token  = _create_access_token(user["id"], user["role"])
    refresh_token = _create_refresh_token(user["id"])

    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=False,        # set True in production behind TLS
        samesite="lax",
        max_age=_REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/auth",
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": _ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "role": user["role"],
    }


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, _admin=Depends(require_admin)):
    """Create a new user account (admin-only)."""
    if body.role not in ("admin", "client", "demo"):
        raise HTTPException(status_code=400, detail="role must be admin, client, or demo")
    if _get_user_by_username(body.username):
        raise HTTPException(status_code=409, detail="Username already exists")
    conn = _get_db()
    try:
        _create_user_db(conn, username=body.username, email=body.email, password=body.password, role=body.role)
    finally:
        conn.close()
    return {"message": f"User '{body.username}' created with role '{body.role}'"}


@router.post("/refresh")
def refresh_token(response: Response, elabs_refresh: Optional[str] = Cookie(default=None)):
    """Exchange a valid refresh cookie for a new access token."""
    if not elabs_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")
    payload = _decode_token(elabs_refresh)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    user = _get_user_by_id(int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    access_token  = _create_access_token(user["id"], user["role"])
    refresh_token_ = _create_refresh_token(user["id"])

    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=refresh_token_,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=_REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/auth",
    )

    return {"access_token": access_token, "token_type": "bearer", "expires_in": _ACCESS_TOKEN_EXPIRE_MINUTES * 60}


@router.post("/logout")
def logout(response: Response):
    """Clear the refresh token cookie."""
    response.delete_cookie(key=_REFRESH_COOKIE_NAME, path="/api/auth")
    return {"message": "Logged out"}


@router.get("/me", response_model=UserOut)
def me(current_user=Depends(get_current_user)):
    """Return current authenticated user's profile."""
    return UserOut(
        id=current_user["id"],
        username=current_user["username"],
        email=current_user["email"] or "",
        role=current_user["role"],
        created_at=current_user["created_at"],
        last_login=current_user["last_login"],
    )


# ──────────────────────────────────────────
# Configurable auth guard
# ──────────────────────────────────────────
#
# ELABS_REQUIRE_AUTH=0 (default) — no-op. Local WebUI works without logging in.
# ELABS_REQUIRE_AUTH=1           — enforces JWT on all guarded endpoints.
# Set to 1 in the server environment before exposing the backend to the internet.
#
_REQUIRE_AUTH = os.environ.get("ELABS_REQUIRE_AUTH", "0") == "1"


def auth_guard(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
):
    """Drop-in FastAPI Depends() guard for protected endpoints.

    ELABS_REQUIRE_AUTH=0 (default): pass-through, returns None.
    ELABS_REQUIRE_AUTH=1           : validates Bearer JWT, raises 401 on failure.
    """
    if not _REQUIRE_AUTH:
        return None
    return get_current_user(credentials)
