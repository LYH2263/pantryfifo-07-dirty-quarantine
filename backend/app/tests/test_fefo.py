from app.engines.fefo import consume_fefo, expire_lots, sort_lots_fefo

def test_fefo_order():
    lots = [
        {"id": 2, "qty_remain": 3, "expiry": "2026-02-01"},
        {"id": 1, "qty_remain": 2, "expiry": "2026-01-10"},
    ]
    assert [l["id"] for l in sort_lots_fefo(lots)] == [1, 2]
    r = consume_fefo(lots, 3)
    assert r["ok"] and r["deductions"][0]["lot_id"] == 1 and r["deductions"][0]["take"] == 2
    assert r["deductions"][1]["take"] == 1

def test_short():
    r = consume_fefo([{"id": 1, "qty_remain": 1, "expiry": "2026-01-01"}], 5)
    assert r["ok"] is False and r["short"] == 4

def test_expire():
    ids = expire_lots([
        {"id": 1, "qty_remain": 1, "expiry": "2025-01-01"},
        {"id": 2, "qty_remain": 1, "expiry": "2027-01-01"},
    ], "2026-01-01")
    assert ids == [1]


def test_dirty_and_non_positive_lots_never_candidates():
    lots = [
        {"id": 1, "qty_remain": 1, "expiry": "2025-01-01", "data_quality": "dirty", "status": "on_shelf"},
        {"id": 2, "qty_remain": -3, "expiry": "2025-02-01", "data_quality": "dirty", "status": "on_shelf"},
        {"id": 3, "qty_remain": 2, "expiry": "2026-03-01", "data_quality": "clean", "status": "on_shelf"},
    ]
    assert [l["id"] for l in sort_lots_fefo(lots)] == [3]
    r = consume_fefo(lots, 2)
    assert r["ok"] and [d["lot_id"] for d in r["deductions"]] == [3]


def test_expire_skips_dirty_even_if_calendar_past():
    ids = expire_lots([
        # 已过期 dirty 批（种子冻饺）不得被下架，留给隔离清洗收口
        {"id": 1, "qty_remain": 1, "expiry": "2025-01-01", "data_quality": "dirty", "status": "on_shelf"},
        {"id": 2, "qty_remain": 0, "expiry": "2025-01-01", "data_quality": "clean", "status": "on_shelf"},
        {"id": 3, "qty_remain": 1, "expiry": "2025-01-01", "data_quality": "clean", "status": "on_shelf"},
    ], "2026-10-05")
    assert ids == [3]
