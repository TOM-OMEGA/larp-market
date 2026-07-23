"""LARP 二手市集 - Flask 後端"""
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import (
    Flask, flash, g, redirect, render_template, request, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# ── Config ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "larp-market.db")
STATIC_DIR = os.path.join(BASE_DIR, "..", "static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")

ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_CONTENT_LEN = 10 * 1024 * 1024  # 10MB

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "larp-market-dev-key-2026")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Discord webhook
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

# Admin auth
ADMIN_USER = os.environ.get("ADMIN_USER", "tom")
ADMIN_PASS_HASH = os.environ.get(
    "ADMIN_PASS_HASH",
    generate_password_hash("tom2026")
)

# ── Helpers ─────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.commit()
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        price INTEGER NOT NULL,
        category TEXT NOT NULL DEFAULT 'weapon',
        condition TEXT NOT NULL DEFAULT 'used',
        is_overseas INTEGER NOT NULL DEFAULT 0,
        seller_name TEXT NOT NULL,
        seller_phone TEXT NOT NULL,
        seller_email TEXT,
        image_path TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        deposit_paid INTEGER NOT NULL DEFAULT 0,
        deposit_buyer_name TEXT,
        deposit_buyer_phone TEXT,
        tracking_number TEXT,
        shipped_at TEXT,
        confirmed_at TEXT,
        transfer_half_at TEXT,
        final_price INTEGER,
        commission_rate REAL DEFAULT 0.10,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS wishes (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        max_price INTEGER,
        contact_name TEXT NOT NULL,
        contact_phone TEXT NOT NULL,
        contact_email TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        buyer_name TEXT NOT NULL,
        buyer_phone TEXT NOT NULL,
        buyer_email TEXT,
        deposit_amount INTEGER NOT NULL,
        final_amount INTEGER,
        status TEXT NOT NULL DEFAULT 'deposit_paid',
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (item_id) REFERENCES items(id)
    );
    """)
    db.commit()
    db.close()


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def save_image(file):
    if not file or file.filename == "":
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        flash("不支援的圖片格式", "error")
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_DIR, filename)
    file.save(path)
    return f"/static/uploads/{filename}"


def discord_notify(msg):
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
    except Exception:
        pass


# ── Routes: Public ───────────────────────────────────────
@app.route("/")
def home():
    db = get_db()
    recent = db.execute(
        "SELECT * FROM items WHERE status='approved' ORDER BY created_at DESC LIMIT 8"
    ).fetchall()
    wishes = db.execute(
        "SELECT * FROM wishes WHERE status='active' ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    return render_template("home.html", items=recent, wishes=wishes)


@app.route("/market")
def market():
    db = get_db()
    category = request.args.get("category", "")
    is_overseas = request.args.get("overseas")
    q = request.args.get("q", "")

    query = "SELECT * FROM items WHERE status='approved'"
    params = []
    if category:
        query += " AND category=? "
        params.append(category)
    if is_overseas == "1":
        query += " AND is_overseas=1"
    elif is_overseas == "0":
        query += " AND is_overseas=0"
    if q:
        query += " AND (title LIKE ? OR description LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    query += " ORDER BY created_at DESC"

    items = db.execute(query, params).fetchall()
    return render_template("market.html", items=items, category=category, q=q,
                           is_overseas=is_overseas)


@app.route("/item/<item_id>")
def item_detail(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        flash("找不到這個商品", "error")
        return redirect(url_for("market"))
    return render_template("item.html", item=item)


@app.route("/wishlist")
def wishlist():
    db = get_db()
    wishes = db.execute(
        "SELECT * FROM wishes WHERE status='active' ORDER BY created_at DESC"
    ).fetchall()
    return render_template("wishlist.html", wishes=wishes)


@app.route("/about")
def about():
    return render_template("about.html")


# ── Routes: Submit ───────────────────────────────────────
@app.route("/list", methods=["GET", "POST"])
def submit_listing():
    if request.method == "POST":
        item_id = uuid.uuid4().hex
        now = datetime.now().isoformat()
        image_path = save_image(request.files.get("image"))

        db = get_db()
        db.execute("""
            INSERT INTO items (
                id, title, description, price, category, condition,
                is_overseas, seller_name, seller_phone, seller_email,
                image_path, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            item_id,
            request.form["title"],
            request.form.get("description", ""),
            int(request.form["price"]),
            request.form.get("category", "weapon"),
            request.form.get("condition", "used"),
            1 if request.form.get("is_overseas") else 0,
            request.form["seller_name"],
            request.form["seller_phone"],
            request.form.get("seller_email", ""),
            image_path,
            now, now
        ))
        db.commit()

        discord_notify(
            f"📦 新上架申請：{request.form['title']}\n"
            f"💰 價格：${request.form['price']}\n"
            f"👤 賣家：{request.form['seller_name']}\n"
            f"🔗 https://larp.example.com/admin"
        )

        flash("已送出審核，請等待通知", "success")
        return redirect(url_for("home"))

    return render_template("list.html")


@app.route("/wish", methods=["GET", "POST"])
def submit_wish():
    if request.method == "POST":
        wish_id = uuid.uuid4().hex
        now = datetime.now().isoformat()

        db = get_db()
        db.execute("""
            INSERT INTO wishes (
                id, title, description, max_price,
                contact_name, contact_phone, contact_email,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """, (
            wish_id,
            request.form["title"],
            request.form.get("description", ""),
            int(request.form.get("max_price", 0)) or None,
            request.form["contact_name"],
            request.form["contact_phone"],
            request.form.get("contact_email", ""),
            now
        ))
        db.commit()

        flash("許願已發布", "success")
        return redirect(url_for("wishlist"))

    return render_template("wish.html")


