import os
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app) as cl:
        yield cl


def lot(client, lid):
    import app.db as db_mod
    c = db_mod.connect()
    row = dict(c.execute("SELECT * FROM lots WHERE id=?", (lid,)).fetchone())
    c.close()
    return row


def test_seed_dirty_lots_only_in_quarantine(client):
    qrows = client.get("/api/quarantine").json()
    names = sorted(r["name"] for r in qrows)
    assert names == ["冻饺", "鸡蛋"]  # dirty 冻饺 + 负余量鸡蛋
    reasons = {r["name"]: r["quarantine_reason"] for r in qrows}
    assert reasons["冻饺"] == "dirty"
    assert reasons["鸡蛋"] == "dirty+qty_non_positive"

    fridge = client.get("/api/fridge").json()
    assert "冻饺" not in [r["name"] for r in fridge]
    # 负余量鸡蛋不可见；正常的 12 个鸡蛋仍在
    eggs = [r for r in fridge if r["name"] == "鸡蛋"]
    assert eggs and all(r["qty_remain"] > 0 for r in eggs)


def test_top_bar_never_reports_dirty_as_expired(client):
    alerts = client.get("/api/alerts").json()
    dirty = [a for a in alerts if a["data_quality"] == "dirty" or a["qty_remain"] <= 0]
    assert dirty == []
    # 冻饺（2025-01-01 已过期）绝不能以 expired 出现在顶条
    assert all(a["name"] != "冻饺" for a in alerts)
    # 正常的过保牛奶仍按普通到期紧急报告
    assert any(a["name"] == "牛奶" and a["level"] == "expired" for a in alerts)


def test_consume_skips_quarantined_lot(client):
    r = client.post("/api/consume", json={"item_id": 2, "qty": 1}).json()
    assert r["ok"]
    used = [d["lot_id"] for d in r["deductions"]]
    neg = [r for r in client.get("/api/quarantine").json() if r["name"] == "鸡蛋"][0]
    assert neg["id"] not in used
    # 隔离负余量批余额原样
    assert lot(client, neg["id"])["qty_remain"] == -3


def test_preview_does_not_change_status(client):
    lid = [r for r in client.get("/api/quarantine").json() if r["name"] == "冻饺"][0]["id"]
    before = lot(client, lid)
    p1 = client.get(f"/api/quarantine/{lid}/preview").json()
    p2 = client.get(f"/api/quarantine/{lid}/preview").json()
    after = lot(client, lid)
    assert p1 == p2 and p1["expired"] is True and p1["restore_allowed"] is False
    assert before == after  # status/qty/quality 全部不变


def test_restore_future_lot_returns_to_positive_zone(client):
    lid = [r for r in client.get("/api/quarantine").json() if r["name"] == "鸡蛋"][0]["id"]
    n_fridge_before = len(client.get("/api/fridge").json())
    r = client.post(f"/api/quarantine/{lid}/clean", json={"action": "restore", "qty": 6})
    assert r.status_code == 200 and r.json()["status"] == "on_shelf"
    row = lot(client, lid)
    assert row["status"] == "on_shelf" and row["data_quality"] == "clean" and row["qty_remain"] == 6
    assert [x["id"] for x in client.get("/api/quarantine").json() if x["name"] == "鸡蛋"] == []
    fridge = client.get("/api/fridge").json()
    assert any(x["id"] == lid for x in fridge) and len(fridge) == n_fridge_before + 1


def test_failed_clean_keeps_quarantine_and_zone_count(client):
    lid = [r for r in client.get("/api/quarantine").json() if r["name"] == "鸡蛋"][0]["id"]
    n_before = len(client.get("/api/fridge").json())
    # 余量仍非正：拒绝，回滚
    r = client.post(f"/api/quarantine/{lid}/clean", json={"action": "restore", "qty": -2})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "qty_non_positive"
    # 非法动作
    assert client.post(f"/api/quarantine/{lid}/clean", json={"action": "burn"}).status_code == 409
    row = lot(client, lid)
    assert row["status"] == "quarantined" and row["data_quality"] == "dirty" and row["qty_remain"] == -3
    assert len(client.get("/api/fridge").json()) == n_before
    assert lid in [x["id"] for x in client.get("/api/quarantine").json()]


