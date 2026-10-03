from bot.db import DB


def test_orders_leads_stats(tmp_path):
    db = DB(tmp_path / "t.sqlite3")
    db.touch_user(1, "alice")
    db.touch_user(1, "alice2")
    db.add_calc(1, {"price_cny": 1}, 100.0)

    oid = db.add_order(1, "alice2", "LVVDB21B5MD123456", "+70000000000", 500)
    assert db.get_order(oid)["status"] == "awaiting_payment"
    db.mark_paid(oid, "charge")
    assert db.get_order(oid)["status"] == "paid"
    assert len(db.list_orders()) == 1
    assert db.mark_done(oid)
    assert db.list_orders() == []
    assert not db.mark_done(999)

    free = db.add_order(2, None, "LVVDB21B5MD123457", "@bob", 0)
    assert db.get_order(free)["status"] == "new"

    db.add_lead(1, "alice2", "Zeekr 001", "4 млн", "Тула", "+7")
    s = db.stats()
    assert s == {"users": 1, "calcs": 1, "calcs_7d": 1, "orders": 2, "orders_paid": 1, "leads": 1}
