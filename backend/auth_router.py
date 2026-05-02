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

try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore  — GitHub OAuth routes will 503 if httpx missing

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# Config (override via environment variables)
# ──────────────────────────────────────────
_SECRET_KEY = os.environ.get("ELABS_JWT_SECRET", "CHANGE_ME_before_production_use_32chars+")
_ALGORITHM  = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES  = int(os.environ.get("ELABS_ACCESS_TOKEN_MINUTES", "15"))
_REFRESH_TOKEN_EXPIRE_DAYS    = int(os.environ.get("ELABS_REFRESH_TOKEN_DAYS", "7"))
_REFRESH_COOKIE_NAME          = "elabs_refresh"
_ACCESS_COOKIE_NAME           = "elabs_token"   # non-HttpOnly; readable by JS + GET billing endpoint
_ADMIN_BOOTSTRAP_USERNAME     = os.environ.get("ELABS_ADMIN_USER", "admin")
_ADMIN_BOOTSTRAP_PASSWORD     = os.environ.get("ELABS_ADMIN_PASS", "")  # empty = disabled

# GitHub OAuth config
_GITHUB_CLIENT_ID     = os.environ.get("GITHUB_CLIENT_ID", "")
_GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
# Google OAuth config
_GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_ALLOWED_REDIRECT_HOST = ".elabsai.com"  # only redirect to *.elabsai.com after OAuth

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


def _set_access_cookie(response: Response, access_token: str) -> None:
    """Set the non-HttpOnly access-token cookie shared across *.elabsai.com."""
    response.set_cookie(
        key=_ACCESS_COOKIE_NAME,
        value=access_token,
        httponly=False,   # JS-readable; needed for billing redirect + cross-subdomain auth check
        secure=False,     # set True in production behind TLS
        samesite="lax",
        domain=".elabsai.com",
        max_age=_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def _validate_redirect(url: str) -> str:
    """Ensure the OAuth return URL is within *.elabsai.com (prevent open redirect)."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not (host == "elabsai.com" or host.endswith(_ALLOWED_REDIRECT_HOST)):
        return "https://www.elabsai.com"
    return url


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
    # Non-HttpOnly access-token cookie so JS and GET billing endpoints can read it
    _set_access_cookie(response, access_token)

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


# ──────────────────────────────────────────
# GitHub OAuth — E-Labs Account SSO
# ──────────────────────────────────────────
# Register a GitHub OAuth App at https://github.com/settings/developers
# Callback URL: https://copilot.elabsai.com/api/auth/github/callback
# Env vars required: GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET
# ──────────────────────────────────────────

import base64 as _base64
import hashlib as _hashlib
import hmac as _hmac
import json as _json


def _github_state_encode(redirect_url: str) -> str:
    """Encode redirect URL + HMAC into a base64 state param."""
    safe_url = _validate_redirect(redirect_url)
    payload = _json.dumps({"r": safe_url}).encode()
    mac = _hmac.new(_SECRET_KEY.encode(), payload, _hashlib.sha256).hexdigest()
    return _base64.urlsafe_b64encode(payload + b"|" + mac.encode()).decode()


def _github_state_decode(state: str) -> str:
    """Decode and verify state; return redirect URL or homepage on failure."""
    try:
        raw = _base64.urlsafe_b64decode(state.encode())
        payload_b, mac_b = raw.rsplit(b"|", 1)
        expected = _hmac.new(_SECRET_KEY.encode(), payload_b, _hashlib.sha256).hexdigest().encode()
        if not _hmac.compare_digest(mac_b, expected):
            return "https://www.elabsai.com"
        data = _json.loads(payload_b)
        return _validate_redirect(data.get("r", "https://www.elabsai.com"))
    except Exception:
        return "https://www.elabsai.com"


def _upsert_github_user(github_id: int, login: str, email: str) -> sqlite3.Row:
    """Find or create a user for this GitHub identity."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE github_id = ?", (github_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            conn.commit()
            return conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        # New user — create account (role=client)
        conn.execute(
            """INSERT INTO users (username, email, hashed_password, role, created_at, github_id)
               VALUES (?, ?, '', 'client', ?, ?)""",
            (login, email or "", datetime.now(timezone.utc).isoformat(), github_id),
        )
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
    finally:
        conn.close()


def _ensure_github_column():
    """Add github_id column to users table if not present (migration)."""
    conn = _get_db()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "github_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN github_id INTEGER")
            conn.commit()
    finally:
        conn.close()


_ensure_github_column()


from fastapi.responses import RedirectResponse as _RedirectResponse


@router.get("/github")
def github_login(redirect: str = "https://www.elabsai.com"):
    """Redirect the browser to GitHub to begin OAuth. Pass ?redirect= to return after login."""
    if not _GITHUB_CLIENT_ID:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured (GITHUB_CLIENT_ID missing)")
    state = _github_state_encode(redirect)
    scope = "read:user,user:email"
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={_GITHUB_CLIENT_ID}"
        f"&scope={scope}"
        f"&state={state}"
    )
    return _RedirectResponse(url)


