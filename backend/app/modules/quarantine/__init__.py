"""脏批隔离 (dirty-batch quarantine).

A lot is isolated while it is still ``on_shelf`` but either
``data_quality='dirty'`` or ``qty_remain <= 0`` (余量非正). Isolated lots:

- only appear in the quarantine entry (``GET /api/quarantine``),
- never enter FEFO / expiry candidates (``POSITIVE_WHERE`` guards them),
- are not treated as normal expiry urgency by the top alert bar.

Cleaning (清洗) is a two-step flow: ``preview_clean`` is pure and never
changes status; ``clean_lot`` finalizes (收口) in one transaction with a
compare-and-swap update, so a concurrent expire-sweep on the same dirty
row leaves exactly one final status.
"""

# on_shelf and (dirty or non-positive remaining) -> quarantine entry only
ISOLATED_WHERE = "status='on_shelf' AND (data_quality='dirty' OR qty_remain<=0)"
# on_shelf, not dirty, positive remaining -> positive zone / candidates / alert bar
POSITIVE_WHERE = "status='on_shelf' AND data_quality!='dirty' AND qty_remain>0"


def is_isolated(lot: dict) -> bool:
    if lot.get("status") != "on_shelf":
        return False
    return lot.get("data_quality") == "dirty" or float(lot.get("qty_remain") or 0) <= 0


def reasons(lot: dict) -> list[str]:
    """Why this lot sits in quarantine (for display)."""
    out = []
    if lot.get("data_quality") == "dirty":
        out.append("dirty")
    if float(lot.get("qty_remain") or 0) <= 0:
        out.append("non_positive_qty")
    return out


def finalize_status(qty_remain: float, expiry: str | None, today: str) -> str:
    """收口 rule for a cleaned lot.

    Non-positive remaining -> ``consumed`` (written off, clamped to 0).
    Calendar already expired (same rule as the alert bar, ``expiry <= today``)
    -> ``expired``, so a cleaned lot never re-enters the shelf while the top
    bar would call it expired. Otherwise back to the positive zone.
    """
    if float(qty_remain) <= 0:
        return "consumed"
    if expiry and expiry <= today:
        return "expired"
    return "on_shelf"


def apply_fix(lot: dict, fix: dict) -> dict:
    """Merge operator corrections over current values (None/'' keeps current)."""
    qty = fix.get("qty_remain")
    expiry = fix.get("expiry")
    return {
        "qty_remain": float(lot["qty_remain"]) if qty is None else float(qty),
        "expiry": lot.get("expiry") if expiry in (None, "") else expiry,
    }


def preview_clean(lot: dict, fix: dict, today: str) -> dict:
    """Pure preview of a clean: computes the收口 result, changes nothing."""
    proposed = apply_fix(lot, fix)
    return {
        "id": lot["id"],
        "current": {
            "qty_remain": lot["qty_remain"],
            "expiry": lot.get("expiry"),
            "status": lot.get("status"),
            "data_quality": lot.get("data_quality"),
        },
        "proposed": proposed,
        "final_status": finalize_status(proposed["qty_remain"], proposed["expiry"], today),
    }


def clean_lot(c, lot_id: int, fix: dict, today: str) -> dict:
    """Confirm a clean inside the caller's transaction.

    The UPDATE is compare-and-swap on the isolation predicate, so if an
    expire-sweep already finalized this row we keep its single status and
    report the current row instead of overwriting it. A failed clean
    (``qty_negative``) writes nothing: the lot stays in quarantine and the
    positive zone is untouched.
    """
    row = c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "not_found", "lot": None}
    lot = dict(row)
    if not is_isolated(lot):
        # e.g. expire-sweep committed first; the row keeps its one status
        return {"ok": False, "reason": "not_in_quarantine", "lot": lot}
    proposed = apply_fix(lot, fix)
    if proposed["qty_remain"] < 0:
        return {"ok": False, "reason": "qty_negative", "lot": lot}
    final = finalize_status(proposed["qty_remain"], proposed["expiry"], today)
    new_qty = 0.0 if final == "consumed" else proposed["qty_remain"]
    cur = c.execute(
        f"""UPDATE lots SET qty_remain=?,
              qty_in=CASE WHEN qty_in<? THEN ? ELSE qty_in END,
              expiry=?, data_quality='clean', status=?
            WHERE id=? AND {ISOLATED_WHERE}""",
        (new_qty, new_qty, new_qty, proposed["expiry"], final, lot_id),
    )
    if cur.rowcount == 0:  # lost the race between check and update
        row = c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
        return {"ok": False, "reason": "already_finalized", "lot": dict(row)}
    lot.update(
        qty_remain=new_qty, expiry=proposed["expiry"], data_quality="clean", status=final
    )
    return {"ok": True, "reason": "", "lot": lot}