# ── Routes: Transaction ─────────────────────────────────
@app.route("/buy/<item_id>", methods=["GET", "POST"])
def buy_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id=? AND status='approved'",
                      (item_id,)).fetchone()
    if not item:
        flash("商品不存在或已下架", "error")
        return redirect(url_for("market"))

    if request.method == "POST":
        deposit = int(item["price"] * 0.5)
        now = datetime.now().isoformat()

        db.execute("""
            UPDATE items SET
                status='deposit_paid',
                deposit_paid=1,
                deposit_buyer_name=?,
                deposit_buyer_phone=?,
                updated_at=?
            WHERE id=?
        """, (
            request.form["buyer_name"],
            request.form["buyer_phone"],
            now, item_id
        ))

        tx_id = uuid.uuid4().hex
        db.execute("""
            INSERT INTO transactions (id, item_id, buyer_name, buyer_phone,
                                      buyer_email, deposit_amount, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (tx_id, item_id, request.form["buyer_name"],
              request.form["buyer_phone"], request.form.get("buyer_email", ""),
              deposit, now, now))
        db.commit()

        discord_notify(
            f"💸 訂金已收：{item['title']}\n"
            f"👤 買家：{request.form['buyer_name']} ({request.form['buyer_phone']})\n"
            f"💰 訂金：${deposit}"
        )

        flash(f"訂金 ${deposit} 已記錄，請轉帳至指定帳戶", "success")
        return redirect(url_for("item_detail", item_id=item_id))

    return render_template("buy.html", item=item)


# ── Routes: Auth ───────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        if user == ADMIN_USER and check_password_hash(ADMIN_PASS_HASH, pw):
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("帳號或密碼錯誤", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("home"))


# ── Routes: Admin ───────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    pending = db.execute(
        "SELECT * FROM items WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()
    active = db.execute(
        "SELECT * FROM items WHERE status NOT IN ('pending','sold','cancelled') "
        "ORDER BY updated_at DESC"
    ).fetchall()
    return render_template("admin.html", pending=pending, active=active)


@app.route("/admin/approve/<item_id>", methods=["POST"])
@admin_required
def approve_item(item_id):
    db = get_db()
    db.execute("UPDATE items SET status='approved', updated_at=? WHERE id=?",
               (datetime.now().isoformat(), item_id))
    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    db.commit()

    discord_notify(
        f"✅ 審核通過：{item['title']}\n💰 ${item['price']}"
    )
    flash("已核准上架", "success")
    return redirect(url_for("admin"))


@app.route("/admin/reject/<item_id>", methods=["POST"])
@admin_required
def reject_item(item_id):
    db = get_db()
    db.execute("UPDATE items SET status='rejected', updated_at=? WHERE id=?",
               (datetime.now().isoformat(), item_id))
    db.commit()
    flash("已駁回", "success")
    return redirect(url_for("admin"))


@app.route("/admin/ship/<item_id>", methods=["POST"])
@admin_required
def ship_item(item_id):
    db = get_db()
    tracking = request.form.get("tracking_number", "").strip()
    now = datetime.now().isoformat()

    db.execute("""
        UPDATE items SET status='shipped', tracking_number=?,
        shipped_at=?, transfer_half_at=?, updated_at=? WHERE id=?
    """, (tracking, now, now, now, item_id))
    db.commit()

    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    half = int(item["price"] * 0.5)
    discord_notify(
        f"📮 已出貨：{item['title']}\n"
        f"🔢 追蹤：{tracking}\n"
        f"💸 轉帳一半：${half}"
    )
    flash(f"已出貨，請轉帳 ${half} 給賣家", "success")
    return redirect(url_for("admin"))


@app.route("/admin/confirm/<item_id>", methods=["POST"])
@admin_required
def confirm_item(item_id):
    db = get_db()
    now = datetime.now().isoformat()
    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    remaining = int(item["price"] * 0.5)
    db.execute("""
        UPDATE items SET status='confirmed', confirmed_at=?, updated_at=?
        WHERE id=?
    """, (now, now, item_id))
    db.commit()

    discord_notify(
        f"✔️ 已確認收到：{item['title']}\n"
        f"💸 轉帳尾款：${remaining}"
    )
    flash(f"已確認，請轉帳尾款 ${remaining} 給賣家", "success")
    return redirect(url_for("admin"))


@app.route("/admin/close/<item_id>", methods=["POST"])
@admin_required
def close_item(item_id):
    db = get_db()
    db.execute("UPDATE items SET status='sold', updated_at=? WHERE id=?",
               (datetime.now().isoformat(), item_id))
    db.commit()
    flash("已結案", "success")
    return redirect(url_for("admin"))


@app.route("/admin/cancel/<item_id>", methods=["POST"])
@admin_required
def cancel_item(item_id):
    db = get_db()
    db.execute("UPDATE items SET status='cancelled', updated_at=? WHERE id=?",
               (datetime.now().isoformat(), item_id))
    db.commit()
    flash("已取消", "success")
    return redirect(url_for("admin"))


@app.route("/admin/wishes")
@admin_required
def admin_wishes():
    db = get_db()
    wishes = db.execute(
        "SELECT * FROM wishes ORDER BY created_at DESC"
    ).fetchall()
    return render_template("admin_wishes.html", wishes=wishes)


# ── Init ─────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
