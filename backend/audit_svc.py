"""审计与互证只读面。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    lim = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (lim,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/capacity-gen")
def capacity_gen_book(authorization: AuthHeader = None):
    """改容抬代册只读面：册定路径、改容抬代记录、偏离记录。"""
    require_user(authorization)
    with connect() as conn:
        reg = conn.execute(
            "SELECT * FROM capacity_gen_registry WHERE id=1"
        ).fetchone()
        rows = conn.execute(
            """SELECT * FROM audit_trail
               WHERE action IN ('capacity_change','capacity_gen_deviation')
               ORDER BY id DESC LIMIT 200"""
        ).fetchall()
    return {
        "registry": dict(reg) if reg else None,
        "changes": [dict(r) for r in rows if r["action"] == "capacity_change"],
        "deviations": [dict(r) for r in rows if r["action"] == "capacity_gen_deviation"],
    }


@router.get("/api/proof/{party_key}")
def proof(party_key: str, authorization: AuthHeader = None):
    """按当事人互证：占位/正式/候补是否同源可读。"""
    require_user(authorization)
    with connect() as conn:
        holds = conn.execute(
            "SELECT * FROM holds WHERE party_key=? ORDER BY id DESC", (party_key,)
        ).fetchall()
        books = conn.execute(
            "SELECT * FROM bookings WHERE party_key=? ORDER BY id DESC", (party_key,)
        ).fetchall()
        waits = conn.execute(
            "SELECT * FROM waitlist WHERE party_key=? ORDER BY id DESC", (party_key,)
        ).fetchall()
        return {
            "party_key": party_key,
            "holds": [dict(r) for r in holds],
            "bookings": [dict(r) for r in books],
            "waitlist": [dict(r) for r in waits],
        }