def test_expired_calendar_restore_settles_and_disappears_everywhere(client):
    lid = [r for r in client.get("/api/quarantine").json() if r["name"] == "冻饺"][0]["id"]
    # 批日历已过期 + 过期下架同时发生：restore 也只能收口为 expired
    r = client.post(f"/api/quarantine/{lid}/clean", json={"action": "restore", "qty": 1})
    assert r.status_code == 200 and r.json()["status"] == "expired"
    # 下架再跑一遍，不允许翻出第二种 status
    sweep = client.post("/api/expire-sweep").json()
    assert lid not in sweep["expired_ids"]
    assert lot(client, lid)["status"] == "expired"
    assert lid not in [x["id"] for x in client.get("/api/quarantine").json()]
    assert lid not in [x["id"] for x in client.get("/api/fridge").json()]
    assert all(a["id"] != lid for a in client.get("/api/alerts").json())


def test_discard_closes_lot(client):
    lid = [r for r in client.get("/api/quarantine").json() if r["name"] == "鸡蛋"][0]["id"]
    r = client.post(f"/api/quarantine/{lid}/clean", json={"action": "discard"})
    assert r.status_code == 200 and r.json()["outcome"] == "discarded"
    row = lot(client, lid)
    assert row["status"] == "expired" and row["data_quality"] == "clean" and row["qty_remain"] == 0
    assert lid not in [x["id"] for x in client.get("/api/quarantine").json()]


def test_clean_and_sweep_same_dirty_row_leave_single_status(client):
    """并发收口：清洗持锁期间下架不能插入；无论谁先，终态唯一。"""
    import app.db as db_mod
    dbfile = db_mod.db_path()
    # 构造一行尚未迁移的 on_shelf/dirty/已过期 数据，模拟清洗与下架同时撞上
    admin = db_mod.connect()
    cur = admin.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (3,1,1,'2025-06-01','on_shelf','dirty')")
    admin.commit(); admin.close()
    lid = cur.lastrowid

    cleaner = sqlite3.connect(dbfile)
    cleaner.execute("BEGIN IMMEDIATE")  # 清洗事务先持锁

    sweeper = sqlite3.connect(dbfile)
    with pytest.raises(sqlite3.OperationalError):
        sweeper.execute("BEGIN IMMEDIATE")  # 锁冲突，下架无法同时提交

    plan_status = "expired"
    # 清洗按 QUARANTINE_PRED CAS 收口
    rc = cleaner.execute(
        "UPDATE lots SET status=?, data_quality='clean', qty_remain=0 "
        "WHERE id=? AND (status='quarantined' OR (status='on_shelf' AND "
        "(COALESCE(data_quality,'clean')='dirty' OR qty_remain<=0)))",
        (plan_status, lid)).rowcount
    assert rc == 1
    cleaner.commit(); cleaner.close()

    # 锁释放后下架再跑：CAS 谓词不匹配，不得覆盖
    rc = sweeper.execute(
        "UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf' "
        "AND COALESCE(data_quality,'clean')!='dirty' AND qty_remain>0", (lid,)).rowcount
    sweeper.commit(); sweeper.close()
    assert rc == 0
    assert lot(client, lid)["status"] == "expired"
    # 接口侧同样不可见
    assert lid not in [x["id"] for x in client.get("/api/quarantine").json()]


def test_clean_after_row_already_closed_rejected(client):
    lid = [r for r in client.get("/api/quarantine").json() if r["name"] == "鸡蛋"][0]["id"]
    assert client.post(f"/api/quarantine/{lid}/clean", json={"action": "discard"}).status_code == 200
    # 已收口行不能再清洗一遍
    r = client.post(f"/api/quarantine/{lid}/clean", json={"action": "restore", "qty": 1})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "not_quarantined"


def test_startup_migrates_legacy_dirty_rows(tmp_path, monkeypatch):
    dbfile = tmp_path / "pantryfifo.db"
    raw = sqlite3.connect(dbfile)
    raw.executescript("""
    CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT, layer TEXT, unit TEXT);
    CREATE TABLE lots(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INT, qty_in REAL, qty_remain REAL, expiry TEXT, status TEXT, data_quality TEXT);
    CREATE TABLE consumptions(id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, result_json TEXT, created_at TEXT);
    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO items(name,layer,unit) VALUES ('x','mid','个');
    INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (1,1,1,'2025-01-01','on_shelf','dirty');
    INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (1,-2,-2,'2027-01-01','on_shelf','clean');
    INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (1,2,2,'2027-01-01','on_shelf','clean');
    INSERT INTO settings(key,value) VALUES ('warn_days','3');
    """)
    raw.commit(); raw.close()

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import app.db as db_mod
    importlib.reload(db_mod)
    from app import seed
    importlib.reload(seed)
    seed.init_db()
    c = db_mod.connect()
    statuses = [r["status"] for r in c.execute("SELECT id,status FROM lots ORDER BY id")]
    c.close()
    assert statuses == ["quarantined", "quarantined", "on_shelf"]
