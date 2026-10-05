from app.modules import quarantine as q

TODAY = "2026-10-05"


def test_normal_lot_predicate():
    assert q.is_normal_lot({"status": "on_shelf", "data_quality": "clean", "qty_remain": 2})
    # dirty / 非正余量 / 非在架 都不是正区批
    assert not q.is_normal_lot({"status": "on_shelf", "data_quality": "dirty", "qty_remain": 1})
    assert not q.is_normal_lot({"status": "on_shelf", "data_quality": "clean", "qty_remain": 0})
    assert not q.is_normal_lot({"status": "on_shelf", "data_quality": "clean", "qty_remain": -3})
    assert not q.is_normal_lot({"status": "quarantined", "data_quality": "dirty", "qty_remain": 1})
    assert not q.is_normal_lot({"status": "expired", "data_quality": "clean", "qty_remain": 1})


def test_quarantine_predicate():
    dirty_frozen = {"status": "on_shelf", "data_quality": "dirty", "qty_remain": 1, "expiry": "2025-01-01"}
    neg_eggs = {"status": "on_shelf", "data_quality": "dirty", "qty_remain": -3, "expiry": "2026-12-01"}
    moved = {"status": "quarantined", "data_quality": "dirty", "qty_remain": 1}
    clean = {"status": "on_shelf", "data_quality": "clean", "qty_remain": 1}
    assert q.is_quarantine_lot(dirty_frozen)
    assert q.is_quarantine_lot(neg_eggs)
    assert q.is_quarantine_lot(moved)
    assert not q.is_quarantine_lot(clean)
    assert q.quarantine_reason(neg_eggs) == "dirty+qty_non_positive"


def test_preview_is_readonly():
    lot = {"id": 4, "status": "on_shelf", "data_quality": "dirty", "qty_remain": 1, "expiry": "2025-01-01"}
    snap = dict(lot)
    p = q.clean_preview(lot, TODAY)
    assert p["ok"] and p["expired"] is True and p["restore_allowed"] is False
    assert lot == snap  # 预览绝不改行

    neg = {"id": 5, "status": "on_shelf", "data_quality": "dirty", "qty_remain": -3, "expiry": "2026-12-01"}
    p2 = q.clean_preview(neg, TODAY)
    assert set(p2["reasons"]) == {"dirty", "qty_non_positive"}
    assert p2["restore_allowed"] is False


def test_confirm_discard():
    lot = {"id": 4, "status": "quarantined", "data_quality": "dirty", "qty_remain": 1, "expiry": "2025-01-01"}
    p = q.plan_confirm(lot, q.ACTION_DISCARD, TODAY)
    assert p["ok"] and p["status"] == "expired" and p["data_quality"] == "clean" and p["qty_remain"] == 0
    assert lot["status"] == "quarantined"  # 不改入参


def test_confirm_restore_future_valid_qty():
    # dirty 冻饺经核实为 1 袋、日期在未来 -> 清掉 dirty 回正区
    lot = {"id": 9, "status": "quarantined", "data_quality": "dirty", "qty_remain": -3, "expiry": "2026-12-01"}
    p = q.plan_confirm(lot, q.ACTION_RESTORE, TODAY, qty=6)
    assert p["ok"] and p["outcome"] == "restored"
    assert p["status"] == "on_shelf" and p["data_quality"] == "clean" and p["qty_remain"] == 6


def test_confirm_restore_expired_calendar_settles_expired():
    # 清洗确认时批日历已过期：restore 也只能收口为 expired，禁止回正区
    lot = {"id": 4, "status": "quarantined", "data_quality": "dirty", "qty_remain": 1, "expiry": "2025-01-01"}
    p = q.plan_confirm(lot, q.ACTION_RESTORE, TODAY)
    assert p["ok"] and p["outcome"] == "expired" and p["status"] == "expired" and p["qty_remain"] == 0


def test_confirm_failures_keep_quarantined():
    neg = {"id": 5, "status": "quarantined", "data_quality": "dirty", "qty_remain": -3, "expiry": "2026-12-01"}
    assert q.plan_confirm(neg, q.ACTION_RESTORE, TODAY)["reason"] == "qty_non_positive"
    assert q.plan_confirm(neg, q.ACTION_RESTORE, TODAY, qty=0)["reason"] == "qty_non_positive"
    assert q.plan_confirm(neg, "burn", TODAY)["reason"] == "bad_action"
    clean = {"id": 1, "status": "on_shelf", "data_quality": "clean", "qty_remain": 2, "expiry": "2026-12-01"}
    assert not q.plan_confirm(clean, q.ACTION_DISCARD, TODAY)["ok"]
