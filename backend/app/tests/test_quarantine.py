import pytest
from fastapi.testclient import TestClient

from app.modules.quarantine import (
    finalize_status, is_isolated, preview_clean, reasons,
)

TODAY = "2026-10-05"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        yield c


def _ids(rows):
    return {r["id"] for r in rows}


def _by_name(rows, name, qty=None):
    for r in rows:
        if r["name"] == name and (qty is None or r["qty_remain"] == qty):
            return r
    raise AssertionError(f"lot not found: {name} qty={qty} in {rows}")


# ---------- pure module ----------

def test_isolated_predicate():
    assert is_isolated({"status": "on_shelf", "data_quality": "dirty", "qty_remain": 1})
    assert is_isolated({"status": "on_shelf", "data_quality": "clean", "qty_remain": 0})
    assert is_isolated({"status": "on_shelf", "data_quality": "clean", "qty_remain": -3})
    assert not is_isolated({"status": "on_shelf", "data_quality": "clean", "qty_remain": 2})
    # finalized rows never re-enter quarantine
    assert not is_isolated({"status": "expired", "data_quality": "dirty", "qty_remain": 1})
    assert not is_isolated({"status": "consumed", "data_quality": "clean", "qty_remain": 0})


def test_reasons():
    assert reasons({"data_quality": "dirty", "qty_remain": -3}) == ["dirty", "non_positive_qty"]
    assert reasons({"data_quality": "clean", "qty_remain": 2}) == []


def test_finalize_status_boundaries():
    assert finalize_status(0, "2026-12-01", TODAY) == "consumed"
    assert finalize_status(-3, None, TODAY) == "consumed"
    assert finalize_status(1, "2026-10-04", TODAY) == "expired"
    # same boundary as the alert bar: expiry <= today is already expired
    assert finalize_status(1, "2026-10-05", TODAY) == "expired"
    assert finalize_status(1, "2026-12-01", TODAY) == "on_shelf"
    assert finalize_status(1, None, TODAY) == "on_shelf"


def test_preview_clean_is_pure():
    lot = {"id": 5, "qty_remain": -3, "expiry": "2026-12-01",
           "status": "on_shelf", "data_quality": "dirty"}
    p = preview_clean(lot, {"qty_remain": 6, "expiry": None}, TODAY)
    assert p["proposed"] == {"qty_remain": 6.0, "expiry": "2026-12-01"}
    assert p["final_status"] == "on_shelf"
    assert lot["qty_remain"] == -3 and lot["data_quality"] == "dirty"


# ---------- API: isolation visibility ----------

def test_seeded_dirty_lots_only_in_quarantine(client):
    q = client.get("/api/quarantine").json()
    egg = _by_name(q, "鸡蛋", -3)
    dum = _by_name(q, "冻饺", 1)
    assert "non_positive_qty" in egg["reasons"] and "dirty" in egg["reasons"]
    assert dum["reasons"] == ["dirty"]
    # positive zone: no dirty, no non-positive
    fridge = client.get("/api/fridge").json()
    assert len(fridge) == 3
    assert all(r["data_quality"] != "dirty" and r["qty_remain"] > 0 for r in fridge)
    assert egg["id"] not in _ids(fridge) and dum["id"] not in _ids(fridge)


def test_topbar_and_fefo_exclude_dirty(client):
    alerts = client.get("/api/alerts").json()
    assert alerts and all(a["data_quality"] != "dirty" for a in alerts)
    assert all(a["name"] != "冻饺" for a in alerts)  # dirty 冻饺不是普通到期紧急
    # 冻饺 only has a dirty lot -> no FEFO candidate -> short
    r = client.post("/api/consume", json={"item_id": 3, "qty": 1})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "short"


# ---------- API: clean flow ----------

