import sqlite3

import pytest

import app as larp_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "search.db"
    monkeypatch.setattr(larp_app, "DB_PATH", str(db_path))
    larp_app.app.config.update(TESTING=True)
    larp_app.init_db()
    connection = sqlite3.connect(db_path)
    rows = [
        ("armor-code", "標準盔甲", "板甲", 3000, "armor", "used"),
        ("armor-legacy", "鎖子甲頭套", "不鏽鋼", 2200, "鎖子甲", "品相完好無瑕疵"),
        ("weapon-code", "泡棉長劍", "訓練武器", 900, "weapon", "used"),
    ]
    for item_id, title, description, price, category, condition in rows:
        connection.execute(
            """
            INSERT INTO items (
                id, title, description, price, category, condition, is_overseas,
                seller_name, seller_phone, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, '賣家', '0900000000', 'approved',
                      '2026-01-01', '2026-01-01')
            """,
            (item_id, title, description, price, category, condition),
        )
    connection.execute(
        "INSERT INTO users (id, username, password_hash, created_at) "
        "VALUES ('user-1', 'buyer', 'unused', '2026-01-01')"
    )
    connection.commit()
    connection.close()
    with larp_app.app.test_client() as test_client:
        yield test_client, db_path


def login_user(client):
    with client.session_transaction() as session:
        session["user_id"] = "user-1"
        session["username"] = "buyer"


def listing_data(**overrides):
    data = {
        "title": "新盔甲",
        "description": "測試商品",
        "price": "1500",
        "category": "armor",
        "condition": "like_new",
        "seller_name": "測試賣家",
        "seller_phone": "0900000000",
        "seller_email": "",
    }
    data.update(overrides)
    return data


def test_category_filter_includes_legacy_armor_values(client):
    test_client, _ = client
    html = test_client.get("/market?category=armor").get_data(as_text=True)
    assert "標準盔甲" in html
    assert "鎖子甲頭套" in html
    assert "泡棉長劍" not in html


def test_keyword_search_matches_title_and_description(client):
    test_client, _ = client
    title_html = test_client.get("/market?q=頭套").get_data(as_text=True)
    description_html = test_client.get("/market?q=訓練武器").get_data(as_text=True)
    assert "鎖子甲頭套" in title_html
    assert "泡棉長劍" in description_html


def test_invalid_price_filter_does_not_crash(client):
    test_client, _ = client
    response = test_client.get("/market?min_price=abc&max_price=xyz")
    assert response.status_code == 200
    assert "標準盔甲" in response.get_data(as_text=True)


def test_market_cards_show_category_label(client):
    test_client, _ = client
    html = test_client.get("/market?category=armor").get_data(as_text=True)
    assert "盔甲" in html
    assert "商品分類" in html


def test_listing_form_has_category_and_condition_options(client):
    test_client, _ = client
    login_user(test_client)
    html = test_client.get("/list").get_data(as_text=True)
    assert 'name="category"' in html
    assert 'value="armor"' in html
    assert 'name="condition"' in html
    assert 'value="like_new"' in html


def test_submit_listing_rejects_invalid_category_or_condition(client):
    test_client, db_path = client
    login_user(test_client)
    response = test_client.post(
        "/list", data=listing_data(category="鎖子甲", condition="完美")
    )
    assert response.status_code == 302
    connection = sqlite3.connect(db_path)
    count = connection.execute(
        "SELECT COUNT(*) FROM items WHERE status='pending'"
    ).fetchone()[0]
    connection.close()
    assert count == 0


def test_submit_listing_stores_standard_category_and_condition(client, monkeypatch):
    test_client, db_path = client
    login_user(test_client)
    monkeypatch.setattr(larp_app, "discord_notify", lambda message: None)
    response = test_client.post("/list", data=listing_data())
    assert response.status_code == 302
    connection = sqlite3.connect(db_path)
    row = connection.execute(
        "SELECT category, condition FROM items WHERE status='pending'"
    ).fetchone()
    connection.close()
    assert row == ("armor", "like_new")
