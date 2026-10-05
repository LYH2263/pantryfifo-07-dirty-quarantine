import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect, immediate_tx
from app.engines.fefo import consume_fefo, expire_lots
from app.modules.quarantine import (
    ISOLATED_WHERE, POSITIVE_WHERE, clean_lot, is_isolated, preview_clean, reasons,
)

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    """Positive zone only: dirty or non-positive lots stay in quarantine."""
    c = connect()
    q = f"""SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE {POSITIVE_WHERE}"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    """Top bar: normal expiry urgency only; isolated lots never listed here."""
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        f"""SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE {POSITIVE_WHERE} AND expiry IS NOT NULL""")]
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
        f"SELECT * FROM lots WHERE item_id=? AND {POSITIVE_WHERE}", (body.item_id,))]
    result = consume_fefo(lots, body.qty)
    if not result["ok"] and result["reason"] == "qty_non_positive":
        c.close(); raise HTTPException(400, result["reason"])
    if not result["ok"]:
        c.close(); raise HTTPException(409, result)
    for d in result["deductions"]:
        c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
    c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
              (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close(); return result

@app.post("/api/expire-sweep")
def expire_sweep():
    """One transaction; each update re-checks the row so a racing clean
    confirm on the same dirty row leaves exactly one status."""
    c = connect()
    today = date.today().isoformat()
    with immediate_tx(c):
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(lots, today)
        for i in ids:
            c.execute(
                """UPDATE lots SET status='expired'
                   WHERE id=? AND status='on_shelf' AND qty_remain>0
                     AND expiry IS NOT NULL AND expiry<?""", (i, today))
    c.close(); return {"expired_ids": ids}

@app.get("/api/quarantine")
def quarantine():
    """Isolation entry: on_shelf lots that are dirty or non-positive."""
    c = connect()
    rows = [dict(r) for r in c.execute(
        f"""SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE {ISOLATED_WHERE} ORDER BY lots.id""")]
    c.close()
    for r in rows: r["reasons"] = reasons(r)
    return rows

class CleanIn(BaseModel):
    lot_id: int
    qty_remain: float | None = None
    expiry: str | None = None

def _check_fix(body: CleanIn):
    if body.qty_remain is not None and body.qty_remain < 0:
        raise HTTPException(400, "qty_negative")
    if body.expiry:
        try: date.fromisoformat(body.expiry)
        except ValueError: raise HTTPException(400, "expiry_invalid")

@app.post("/api/quarantine/preview")
def quarantine_preview(body: CleanIn):
    """清洗预览: pure read + compute, never changes status."""
    _check_fix(body)
    c = connect()
    row = c.execute("SELECT * FROM lots WHERE id=?", (body.lot_id,)).fetchone()
    c.close()
    if not row: raise HTTPException(404, "lot")
    lot = dict(row)
    if not is_isolated(lot):
        return {"ok": False, "reason": "not_in_quarantine", "lot": lot}
    return preview_clean(lot, {"qty_remain": body.qty_remain, "expiry": body.expiry},
                         date.today().isoformat())

@app.post("/api/quarantine/clean")
def quarantine_clean(body: CleanIn):
    """清洗确认: finalize the dirty row atomically. Races with expire-sweep
    resolve to a single status; the response carries the收口 result."""
    _check_fix(body)
    c = connect()
    with immediate_tx(c):
        res = clean_lot(c, body.lot_id, {"qty_remain": body.qty_remain, "expiry": body.expiry},
                        date.today().isoformat())
    c.close()
    if res["reason"] == "not_found": raise HTTPException(404, "lot")
    if res["reason"] == "qty_negative": raise HTTPException(400, "qty_negative")
    return res

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
