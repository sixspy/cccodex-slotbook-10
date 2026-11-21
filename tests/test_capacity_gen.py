"""改容抬代：夹具复现假绿 → 改容必抬代 / 双人争用（先红后绿）。

口径（改容抬代册，2026-11-21 择定，当天禁止改口）：甲路径——
同一事务改容量并令代数加一，失败则容量与代数同滚。
禁止只改容量数不抬代；偏离册拒写并记偏。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import init_db  # noqa: E402
import db as dbmod  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    # 夹具跑本机文件库（tmp 文件库，非内存库）
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user="admin", password="123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def buggy_capacity_write():
    """夹具：复现旧改容写口——直改容量数、漏抬代数（本机文件库）。"""

    def _apply(code: str = "S-AM-01", new_capacity: int = 12) -> dict:
        conn = sqlite3.connect(dbmod.DB_PATH)  # 本机文件库
        conn.row_factory = sqlite3.Row
        before = conn.execute(
            "SELECT capacity, generation FROM slots WHERE code=?", (code,)
        ).fetchone()
        # 旧写口症结：只改容量数，不抬代
        conn.execute("UPDATE slots SET capacity=? WHERE code=?", (new_capacity, code))
        conn.commit()
        after = conn.execute(
            "SELECT capacity, generation FROM slots WHERE code=?", (code,)
        ).fetchone()
        conn.close()
        return {"before": dict(before), "after": dict(after)}

    return _apply


def test_bug_repro_false_green(buggy_capacity_write):
    """假绿复现：容量改了，代数列/占位挂代/对账代数灯全停在旧代。"""
    with TestClient(app) as c:
        h = _login(c)
        rep = buggy_capacity_write("S-AM-01", 12)
        assert rep["before"] == {"capacity": 10, "generation": 1}
        assert rep["after"]["capacity"] == 12  # 容量值已改
        assert rep["after"]["generation"] == 1  # 代数列停在原地 ← 症结
        # 新占位还挂旧代
        r = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-AM-01", "party_key": "P-BUG", "seats": 1},
        )
        assert r.status_code == 200 and r.json()["disposition"] == "占位"
        holds = c.get("/api/holds", headers=h).json()["items"]
        hold = [x for x in holds if x["party_key"] == "P-BUG"][0]
        assert hold["gen"] == 1  # 占位所挂代 = 旧代
        # 对账代数灯不跳
        recon = c.get("/api/reconcile/S-AM-01", headers=h).json()
        assert recon["generation"] == 1


def test_capacity_change_bumps_generation():
    """改容必抬代：容量值/代数列/占位挂代/对账/审计五处同态。"""
    with TestClient(app) as c:
        h = _login(c)
        r = c.post(
            "/api/slots/capacity",
            headers=h,
            json={"code": "S-AM-01", "capacity": 12},
        )
        assert r.status_code == 200, r.text
        assert r.json()["generation"] == 2
        slot = c.get("/api/slots/S-AM-01", headers=h).json()
        assert slot["capacity"] == 12  # 容量值
        assert slot["generation"] == 2  # 代数列
        # 占位所挂代：改容后新占位挂新代
        r2 = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-AM-01", "party_key": "P-G2", "seats": 1},
        )
        assert r2.status_code == 200, r2.text
        holds = c.get("/api/holds", headers=h).json()["items"]
        hold = [x for x in holds if x["party_key"] == "P-G2"][0]
        assert hold["gen"] == 2
        # 对账：代数灯跳、容量同态
        recon = c.get("/api/reconcile/S-AM-01", headers=h).json()
        assert recon["generation"] == 2
        assert recon["capacity"] == 12
        # 审计：改容抬代入册，新旧容量与新旧代同录
        book = c.get("/api/audit/capacity-gen", headers=h).json()
        assert book["registry"]["path"] == "甲同事务"
        changes = [x for x in book["changes"] if x["target"] == "S-AM-01"]
        assert changes, "改容必须入审计"
        payload = json.loads(changes[0]["payload"])
        assert payload["old_capacity"] == 10 and payload["new_capacity"] == 12
        assert payload["old_gen"] == 1 and payload["new_gen"] == 2


def test_capacity_change_without_bump_rejected():
    """禁止只改容量数不抬代：拒写并记偏，容量与代数零改。"""
    with TestClient(app) as c:
        h = _login(c)
        r = c.post(
            "/api/slots/capacity",
            headers=h,
            json={"code": "S-AM-01", "capacity": 12, "bump_generation": False},
        )
        assert r.status_code == 409, r.text
        slot = c.get("/api/slots/S-AM-01", headers=h).json()
        assert slot["capacity"] == 10 and slot["generation"] == 1
        book = c.get("/api/audit/capacity-gen", headers=h).json()
        assert any(x["target"] == "S-AM-01" for x in book["deviations"]), "偏离必须记偏"


def test_capacity_change_wrong_path_rejected():
    """偏离改容抬代册（当天禁止改口）：拒写并记偏。"""
    with TestClient(app) as c:
        h = _login(c)
        r = c.post(
            "/api/slots/capacity",
            headers=h,
            json={"code": "S-AM-01", "capacity": 12, "path": "乙影子"},
        )
        assert r.status_code == 409, r.text
        slot = c.get("/api/slots/S-AM-01", headers=h).json()
        assert slot["capacity"] == 10 and slot["generation"] == 1
        book = c.get("/api/audit/capacity-gen", headers=h).json()
        assert any(x["target"] == "S-AM-01" for x in book["deviations"]), "偏离必须记偏"


def test_capacity_change_failure_rolls_back_both():
    """甲路径：失败则容量与代数同滚（新容量低于已订 → 两边零改）。"""
    with TestClient(app) as c:
        h = _login(c)
        # S-AM-01 种子正式册已订 2，新容量 1 低于已订 → 拒写
        r = c.post(
            "/api/slots/capacity",
            headers=h,
            json={"code": "S-AM-01", "capacity": 1},
        )
        assert r.status_code == 409, r.text
        slot = c.get("/api/slots/S-AM-01", headers=h).json()
        assert slot["capacity"] == 10 and slot["generation"] == 1


def test_capacity_change_contention():
    """双人改同一档容量：先者占档锁，后到只记争用、不写业务。"""
    with TestClient(app) as c:
        ha = _login(c, "admin")
        hb = _login(c, "clerk")
        r = c.post("/api/locks/acquire", headers=ha, json={"lock_key": "slot:S-AM-01"})
        assert r.status_code == 200, r.text
        r2 = c.post(
            "/api/slots/capacity",
            headers=hb,
            json={"code": "S-AM-01", "capacity": 12},
        )
        assert r2.status_code == 409, r2.text
        cont = c.get("/api/contention", headers=ha).json()["items"]
        assert any(
            x["actor"] == "clerk" and x["lock_key"] == "slot:S-AM-01" for x in cont
        ), "后到只记争用"
        slot = c.get("/api/slots/S-AM-01", headers=ha).json()
        assert slot["capacity"] == 10 and slot["generation"] == 1


def test_waitlist_promote_attaches_current_generation():
    """候补晋升成占位也须挂当前代（改容后不得挂旧代）。"""
    with TestClient(app) as c:
        h = _login(c)
        # S-EVE-03 容量 6 → 改容到 8（代 1→2）
        r = c.post(
            "/api/slots/capacity", headers=h, json={"code": "S-EVE-03", "capacity": 8}
        )
        assert r.status_code == 200 and r.json()["generation"] == 2
        # 占满 8 个游离位 → 再占进候补
        for i in range(8):
            c.post(
                "/api/holds",
                headers=h,
                json={"slot_code": "S-EVE-03", "party_key": f"P-W{i}", "seats": 1},
            )
        r = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-EVE-03", "party_key": "P-WAIT9", "seats": 1},
        )
        assert r.json()["disposition"] == "候补"
        # 再改容到 9（代 2→3）→ 晋升候补 → 新占位须挂代 3
        r = c.post(
            "/api/slots/capacity", headers=h, json={"code": "S-EVE-03", "capacity": 9}
        )
        assert r.json()["generation"] == 3
        waits = c.get("/api/waitlist", headers=h).json()["items"]
        wid = [x for x in waits if x["party_key"] == "P-WAIT9"][0]["id"]
        r = c.post("/api/waitlist/promote", headers=h, json={"wait_id": wid})
        assert r.status_code == 200, r.text
        holds = c.get("/api/holds", headers=h).json()["items"]
        hold = [x for x in holds if x["party_key"] == "P-WAIT9"][0]
        assert hold["gen"] == 3
