"""FEFO consume: earliest expiry first among positive remaining, clean lots.

脏批（data_quality='dirty' 或余量非正）不属于按临期体系：不进消费候选，
也不被过期下架触碰——它们只能从隔离入口清洗收口。
"""

DIRTY = "dirty"


def is_normal_lot(lot: dict) -> bool:
    return (
        lot.get("status", "on_shelf") == "on_shelf"
        and lot.get("data_quality") != DIRTY
        and float(lot.get("qty_remain", 0) or 0) > 0
    )


def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if is_normal_lot(l)],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: clean, remaining>0, on_shelf, expiry < today.

    脏批不在此列：dirty 行走隔离清洗收口，避免同一行与清洗争出两种 status。
    """
    out = []
    for l in lots:
        exp = l.get("expiry")
        if (
            l.get("status", "on_shelf") == "on_shelf"
            and l.get("data_quality") != DIRTY
            and exp and exp < today
            and float(l.get("qty_remain", 0) or 0) > 0
        ):
            out.append(l["id"])
    return out
