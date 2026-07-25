import sqlite3

import pytest

import app as larp_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(larp_app, "DB_PATH", str(db_path))
    larp_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    larp_app.init_db()

    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO items (
            id, title, description, price, category, condition, is_overseas,
            seller_name, seller_phone, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            "pending-item", "舊標題", "舊描述", 1200, "護甲", "二手",
            "賣家", "0900000000", "pending", "2026-01-01", "2026-01-01",
        ),
    )
    connection.execute(
        """
        INSERT INTO items (
            id, title, description, price, category, condition, is_overseas,
            seller_name, seller_phone, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            "approved-item", "已上架", "不可從審核入口改", 2200, "護甲", "二手",
            "賣家", "0900000000", "approved", "2026-01-01", "2026-01-01",
        ),
    )
    connection.commit()
    connection.close()

    with larp_app.app.test_client() as test_client:
        yield test_client, db_path


def login_admin(client):
    response = client.post(
        "/login", data={"username": "tom", "password": "tom2026"}
    )
    assert response.status_code == 302
    with client.session_transaction() as session_data:
        return session_data.get("csrf_token", "")


def edit_data(csrf_token, **overrides):
    data = {
        "csrf_token": csrf_token,
        "title": "標題",
        "description": "描述",
        "price": "1500",
        "category": "armor",
        "condition": "used",
    }
    data.update(overrides)
    return data


def read_item(db_path, item_id):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    connection.close()
    return row


def test_edit_pending_item_requires_admin(client):
    test_client, db_path = client
    response = test_client.post(
        "/admin/edit/pending-item",
        data={"title": "新標題", "description": "新描述", "price": "1500"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(("/login", "/register"))
    assert read_item(db_path, "pending-item")["title"] == "舊標題"


def test_admin_can_edit_pending_item(client):
    test_client, db_path = client
    csrf_token = login_admin(test_client)
    response = test_client.post(
        "/admin/edit/pending-item",
        data=edit_data(
            csrf_token,
            title="  新標題  ",
            description="新描述",
            price="1500",
            category="armor",
            condition="like_new",
        ),
    )
    assert response.status_code == 302
    item = read_item(db_path, "pending-item")
    assert item["title"] == "新標題"
    assert item["description"] == "新描述"
    assert item["price"] == 1500
    assert item["category"] == "armor"
    assert item["condition"] == "like_new"
    assert item["status"] == "pending"


@pytest.mark.parametrize(
    ("title", "price"),
    [("", "1500"), ("標題", "0"), ("標題", "abc"), ("標題", "100000000")],
)
def test_edit_rejects_invalid_title_or_price(client, title, price):
    test_client, db_path = client
    csrf_token = login_admin(test_client)
    response = test_client.post(
        "/admin/edit/pending-item",
        data=edit_data(
            csrf_token,
            title=title,
            description="新描述",
            price=price,
            category="armor",
            condition="used",
        ),
    )
    assert response.status_code == 302
    assert read_item(db_path, "pending-item")["title"] == "舊標題"


def test_edit_rejects_invalid_category_or_condition(client):
    test_client, db_path = client
    csrf_token = login_admin(test_client)
    response = test_client.post(
        "/admin/edit/pending-item",
        data=edit_data(
            csrf_token,
            category="任意分類",
            condition="任意品相",
        ),
    )
    assert response.status_code == 302
    item = read_item(db_path, "pending-item")
    assert item["title"] == "舊標題"
    assert item["category"] == "護甲"
    assert item["condition"] == "二手"


def test_existing_admin_session_gets_csrf_token(client):
    test_client, _ = client
    with test_client.session_transaction() as session_data:
        session_data["admin"] = True
    response = test_client.get("/admin?tab=pending")
    assert response.status_code == 200
    with test_client.session_transaction() as session_data:
        assert session_data["csrf_token"]


def test_admin_form_maps_legacy_category_and_condition_values(client):
    test_client, _ = client
    login_admin(test_client)
    response = test_client.get("/admin?tab=pending")
    html = response.get_data(as_text=True)
    assert '<option value="armor" selected>盔甲</option>' in html
    assert '<option value="used" selected>二手</option>' in html


def test_edit_rejects_missing_csrf_token(client):
    test_client, db_path = client
    login_admin(test_client)
    response = test_client.post(
        "/admin/edit/pending-item",
        data=edit_data(""),
    )
    assert response.status_code == 302
    assert read_item(db_path, "pending-item")["title"] == "舊標題"


def test_edit_rejects_wrong_csrf_token(client):
    test_client, db_path = client
    login_admin(test_client)
    response = test_client.post(
        "/admin/edit/pending-item",
        data=edit_data("forged-token"),
    )
    assert response.status_code == 302
    assert read_item(db_path, "pending-item")["title"] == "舊標題"


def test_edit_route_does_not_modify_approved_item(client):
    test_client, db_path = client
    csrf_token = login_admin(test_client)
    response = test_client.post(
        "/admin/edit/approved-item",
        data=edit_data(
            csrf_token,
            title="不該成功",
            description="不該成功",
            price="999",
            category="armor",
            condition="used",
        ),
    )
    assert response.status_code == 302
    assert read_item(db_path, "approved-item")["title"] == "已上架"
