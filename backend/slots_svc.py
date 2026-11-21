"""档期盘：开档、关档、改容抬代、容量查询。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import release, require_lock, try_acquire

router = APIRouter(tags=["slots"])


class SlotIn(BaseModel):
    code: str
    title: str
    start_at: str
    end_at: str
    capacity: int = Field(ge=1, le=500)


class CloseIn(BaseModel):
    code: str


class CapacityIn(BaseModel):
    code: str
    capacity: int = Field(ge=1, le=500)
    bump_generation: bool = True
    path: str | None = None  # 缺省 = 遵册；显式给且与册不符 = 偏离


@router.get("/api/slots")
def list_slots(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM slots ORDER BY start_at, code"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/slots/{code}")
def get_slot(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(404, "档期不存在")
        return dict(row)


@router.post("/api/slots")
def create_slot(body: SlotIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, f"slot:{body.code}", user)
        try:
            exists = conn.execute(
                "SELECT id FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if exists:
                raise HTTPException(409, "档期编码已存在")
            if body.end_at <= body.start_at:
                raise HTTPException(400, "结束时间必须晚于开始")
            conn.execute(
                """INSERT INTO slots(code,title,start_at,end_at,capacity,booked,status,generation)
                   VALUES (?,?,?,?,?,0,'开放',1)""",
                (body.code, body.title, body.start_at, body.end_at, body.capacity),
            )
            write_audit(conn, user, "slot_create", body.code, body.model_dump())
            conn.commit()
        finally:
            release(conn, f"slot:{body.code}", user)
            conn.commit()
    return {"ok": True, "code": body.code}


@router.post("/api/slots/close")
def close_slot(body: CloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, f"slot:{body.code}", user)
        try:
            row = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "档期不存在")
            if row["status"] == "关闭":
                raise HTTPException(409, "档期已关闭")
            open_holds = conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
                (row["id"],),
            ).fetchone()["c"]
            if open_holds:
                raise HTTPException(409, "仍有未确认占位，禁止关档")
            conn.execute(
                "UPDATE slots SET status='关闭' WHERE id=?", (row["id"],)
            )
            write_audit(conn, user, "slot_close", body.code, {"generation": row["generation"]})
            conn.commit()
        finally:
            release(conn, f"slot:{body.code}", user)
            conn.commit()
    return {"ok": True}


@router.post("/api/slots/capacity")
def change_capacity(body: CapacityIn, authorization: AuthHeader = None):
    """改容写口：改容必须连带抬代。

    甲路径（改容抬代册 2026-11-21 择定，当天禁止改口）：
    同一事务改容量并令代数加一，失败则容量与代数同滚。
    禁止只改容量数不抬代；偏离册拒写并记偏；双人改同档须占档锁，后到只记争用。
    """
    user = require_writer(authorization)
    lock_key = f"slot:{body.code}"
    with connect() as conn:
        reg = conn.execute(
            "SELECT * FROM capacity_gen_registry WHERE id=1"
        ).fetchone()
        if reg is None:
            raise HTTPException(500, "改容抬代册未立，拒写")
        if body.path is not None and body.path != reg["path"]:
            write_audit(
                conn,
                user,
                "capacity_gen_deviation",
                body.code,
                {
                    "detail": f"册定{reg['path']}，来单{body.path}，当天禁止改口",
                    "capacity": body.capacity,
                },
            )
            conn.commit()  # 记偏落库
            raise HTTPException(409, "偏离改容抬代册，拒写并记偏")
        if not body.bump_generation:
            write_audit(
                conn,
                user,
                "capacity_gen_deviation",
                body.code,
                {"detail": "只改容量数不抬代", "capacity": body.capacity},
            )
            conn.commit()  # 记偏落库
            raise HTTPException(409, "禁止只改容量数不抬代，拒写并记偏")
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 争用落库：后到只记争用，不写业务
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            row = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "档期不存在")
            if body.capacity < int(row["booked"]):
                raise HTTPException(409, "新容量低于已订，拒写")
            # 甲路径：同一事务改容量并令代数加一
            conn.execute(
                "UPDATE slots SET capacity=?, generation=generation+1 WHERE id=?",
                (body.capacity, row["id"]),
            )
            write_audit(
                conn,
                user,
                "capacity_change",
                body.code,
                {
                    "old_capacity": row["capacity"],
                    "new_capacity": body.capacity,
                    "old_gen": row["generation"],
                    "new_gen": int(row["generation"]) + 1,
                    "path": reg["path"],
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()  # 失败则容量与代数同滚
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
        return {
            "ok": True,
            "code": body.code,
            "capacity": body.capacity,
            "generation": int(row["generation"]) + 1,
            "path": reg["path"],
        }


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        return {
            "slots_open": conn.execute(
                "SELECT COUNT(*) c FROM slots WHERE status='开放'"
            ).fetchone()["c"],
            "holds_open": conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE status='占位中'"
            ).fetchone()["c"],
            "bookings": conn.execute(
                "SELECT COUNT(*) c FROM bookings WHERE status='已确认'"
            ).fetchone()["c"],
            "waitlist": conn.execute(
                "SELECT COUNT(*) c FROM waitlist WHERE status='候补中'"
            ).fetchone()["c"],
            "now": datetime.now().isoformat(timespec="seconds"),
        }
