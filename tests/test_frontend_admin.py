import sqlite3

import pytest

import app as larp_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "frontend-admin.db"
    monkeypatch.setattr(larp_app, "DB_PATH", str(db_path))
    larp_app.app.config.update(TESTING=True)
    larp_app.init_db()
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO items (
            id, title, description, price, category, condition, is_overseas,
            seller_name, seller_phone, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'approved', ?, ?)
        """,
        (
            "approved-item", "公開商品", "原描述", 1200, "armor", "used",
            "賣家", "0900000000", "2026-01-01", "2026-01-01",
        ),
    )
    connection.commit()
    connection.close()
    with larp_app.app.test_client() as test_client:
        yield test_client, db_path


def login_admin(client):
    assert client.post(
        "/login", data={"username": "tom", "password": "tom2026"}
    ).status_code == 302
    with client.session_transaction() as session_data:
        return session_data["csrf_token"]


def item_row(db_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM items WHERE id='approved-item'"
    ).fetchone()
    connection.close()
    return row


def edit_data(token, **overrides):
    data = {
        "csrf_token": token,
        "title": "前台修改標題",
        "description": "前台修改描述",
        "price": "4321",
        "category": "armor",
        "condition": "like_new",
    }
    data.update(overrides)
    return data


def test_admin_controls_are_hidden_from_anonymous_users(client):
    test_client, _ = client
    html = test_client.get("/item/approved-item").get_data(as_text=True)
    assert "管理商品" not in html
    assert "/admin/frontend/edit/approved-item" not in html
    assert "/admin/archive/approved-item" not in html


def test_admin_sees_frontend_edit_and_archive_controls(client):
    test_client, _ = client
    login_admin(test_client)
    html = test_client.get("/item/approved-item").get_data(as_text=True)
    assert "管理商品" in html
    assert 'href="/admin">後台</a>' in html
    assert 'href="/logout"' in html
    assert "/admin/frontend/edit/approved-item" in html
    assert "/admin/archive/approved-item" in html
    assert "前台修改" in html
    assert "下架" in html


def test_anonymous_user_cannot_edit_or_archive(client):
    test_client, db_path = client
    assert test_client.post(
        "/admin/frontend/edit/approved-item", data=edit_data("fake")
    ).status_code == 302
    assert test_client.post(
        "/admin/archive/approved-item", data={"csrf_token": "fake"}
    ).status_code == 302
    row = item_row(db_path)
    assert row["title"] == "公開商品"
    assert row["status"] == "approved"


def test_admin_can_edit_approved_item_from_frontend(client):
    test_client, db_path = client
    token = login_admin(test_client)
    response = test_client.post(
        "/admin/frontend/edit/approved-item", data=edit_data(token)
    )
    assert response.status_code == 302
    row = item_row(db_path)
    assert row["title"] == "前台修改標題"
    assert row["description"] == "前台修改描述"
    assert row["price"] == 4321
    assert row["category"] == "armor"
    assert row["condition"] == "like_new"
    assert row["status"] == "approved"


def test_admin_can_archive_item_and_public_can_no_longer_view_it(client):
    test_client, db_path = client
    token = login_admin(test_client)
    response = test_client.post(
        "/admin/archive/approved-item", data={"csrf_token": token}
    )
    assert response.status_code == 302
    assert item_row(db_path)["status"] == "archived"

    test_client.get("/logout")
    detail = test_client.get("/item/approved-item")
    assert detail.status_code == 302
    market_html = test_client.get("/market").get_data(as_text=True)
    assert "公開商品" not in market_html


def test_admin_can_view_archived_item(client):
    test_client, db_path = client
    token = login_admin(test_client)
    test_client.post(
        "/admin/archive/approved-item", data={"csrf_token": token}
    )
    response = test_client.get("/item/approved-item")
    assert response.status_code == 200
    assert "已下架" in response.get_data(as_text=True)
    assert item_row(db_path)["status"] == "archived"


def test_archive_rejects_forged_csrf(client):
    test_client, db_path = client
    login_admin(test_client)
    test_client.post(
        "/admin/archive/approved-item", data={"csrf_token": "forged"}
    )
    assert item_row(db_path)["status"] == "approved"
