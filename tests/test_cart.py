import sqlite3

import pytest

import app as larp_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "cart.db"
    monkeypatch.setattr(larp_app, "DB_PATH", str(db_path))
    larp_app.app.config.update(TESTING=True)
    larp_app.init_db()
    connection = sqlite3.connect(db_path)
    connection.executemany(
        "INSERT INTO users (id, username, password_hash, email, created_at) VALUES (?, ?, 'unused', ?, '2026-01-01')",
        [
            ("user-1", "buyer", "buyer@example.com"),
            ("user-2", "other", "other@example.com"),
        ],
    )
    for item_id, title, price, status in [
        ("item-1", "板甲護手", 1200, "approved"),
        ("item-2", "鎖子甲頭套", 2400, "approved"),
        ("item-3", "已下架商品", 900, "archived"),
    ]:
        connection.execute(
            """
            INSERT INTO items (
                id, title, description, price, category, condition, is_overseas,
                seller_name, seller_phone, status, created_at, updated_at
            ) VALUES (?, ?, '', ?, 'armor', 'used', 0, '賣家', '0900', ?, '2026-01-01', '2026-01-01')
            """,
            (item_id, title, price, status),
        )
    connection.commit()
    connection.close()
    monkeypatch.setattr(larp_app, "discord_notify", lambda message: None)
    with larp_app.app.test_client() as test_client:
        yield test_client, db_path


def login_user(client, user_id="user-1", username="buyer"):
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["username"] = username
        session["csrf_token"] = "valid-token"
    return "valid-token"


def cart_rows(db_path):
    connection = sqlite3.connect(db_path)
    rows = connection.execute(
        "SELECT user_id, item_id FROM cart_items ORDER BY item_id"
    ).fetchall()
    connection.close()
    return rows


def test_anonymous_user_cannot_view_or_add_to_cart(client):
    test_client, db_path = client
    assert test_client.get("/cart").status_code == 302
    assert test_client.post(
        "/cart/add/item-1", data={"csrf_token": "fake"}
    ).status_code == 302
    assert cart_rows(db_path) == []


def test_user_can_add_approved_item_once(client):
    test_client, db_path = client
    token = login_user(test_client)
    for _ in range(2):
        response = test_client.post(
            "/cart/add/item-1", data={"csrf_token": token}
        )
        assert response.status_code == 302
    assert cart_rows(db_path) == [("user-1", "item-1")]
    html = test_client.get("/cart").get_data(as_text=True)
    assert "板甲護手" in html
    assert "NT$ 1,200" in html
    assert "購物車 (1)" in html


def test_cart_rejects_forged_csrf_and_unavailable_item(client):
    test_client, db_path = client
    login_user(test_client)
    test_client.post("/cart/add/item-1", data={"csrf_token": "forged"})
    test_client.post("/cart/add/item-3", data={"csrf_token": "valid-token"})
    assert cart_rows(db_path) == []


def test_user_cannot_remove_another_users_cart_item(client):
    test_client, db_path = client
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO cart_items (id, user_id, item_id, created_at) VALUES ('cart-other', 'user-2', 'item-1', '2026')"
    )
    connection.commit()
    connection.close()
    token = login_user(test_client)
    test_client.post(
        "/cart/remove/cart-other", data={"csrf_token": token}
    )
    assert cart_rows(db_path) == [("user-2", "item-1")]


def test_checkout_reserves_all_items_and_clears_cart(client):
    test_client, db_path = client
    token = login_user(test_client)
    for item_id in ("item-1", "item-2"):
        test_client.post(f"/cart/add/{item_id}", data={"csrf_token": token})
    response = test_client.post(
        "/cart/checkout",
        data={
            "csrf_token": token,
            "buyer_name": "湯姆",
            "buyer_phone": "0912345678",
            "buyer_email": "buyer@example.com",
            "notes": "週三面交",
        },
    )
    assert response.status_code == 302
    connection = sqlite3.connect(db_path)
    statuses = connection.execute(
        "SELECT id, status FROM items WHERE id IN ('item-1','item-2') ORDER BY id"
    ).fetchall()
    transactions = connection.execute(
        "SELECT item_id, user_id, order_id, status, deposit_amount, notes FROM transactions ORDER BY item_id"
    ).fetchall()
    connection.close()
    assert statuses == [("item-1", "reservation_requested"), ("item-2", "reservation_requested")]
    assert len(transactions) == 2
    assert {row[0] for row in transactions} == {"item-1", "item-2"}
    assert all(row[1] == "user-1" for row in transactions)
    assert len({row[2] for row in transactions}) == 1
    assert all(row[3] == "reservation_requested" for row in transactions)
    assert [row[4] for row in transactions] == [600, 1200]
    assert all(row[5] == "週三面交" for row in transactions)
    assert cart_rows(db_path) == []


def test_checkout_is_atomic_when_an_item_becomes_unavailable(client):
    test_client, db_path = client
    token = login_user(test_client)
    for item_id in ("item-1", "item-2"):
        test_client.post(f"/cart/add/{item_id}", data={"csrf_token": token})
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE items SET status='archived' WHERE id='item-2'")
    connection.commit()
    connection.close()
    response = test_client.post(
        "/cart/checkout",
        data={
            "csrf_token": token,
            "buyer_name": "湯姆",
            "buyer_phone": "0912345678",
            "buyer_email": "buyer@example.com",
        },
    )
    assert response.status_code == 302
    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT status FROM items WHERE id='item-1'"
    ).fetchone()[0] == "approved"
    assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    connection.close()
    assert cart_rows(db_path) == [("user-1", "item-1"), ("user-1", "item-2")]


