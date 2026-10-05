import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.modules import quarantine as q

app = FastAPI(title="Pantryfifo", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 正区批统一谓词：在架 + 非 dirty + 正余量。
# 脏批只进隔离入口，不进正区 / 按临期候选 / 顶条。
NORMAL_PRED = "lots.status='on_shelf' AND COALESCE(lots.data_quality,'clean')!='dirty' AND lots.qty_remain>0"
# 隔离可见：已隔离，或历史遗留的在架脏批（dirty 或余量非正）。
QUARANTINE_PRED = ("(lots.status='quarantined' OR "
                   "(lots.status='on_shelf' AND (COALESCE(lots.data_quality,'clean')='dirty' OR lots.qty_remain<=0)))")


@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    sql = f"""SELECT lots.*, items.name, items.layer, items.unit FROM lots
              JOIN items ON items.id=lots.item_id WHERE {NORMAL_PRED}"""
    args = []
    if layer:
        sql += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(sql, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        f"""SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
            WHERE {NORMAL_PRED} AND lots.expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    lots = [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' "
        "AND COALESCE(data_quality,'clean')!='dirty' AND qty_remain>0", (body.item_id,))]
    result = consume_fefo(lots, body.qty)
    if not result["ok"] and result["reason"] == "qty_non_positive":
        c.close(); raise HTTPException(400, result["reason"])
    if not result["ok"]:
        c.close(); raise HTTPException(409, result)
    for d in result["deductions"]:
        # 只从正区批扣减：CAS 防止并发清洗把某批收走后仍被扣成负数。
        cur = c.execute(
            "UPDATE lots SET qty_remain = qty_remain - ? "
            "WHERE id=? AND status='on_shelf' AND COALESCE(data_quality,'clean')!='dirty' AND qty_remain>=?",
            (d["take"], d["lot_id"], d["take"]))
        if cur.rowcount == 0:
            c.rollback(); c.close()
            raise HTTPException(409, {"reason": "lot_changed", "lot_id": d["lot_id"]})
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=? AND status='on_shelf'", (d["lot_id"],))
    c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
              (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close(); return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    # 行锁 + 只扫正区批；脏批（quarantined / dirty）不参与，交给清洗收口。
    c.execute("BEGIN IMMEDIATE")
    lots = [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE status='on_shelf' AND COALESCE(data_quality,'clean')!='dirty'")]
    ids = expire_lots(lots, date.today().isoformat())
    expired = []
    for i in ids:
        # SQL 层 CAS：提交瞬间该行若已被清洗收口（非正区状态），绝不覆盖。
        cur = c.execute(
            "UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf' "
            "AND COALESCE(data_quality,'clean')!='dirty' AND qty_remain>0", (i,))
        if cur.rowcount:
            expired.append(i)
    c.commit(); c.close(); return {"expired_ids": expired}

@app.get("/api/quarantine")
def quarantine_list():
    """隔离入口：与全层正区完全分开的脏批列表（附带只读清洗预览）。"""
    c = connect()
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        f"""SELECT lots.*, items.name, items.layer, items.unit FROM lots
            JOIN items ON items.id=lots.item_id WHERE {QUARANTINE_PRED}""")]
    c.close()
    for r in rows:
        r["preview"] = q.clean_preview(r, today)
        r["quarantine_reason"] = q.quarantine_reason(r)
    return rows

@app.get("/api/quarantine/{lot_id}/preview")
def quarantine_preview(lot_id: int):
    """清洗预览：只读，不改 status。"""
    c = connect()
    row = c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    c.close()
    if not row:
        raise HTTPException(404, "lot")
    lot = dict(row)
    if not q.is_quarantine_lot(lot):
        raise HTTPException(409, {"reason": "not_quarantined", "lot_id": lot_id})
    return q.clean_preview(lot, date.today().isoformat())

class CleanIn(BaseModel):
    action: str  # restore | discard
    qty: float | None = None

@app.post("/api/quarantine/{lot_id}/clean")
def quarantine_clean(lot_id: int, body: CleanIn):
    """清洗确认。单事务收口，与过期下架对同一行最多留下一种 status。

    - restore 且余量正、未过期 -> on_shelf/clean，回正区；
    - discard 或日历已过期 -> expired/clean，从正区与顶条消失；
    - 校验失败 / 行已被并发收口 -> 不改 status，批留在隔离，正区条数不变。
    """
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
        if not row:
            c.rollback(); raise HTTPException(404, "lot")
        lot = dict(row)
        if not q.is_quarantine_lot(lot):
            c.rollback()
            raise HTTPException(409, {"reason": "not_quarantined", "lot_id": lot_id})

        today = date.today().isoformat()
        try:
            plan = q.plan_confirm(lot, body.action, today, body.qty)
        except (TypeError, ValueError):
            c.rollback()
            raise HTTPException(400, {"reason": "bad_qty", "lot_id": lot_id})
        if not plan["ok"]:
            c.rollback()
            raise HTTPException(409, {"reason": plan["reason"], "lot_id": lot_id})

        # CAS：仅当该行此刻仍是隔离态时才写入，禁止覆盖下架/消费/已清洗的结果。
        cur = c.execute(
            f"""UPDATE lots SET status=?, data_quality=?, qty_remain=?
                WHERE id=? AND {QUARANTINE_PRED.replace('lots.', '')}""",
            (plan["status"], plan["data_quality"], plan["qty_remain"], lot_id))
        if cur.rowcount == 0:
            # 并发收口：重读终态。同一终态（如双方都判 expired）即幂等成功。
            now = c.execute("SELECT status FROM lots WHERE id=?", (lot_id,)).fetchone()
            final = now["status"] if now else None
            c.rollback()
            if final == plan["status"]:
                return {"lot_id": lot_id, "outcome": plan["outcome"], "status": final, "converged": True}
            raise HTTPException(409, {"reason": "already_closed", "lot_id": lot_id, "status": final})
        c.commit()
    finally:
        c.close()
    return {"lot_id": lot_id, "outcome": plan["outcome"], "status": plan["status"]}

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