def test_preview_does_not_change_status(client):
    egg = _by_name(client.get("/api/quarantine").json(), "鸡蛋", -3)
    p = client.post("/api/quarantine/preview",
                    json={"lot_id": egg["id"], "qty_remain": 6}).json()
    assert p["final_status"] == "on_shelf"
    after = _by_name(client.get("/api/quarantine").json(), "鸡蛋")
    assert after["status"] == "on_shelf"
    assert after["data_quality"] == "dirty" and after["qty_remain"] == -3
    assert len(client.get("/api/fridge").json()) == 3


def test_clean_failure_keeps_quarantine_and_positive_zone(client):
    egg = _by_name(client.get("/api/quarantine").json(), "鸡蛋", -3)
    before = len(client.get("/api/fridge").json())
    r = client.post("/api/quarantine/clean", json={"lot_id": egg["id"], "qty_remain": -1})
    assert r.status_code == 400
    assert egg["id"] in _ids(client.get("/api/quarantine").json())
    assert len(client.get("/api/fridge").json()) == before


def test_clean_back_to_positive_zone(client):
    egg = _by_name(client.get("/api/quarantine").json(), "鸡蛋", -3)
    r = client.post("/api/quarantine/clean",
                    json={"lot_id": egg["id"], "qty_remain": 6}).json()
    assert r["ok"] and r["lot"]["status"] == "on_shelf"
    assert r["lot"]["data_quality"] == "clean" and r["lot"]["qty_remain"] == 6
    fridge = client.get("/api/fridge").json()
    assert egg["id"] in _ids(fridge)
    assert egg["id"] not in _ids(client.get("/api/quarantine").json())
    assert egg["id"] not in _ids(client.get("/api/alerts").json())


def test_clean_expired_dirty_goes_expired_not_topbar(client):
    dum = _by_name(client.get("/api/quarantine").json(), "冻饺", 1)
    r = client.post("/api/quarantine/clean", json={"lot_id": dum["id"]}).json()
    assert r["ok"] and r["lot"]["status"] == "expired"
    assert dum["id"] not in _ids(client.get("/api/fridge").json())
    assert dum["id"] not in _ids(client.get("/api/alerts").json())
    assert dum["id"] not in _ids(client.get("/api/quarantine").json())


def test_clean_zero_qty_written_off(client):
    egg = _by_name(client.get("/api/quarantine").json(), "鸡蛋", -3)
    r = client.post("/api/quarantine/clean",
                    json={"lot_id": egg["id"], "qty_remain": 0}).json()
    assert r["ok"] and r["lot"]["status"] == "consumed" and r["lot"]["qty_remain"] == 0
    assert egg["id"] not in _ids(client.get("/api/fridge").json())
    assert egg["id"] not in _ids(client.get("/api/quarantine").json())


def test_clean_and_sweep_leave_single_status(client):
    q = client.get("/api/quarantine").json()
    egg = _by_name(q, "鸡蛋", -3)
    dum = _by_name(q, "冻饺", 1)
    # sweep commits first on the dirty 冻饺 -> clean must not resurrect it
    client.post("/api/expire-sweep")
    r = client.post("/api/quarantine/clean", json={"lot_id": dum["id"]}).json()
    assert not r["ok"] and r["reason"] in ("not_in_quarantine", "already_finalized")
    assert r["lot"]["status"] == "expired"
    # clean commits first (calendar already past) -> sweep must not flip it again
    r = client.post("/api/quarantine/clean",
                    json={"lot_id": egg["id"], "qty_remain": 5, "expiry": "2026-01-01"}).json()
    assert r["ok"] and r["lot"]["status"] == "expired"
    swept = client.post("/api/expire-sweep").json()["expired_ids"]
    assert egg["id"] not in swept
    # closed world: quarantine empty, positive zone all positive, no dirty in top bar
    assert client.get("/api/quarantine").json() == []
    assert all(x["qty_remain"] > 0 for x in client.get("/api/fridge").json())
    assert all(a["data_quality"] != "dirty" for a in client.get("/api/alerts").json())