def test_legacy_buy_route_requires_login_and_cannot_change_item(client):
    test_client, db_path = client
    response = test_client.post(
        "/buy/item-1",
        data={"buyer_name": "訪客", "buyer_phone": "0912345678"},
    )
    assert response.status_code == 302
    connection = sqlite3.connect(db_path)
    status = connection.execute(
        "SELECT status FROM items WHERE id='item-1'"
    ).fetchone()[0]
    connection.close()
    assert status == "approved"


def test_item_page_uses_cart_action(client):
    test_client, _ = client
    anonymous = test_client.get("/item/item-1").get_data(as_text=True)
    assert "登入後加入購物車" in anonymous
    assert "/buy/item-1" not in anonymous
    login_user(test_client)
    member = test_client.get("/item/item-1").get_data(as_text=True)
    assert 'action="/cart/add/item-1"' in member
    assert "加入購物車" in member
    assert "/buy/item-1" not in member


def test_admin_can_review_and_confirm_reservation(client):
    test_client, db_path = client
    token = login_user(test_client)
    test_client.post("/cart/add/item-1", data={"csrf_token": token})
    test_client.post(
        "/cart/checkout",
        data={
            "csrf_token": token,
            "buyer_name": "湯姆",
            "buyer_phone": "0912345678",
            "buyer_email": "buyer@example.com",
            "notes": "週三面交",
        },
    )
    with test_client.session_transaction() as session:
        session.clear()
        session["admin"] = True
        session["csrf_token"] = "admin-token"
    html = test_client.get("/admin?tab=active").get_data(as_text=True)
    assert "湯姆" in html
    assert "0912345678" in html
    assert "buyer@example.com" in html
    assert "週三面交" in html
    assert "/admin/reservation/confirm/item-1" in html
    response = test_client.post(
        "/admin/reservation/confirm/item-1",
        data={"csrf_token": "admin-token"},
    )
    assert response.status_code == 302
    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT status FROM items WHERE id='item-1'"
    ).fetchone()[0] == "deposit_paid"
    assert connection.execute(
        "SELECT status FROM transactions WHERE item_id='item-1'"
    ).fetchone()[0] == "deposit_paid"
    connection.close()


def test_admin_can_release_reservation_but_forged_csrf_cannot(client):
    test_client, db_path = client
    token = login_user(test_client)
    test_client.post("/cart/add/item-1", data={"csrf_token": token})
    test_client.post(
        "/cart/checkout",
        data={
            "csrf_token": token,
            "buyer_name": "湯姆",
            "buyer_phone": "0912345678",
            "buyer_email": "buyer@example.com",
        },
    )
    with test_client.session_transaction() as session:
        session.clear()
        session["admin"] = True
        session["csrf_token"] = "admin-token"
    test_client.post(
        "/admin/reservation/release/item-1",
        data={"csrf_token": "forged"},
    )
    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT status FROM items WHERE id='item-1'"
    ).fetchone()[0] == "reservation_requested"
    connection.close()
    test_client.post(
        "/admin/reservation/release/item-1",
        data={"csrf_token": "admin-token"},
    )
    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT status FROM items WHERE id='item-1'"
    ).fetchone()[0] == "approved"
    assert connection.execute(
        "SELECT status FROM transactions WHERE item_id='item-1'"
    ).fetchone()[0] == "cancelled"
    connection.close()


def test_detail_pages_have_stable_back_navigation(client):
    test_client, _ = client
    public_cases = {
        "/item/item-1": ('href="/market"', "返回市集", "nav-trail"),
        "/wish": ('href="/wishlist"', "返回許願區", "nav-trail"),
    }
    for path, markers in public_cases.items():
        html = test_client.get(path).get_data(as_text=True)
        for marker in markers:
            assert marker in html
        assert "history.back" not in html

    login_user(test_client)
    member_cases = {
        "/cart": ('href="/market"', "繼續逛市集", "nav-trail"),
        "/list": ('href="/market"', "取消並返回市集", "nav-trail"),
        "/my-items": ('href="/market"', "返回市集", "nav-trail"),
    }
    for path, markers in member_cases.items():
        html = test_client.get(path).get_data(as_text=True)
        for marker in markers:
            assert marker in html
        assert "history.back" not in html


def test_cart_navigation_uses_site_branding(client):
    test_client, _ = client
    login_user(test_client)
    html = test_client.get("/cart").get_data(as_text=True)
    assert 'src="/lion-aquitaine.svg"' in html
    assert 'class="shield"' in html
    assert "Cormorant Garamond" in html


def test_frontend_navigation_matches_session_role(client):
    test_client, _ = client
    public_pages = ("/", "/market", "/wishlist", "/wish", "/about", "/item/item-1")
    for page in public_pages:
        anonymous = test_client.get(page).get_data(as_text=True)
        assert 'href="/login"' in anonymous
        assert 'href="/logout"' not in anonymous
    assert test_client.get("/list").status_code == 302

    login_user(test_client)
    for page in public_pages + ("/list",):
        member = test_client.get(page).get_data(as_text=True)
        assert 'href="/cart"' in member
        assert 'href="/my-items"' in member
        assert 'href="/logout"' in member

    with test_client.session_transaction() as session:
        session.clear()
        session["admin"] = True
        session["csrf_token"] = "admin-token"
    for page in public_pages:
        admin = test_client.get(page).get_data(as_text=True)
        assert 'href="/admin"' in admin
        assert 'href="/logout"' in admin
        assert 'href="/login"' not in admin