@router.get("/github/callback")
async def github_callback(code: str = "", state: str = "", response: Response = None):
    """GitHub calls this after the user authorizes. Issues JWT and redirects back."""
    if not _GITHUB_CLIENT_ID or not _GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
    if not _httpx:
        raise HTTPException(status_code=503, detail="httpx is required: pip install httpx")
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")

    redirect_to = _github_state_decode(state) if state else "https://www.elabsai.com"

    # Exchange code for access token
    async with _httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": _GITHUB_CLIENT_ID, "client_secret": _GITHUB_CLIENT_SECRET, "code": code},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="GitHub token exchange failed")
        gh_token = token_resp.json().get("access_token", "")
        if not gh_token:
            raise HTTPException(status_code=502, detail="GitHub did not return an access token")

        # Fetch GitHub user profile
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {gh_token}", "Accept": "application/json"},
            timeout=10,
        )
        if user_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch GitHub user profile")
        gh_user = user_resp.json()
        github_id = gh_user["id"]
        login = gh_user.get("login", f"gh_{github_id}")

        # Fetch primary verified email
        email = gh_user.get("email") or ""
        if not email:
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"token {gh_token}", "Accept": "application/json"},
                timeout=10,
            )
            if emails_resp.status_code == 200:
                for e in emails_resp.json():
                    if e.get("primary") and e.get("verified"):
                        email = e["email"]
                        break

    user = _upsert_github_user(github_id, login, email)
    access_token  = _create_access_token(user["id"], user["role"])
    refresh_token = _create_refresh_token(user["id"])

    redirect_resp = _RedirectResponse(redirect_to, status_code=302)
    redirect_resp.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=_REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/auth",
    )
    _set_access_cookie(redirect_resp, access_token)
    return redirect_resp


# ──────────────────────────────────────────
# Google OAuth — E-Labs Account SSO
# ──────────────────────────────────────────
# Register a Google OAuth App at https://console.cloud.google.com/apis/credentials
# Callback URL: https://copilot.elabsai.com/api/auth/google/callback
# Env vars required: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
# ──────────────────────────────────────────

def _ensure_google_column():
    """Add google_id column to users table if not present (migration)."""
    conn = _get_db()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "google_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN google_id TEXT")
            conn.commit()
    finally:
        conn.close()


_ensure_google_column()


def _upsert_google_user(google_id: str, name: str, email: str) -> sqlite3.Row:
    """Find or create a user for this Google identity."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE google_id = ?", (google_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            conn.commit()
            return conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        # New user — create account with role=client
        username = (email.split("@")[0] if email else name or f"google_{google_id}").replace(" ", "_")
        conn.execute(
            """INSERT INTO users (username, email, hashed_password, role, created_at, google_id)
               VALUES (?, ?, '', 'client', ?, ?)""",
            (username, email or "", datetime.now(timezone.utc).isoformat(), google_id),
        )
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
    finally:
        conn.close()


@router.get("/google")
def google_login(redirect: str = "https://www.elabsai.com"):
    """Redirect the browser to Google to begin OAuth. Pass ?redirect= to return after login."""
    if not _GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google OAuth not configured (GOOGLE_CLIENT_ID missing)")
    state = _github_state_encode(redirect)  # reuse same HMAC-signed state helper
    scope = "openid email profile"
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={_GOOGLE_CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri=https://copilot.elabsai.com/api/auth/google/callback"
        f"&scope={scope.replace(' ', '%20')}"
        f"&state={state}"
        "&access_type=online"
        "&prompt=select_account"
    )
    return _RedirectResponse(url)


@router.get("/google/callback")
async def google_callback(code: str = "", state: str = "", response: Response = None):
    """Google calls this after authorization. Issues JWT and redirects back."""
    if not _GOOGLE_CLIENT_ID or not _GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Google OAuth not configured")
    if not _httpx:
        raise HTTPException(status_code=503, detail="httpx is required: pip install httpx")
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")

    redirect_to = _github_state_decode(state) if state else "https://www.elabsai.com"

    async with _httpx.AsyncClient() as client:
        # Exchange code for tokens
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": _GOOGLE_CLIENT_ID,
                "client_secret": _GOOGLE_CLIENT_SECRET,
                "redirect_uri": "https://copilot.elabsai.com/api/auth/google/callback",
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Google token exchange failed")
        token_data = token_resp.json()
        g_access = token_data.get("access_token", "")
        if not g_access:
            raise HTTPException(status_code=502, detail="Google did not return an access token")

        # Fetch user info
        user_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {g_access}"},
            timeout=10,
        )
        if user_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch Google user profile")
        g_user = user_resp.json()

    google_id = str(g_user.get("sub", ""))
    email     = g_user.get("email", "")
    name      = g_user.get("name", "") or g_user.get("given_name", "")

    if not google_id:
        raise HTTPException(status_code=502, detail="Google profile missing sub claim")

    user = _upsert_google_user(google_id, name, email)
    access_token  = _create_access_token(user["id"], user["role"])
    refresh_token = _create_refresh_token(user["id"])

    redirect_resp = _RedirectResponse(redirect_to, status_code=302)
    redirect_resp.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=_REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/auth",
    )
    _set_access_cookie(redirect_resp, access_token)
    return redirect_resp

