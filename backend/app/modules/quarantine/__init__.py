# 0-2: dirty lot quarantine — pure decision logic. No DB access here.

ON_SHELF = "on_shelf"
QUARANTINED = "quarantined"
EXPIRED = "expired"
CONSUMED = "consumed"

DIRTY = "dirty"
CLEAN = "clean"

# 清洗动作
ACTION_RESTORE = "restore"  # 核实余量无误 / 已更正 -> 回正区
ACTION_DISCARD = "discard"  # 无法核实 -> 报废丢弃


def is_dirty(lot: dict) -> bool:
    return lot.get("data_quality") == DIRTY


def is_quarantined(lot: dict) -> bool:
    return lot.get("status") == QUARANTINED


def is_normal_lot(lot: dict) -> bool:
    """正区批：非 dirty、在架、余量为正。

    全层正区、按临期候选、顶条预警、过期下架都只看这一类批。
    """
    return (
        lot.get("status") == ON_SHELF
        and not is_dirty(lot)
        and float(lot.get("qty_remain", 0) or 0) > 0
    )


def is_quarantine_lot(lot: dict) -> bool:
    """隔离入口可见：quarantined 状态，或虽未迁移但属脏批（dirty 或余量非正）。"""
    if lot.get("status") == QUARANTINED:
        return True
    if lot.get("status") != ON_SHELF:
        return False
    if is_dirty(lot):
        return True
    return float(lot.get("qty_remain", 0) or 0) <= 0


def quarantine_reason(lot: dict) -> str:
    reasons = []
    if is_dirty(lot):
        reasons.append("dirty")
    try:
        if float(lot.get("qty_remain", 0) or 0) <= 0:
            reasons.append("qty_non_positive")
    except (TypeError, ValueError):
        reasons.append("qty_non_positive")
    return "+".join(reasons) if reasons else ""


def is_expired(lot: dict, today: str) -> bool:
    exp = lot.get("expiry")
    return bool(exp) and exp < today


def clean_preview(lot: dict, today: str) -> dict:
    """清洗预览：只读投影，绝不改 status / qty / data_quality。"""
    if not is_quarantine_lot(lot):
        return {"ok": False, "reason": "not_quarantined"}
    qty = float(lot.get("qty_remain", 0) or 0)
    return {
        "ok": True,
        "lot_id": lot["id"],
        "qty_remain": qty,
        "data_quality": lot.get("data_quality"),
        "expiry": lot.get("expiry"),
        "expired": is_expired(lot, today),
        "reasons": [r for r in quarantine_reason(lot).split("+") if r],
        # restore 合法的前提：清洗后必须是一条正区合法批（余量为正且未过期）；
        # dirty 标记由清洗确认本身清除，不作为禁止恢复的理由。
        "restore_allowed": qty > 0 and not is_expired(lot, today),
    }


def plan_confirm(lot: dict, action: str, today: str, qty: float | None = None) -> dict:
    """决定一次清洗确认后该行的最终字段。纯函数，不改入参。

    收口规则：
    - restore 且余量为正且未过期 -> on_shelf/clean（回正区，按临期体系可见）
    - discard，或 restore 时批日历已过期 -> expired（从正区与顶条消失；
      与「过期下架」对同一 dirty 行最终只留下这一种 status）
    - 非法动作 / 非隔离批 / restore 时余量非正 -> 拒绝（保持隔离，正区条数不变）

    dirty 标记由清洗核实本身清除：restore/discard 成功即落 clean。
    """
    if not is_quarantine_lot(lot):
        return {"ok": False, "reason": "not_quarantined"}
    if action not in (ACTION_RESTORE, ACTION_DISCARD):
        return {"ok": False, "reason": "bad_action"}

    new_qty = float(lot.get("qty_remain", 0) or 0) if qty is None else float(qty)

    if action == ACTION_DISCARD:
        return {
            "ok": True,
            "outcome": "discarded",
            "status": EXPIRED,
            "data_quality": CLEAN,
            "qty_remain": 0.0,
        }

    # restore
    if new_qty <= 0:
        return {"ok": False, "reason": "qty_non_positive"}
    if is_expired({**lot, "qty_remain": new_qty}, today):
        # 批日历已过期：restore 不允许回正区，收口为下架
        return {
            "ok": True,
            "outcome": "expired",
            "status": EXPIRED,
            "data_quality": CLEAN,
            "qty_remain": 0.0,
        }
    return {
        "ok": True,
        "outcome": "restored",
        "status": ON_SHELF,
        "data_quality": CLEAN,
        "qty_remain": new_qty,
    }
