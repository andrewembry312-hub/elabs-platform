"""
org_router.py — Organization and team management for E-Labs Platform
────────────────────────────────────────────────────────────────────
Mounted at /api/auth/orgs in app.py:
    from org_router import router as _org_router
    app.include_router(_org_router, prefix="/api/auth")

Tables used (created by billing_router._init_billing_tables()):
    organizations, org_members

Endpoints:
    POST  /api/auth/orgs              — create org (caller becomes owner)
    GET   /api/auth/orgs/me           — list caller's orgs + roles
    GET   /api/auth/orgs/{id}         — get org details (members only)
    POST  /api/auth/orgs/{id}/invite  — invite by email (admin/owner)
    POST  /api/auth/orgs/{id}/accept  — accept invite via token
    GET   /api/auth/orgs/{id}/members — list members (admin/owner)
    DELETE /api/auth/orgs/{id}/members/{uid} — remove member (admin/owner)
    DELETE /api/auth/orgs/{id}        — delete org (owner only)
"""

import os
import secrets
import sqlite3
import logging
from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, EmailStr

from auth_router import get_current_user

log = logging.getLogger("orgs")

_DB_PATH = os.path.join(os.path.dirname(__file__), "elabs_users.db")


@contextmanager
def _db():
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_org_role(org_id: int, user_id: int, min_role: str = "member") -> sqlite3.Row:
    """Raise 403 if user is not in org (or below min_role). Returns member row."""
    role_rank = {"member": 0, "admin": 1, "owner": 2}
    with _db() as conn:
        row = conn.execute(
            """SELECT role FROM org_members
               WHERE org_id = ? AND user_id = ? AND accepted_at IS NOT NULL""",
            (org_id, user_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="You are not a member of this organization")
    if role_rank.get(row["role"], -1) < role_rank.get(min_role, 0):
        raise HTTPException(status_code=403, detail=f"Requires role: {min_role}")
    return row


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateOrgRequest(BaseModel):
    name: str


class InviteRequest(BaseModel):
    email: str   # EmailStr validation keeps dep count low


# ── Router ────────────────────────────────────────────────────────────────────
router = APIRouter(tags=["organizations"])


@router.post("/orgs", status_code=201)
async def create_org(body: CreateOrgRequest, user=Depends(get_current_user)):
    """Create a new organization. The caller becomes the owner."""
    user_id = user["id"]
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Organization name cannot be empty")

    with _db() as conn:
        result = conn.execute(
            "INSERT INTO organizations(name, owner_id) VALUES(?, ?)",
            (name, user_id),
        )
        org_id = result.lastrowid
        conn.execute(
            """INSERT INTO org_members(org_id, user_id, role, accepted_at)
               VALUES(?, ?, 'owner', strftime('%s','now'))""",
            (org_id, user_id),
        )

    log.info("Org created: id=%s name=%s owner=%s", org_id, name, user_id)
    return {"id": org_id, "name": name, "role": "owner"}


@router.get("/orgs/me")
async def my_orgs(user=Depends(get_current_user)):
    """Return all organizations the current user belongs to."""
    user_id = user["id"]
    with _db() as conn:
        rows = conn.execute(
            """SELECT o.id, o.name, o.plan, o.created_at, m.role
               FROM organizations o
               JOIN org_members m ON m.org_id = o.id
               WHERE m.user_id = ? AND m.accepted_at IS NOT NULL""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/orgs/{org_id}")
async def get_org(
    org_id: int = Path(..., ge=1),
    user=Depends(get_current_user),
):
    """Get org details. Caller must be a member."""
    _require_org_role(org_id, user["id"], "member")
    with _db() as conn:
        org = conn.execute(
            "SELECT id, name, plan, owner_id, created_at FROM organizations WHERE id = ?",
            (org_id,),
        ).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return dict(org)


@router.get("/orgs/{org_id}/members")
async def list_members(
    org_id: int = Path(..., ge=1),
    user=Depends(get_current_user),
):
    """List all members of an org. Caller must be admin or owner."""
    _require_org_role(org_id, user["id"], "admin")
    with _db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.role, m.invited_email, m.accepted_at,
                      u.username, u.email
               FROM org_members m
               LEFT JOIN users u ON u.id = m.user_id
               WHERE m.org_id = ?""",
            (org_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/orgs/{org_id}/invite", status_code=201)
async def invite_member(
    body: InviteRequest,
    org_id: int = Path(..., ge=1),
    user=Depends(get_current_user),
):
    """
    Invite a user by email. Generates a secure one-time token.
    The invitee calls /accept with this token to join.
    """
    _require_org_role(org_id, user["id"], "admin")
    email = body.email.strip().lower()
    token = secrets.token_urlsafe(32)

    with _db() as conn:
        # Check if already invited / member
        existing = conn.execute(
            "SELECT id FROM org_members WHERE org_id = ? AND invited_email = ?",
            (org_id, email),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="User already invited or is a member")

        conn.execute(
            """INSERT INTO org_members(org_id, invited_email, invite_token, role)
               VALUES(?, ?, ?, 'member')""",
            (org_id, email, token),
        )

    log.info("Invite sent: org=%s email=%s", org_id, email)
    # In production: send token via email. For now return in response (dev mode).
    return {
        "invited_email": email,
        "invite_token": token,
        "accept_url": f"https://copilot.elabsai.com/api/auth/orgs/{org_id}/accept?token={token}",
        "note": "Share the accept_url with the invitee. Token is single-use.",
    }


@router.post("/orgs/{org_id}/accept")
async def accept_invite(
    token: str,
    org_id: int = Path(..., ge=1),
    user=Depends(get_current_user),
):
    """Accept an org invite using the token sent to the invitee."""
    user_id = user["id"]
    with _db() as conn:
        row = conn.execute(
            """SELECT id, invited_email FROM org_members
               WHERE org_id = ? AND invite_token = ? AND accepted_at IS NULL""",
            (org_id, token),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Invite not found or already used")

        conn.execute(
            """UPDATE org_members
               SET user_id = ?, accepted_at = strftime('%s','now'), invite_token = NULL
               WHERE id = ?""",
            (user_id, row["id"]),
        )

    log.info("Invite accepted: org=%s user=%s", org_id, user_id)
    return {"org_id": org_id, "status": "joined"}


@router.delete("/orgs/{org_id}/members/{member_user_id}", status_code=204)
async def remove_member(
    org_id: int = Path(..., ge=1),
    member_user_id: int = Path(..., ge=1),
    user=Depends(get_current_user),
):
    """Remove a member from the org. Admin/owner only. Owner cannot be removed."""
    _require_org_role(org_id, user["id"], "admin")

    with _db() as conn:
        target = conn.execute(
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, member_user_id),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Member not found")
        if target["role"] == "owner":
            raise HTTPException(status_code=400, detail="Cannot remove the org owner")

        conn.execute(
            "DELETE FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, member_user_id),
        )

    return None


@router.delete("/orgs/{org_id}", status_code=204)
async def delete_org(
    org_id: int = Path(..., ge=1),
    user=Depends(get_current_user),
):
    """Delete an organization. Owner only."""
    _require_org_role(org_id, user["id"], "owner")

    with _db() as conn:
        conn.execute("DELETE FROM org_members WHERE org_id = ?", (org_id,))
        conn.execute("DELETE FROM organizations WHERE id = ?", (org_id,))

    log.info("Org deleted: id=%s by user=%s", org_id, user["id"])
    return None
