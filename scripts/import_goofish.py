#!/mnt/MainData-1/Share/olv-venv/bin/python3
"""Import selected Goofish listings into LARP Market without cross-item image mixups."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from apify_client import ApifyClient
from opencc import OpenCC
from PIL import Image

DB = Path("/mnt/MainData-1/Share/larp-market/backend/larp-market.db")
UPLOADS = Path("/mnt/MainData-1/Share/larp-market/backend/uploads")
DETAILER = "zen-studio/goofish-xianyu-item-detail-scraper"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
ID_RE = re.compile(r"(?:[?&]id=)(\d+)")
CONVERTER = OpenCC("s2twp")


class ImportFailure(RuntimeError):
    pass


def extract_item_id(value: str) -> str:
    match = ID_RE.search(value)
    if match:
        return match.group(1)
    if value.isdigit():
        return value
    raise ImportFailure(f"無法從連結取得商品 ID：{value}")


def normalize_url(item_id: str) -> str:
    return f"https://www.goofish.com/item?id={item_id}"


def detail_item_id(item: dict) -> str:
    value = item.get("id") or item.get("itemId")
    if value:
        return str(value)
    for field in ("url", "input"):
        match = ID_RE.search(str(item.get(field, "")))
        if match:
            return match.group(1)
    raise ImportFailure(f"詳情資料缺少商品 ID：{item.get('title', '無標題')}")


def fetch_details(urls: list[str], api_key: str, dataset_id: str | None = None) -> dict[str, dict]:
    requested = {extract_item_id(url) for url in urls}
    canonical_urls = [normalize_url(item_id) for item_id in requested]
    if dataset_id is None:
        run = ApifyClient(api_key).actor(DETAILER).call(
            run_input={"startUrls": canonical_urls}, timeout=timedelta(seconds=180)
        )
        dataset_id = run.model_dump().get("default_dataset_id")
        if not dataset_id:
            raise ImportFailure("Apify 沒有回傳 dataset ID")
    response = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    response.raise_for_status()

    details: dict[str, dict] = {}
    for item in response.json():
        item_id = detail_item_id(item)
        if item_id not in requested:
            continue
        if item_id in details:
            raise ImportFailure(f"Apify 重複回傳商品 ID：{item_id}")
        details[item_id] = item

    missing = requested - details.keys()
    if missing:
        raise ImportFailure(f"Apify 缺少商品：{', '.join(sorted(missing))}")
    return details


def image_url(image: object) -> str:
    if isinstance(image, str):
        raw = image
    elif isinstance(image, dict):
        raw = str(image.get("url") or "")
    else:
        raw = ""
    if not raw:
        raise ImportFailure("圖片資料缺少 URL")
    return raw.replace("http://", "https://", 1)


def download_and_validate(item_id: str, images: list[object], stage_dir: Path) -> list[dict]:
    if not images:
        raise ImportFailure(f"商品 {item_id} 沒有圖片")

    manifest = []
    for index, image in enumerate(images[:5], 1):
        url = image_url(image)
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.goofish.com/"},
            timeout=30,
        )
        if response.status_code != 200:
            raise ImportFailure(f"商品 {item_id} 圖片 {index} 下載失敗：HTTP {response.status_code}")
        if len(response.content) < 5000:
            raise ImportFailure(f"商品 {item_id} 圖片 {index} 太小：{len(response.content)} bytes")
        try:
            with Image.open(io.BytesIO(response.content)) as decoded:
                decoded.verify()
        except Exception as exc:
            raise ImportFailure(f"商品 {item_id} 圖片 {index} 無法解碼：{exc}") from exc

        filename = f"larp_{item_id}_{index}.jpg"
        path = stage_dir / filename
        path.write_bytes(response.content)
        manifest.append(
            {
                "index": index,
                "filename": filename,
                "source_url": url,
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "bytes": len(response.content),
            }
        )
    return manifest


def condition_code(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("label", "")
    text = CONVERTER.convert(str(value or "二手"))
    if any(word in text for word in ("近全新", "幾乎全新")):
        return "like_new"
    if any(word in text for word in ("全新", "未使用", "完好無瑕疵")):
        return "new"
    return "used"


def estimate_weight(title: str, description: str) -> float:
    text = title + description
    if any(word in text for word in ("頭盔", "盔", "面甲", "桶盔")):
        return 2.5
    if any(word in text for word in ("手甲", "手套", "臂甲", "護手")):
        return 1.5
    if "胸甲" in text:
        return 4.0
    if any(word in text for word in ("鎖子甲", "札甲")):
        return 3.0
    if "小腿" in text:
        return 3.0
    if any(word in text for word in ("肩甲", "喉甲")):
        return 2.5
    if "全套" in text:
        return 10.0
    return 2.0


def category_for(title: str, description: str) -> str:
    return "armor"


def price_twd(cny: float, weight: float) -> int:
    return int(round((cny * 1.5 * 4.8 + weight * 60) / 10) * 10)


def ensure_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    required = {"source_platform", "source_item_id", "source_url"}
    missing = required - columns
    if missing:
        raise ImportFailure(f"DB 缺少 migration 欄位：{', '.join(sorted(missing))}")


def import_item(connection: sqlite3.Connection, item_id: str, item: dict, stage_root: Path) -> dict:
    existing = connection.execute(
        "SELECT id, title FROM items WHERE source_platform=? AND source_item_id=?",
        ("goofish", item_id),
    ).fetchone()
    if existing:
        raise ImportFailure(f"商品 {item_id} 已匯入：{existing[1]}")

    title = CONVERTER.convert(str(item.get("title") or "").strip())
    description = CONVERTER.convert(str(item.get("description") or "").strip())
    if not title or not description:
        raise ImportFailure(f"商品 {item_id} 缺少標題或描述")
    if re.search(r"[这为后发里无钢锁买卖顺现价开护锈]", title + description):
        raise ImportFailure(f"商品 {item_id} 繁體轉換後仍疑似含簡體字")

    raw_price = float(item.get("price") or 0)
    if raw_price <= 0:
        raise ImportFailure(f"商品 {item_id} 價格無效")

    stage_dir = stage_root / item_id
    stage_dir.mkdir(parents=True)
    manifest = download_and_validate(item_id, item.get("images") or [], stage_dir)
    (stage_dir / "manifest.json").write_text(
        json.dumps({"item_id": item_id, "title": title, "images": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    final_paths = []
    for record in manifest:
        source = stage_dir / record["filename"]
        target = UPLOADS / record["filename"]
        os.replace(source, target)
        final_paths.append(target)

    now = datetime.now().isoformat()
    weight = estimate_weight(title, description)
    final_price = price_twd(raw_price, weight)
    try:
        connection.execute(
            """
            INSERT INTO items (
                id, title, description, price, category, condition, is_overseas,
                image_path, status, seller_name, seller_phone, created_at, updated_at,
                source_platform, source_item_id, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                title,
                description + "\n\n產地：海外，預計 2-4 週到貨。",
                final_price,
                category_for(title, description),
                condition_code(item.get("condition")),
                f"/uploads/{manifest[0]['filename']}",
                "達斯維達995",
                "0900000000",
                now,
                now,
                "goofish",
                item_id,
                normalize_url(item_id),
            ),
        )
    except Exception:
        for path in final_paths:
            path.unlink(missing_ok=True)
        raise

    return {
        "item_id": item_id,
        "title": title,
        "price": final_price,
        "images": len(manifest),
        "manifest": manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="+", help="Goofish 商品連結或 ID")
    parser.add_argument("--commit", action="store_true", help="通過驗證後寫入 DB")
    parser.add_argument("--dataset-id", help="重用既有 Apify dataset，避免重複付費抓取")
    args = parser.parse_args()

    api_key = os.environ.get("APIFY_API_KEY")
    if not api_key:
        raise ImportFailure("缺少 APIFY_API_KEY 環境變數")

    item_ids = [extract_item_id(value.rstrip(".")) for value in args.urls]
    if len(item_ids) != len(set(item_ids)):
        raise ImportFailure("輸入包含重複商品 ID")

    preflight = sqlite3.connect(DB)
    ensure_schema(preflight)
    placeholders = ",".join("?" for _ in item_ids)
    existing = preflight.execute(
        f"SELECT source_item_id, title FROM items WHERE source_platform='goofish' AND source_item_id IN ({placeholders})",
        item_ids,
    ).fetchall()
    preflight.close()
    if existing:
        duplicate_text = "、".join(f"{item_id}（{title}）" for item_id, title in existing)
        raise ImportFailure(f"以下商品已匯入：{duplicate_text}")

    details = fetch_details(item_ids, api_key, args.dataset_id)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".goofish-stage-", dir=UPLOADS))
    results = []
    moved_files: list[Path] = []
    try:
        connection = sqlite3.connect(DB)
        ensure_schema(connection)
        connection.execute("BEGIN")
        for item_id in item_ids:
            result = import_item(connection, item_id, details[item_id], stage_root)
            results.append(result)
            moved_files.extend(UPLOADS / record["filename"] for record in result["manifest"])
            print(f"驗證通過：{item_id}｜{result['title']}｜{result['images']} 張｜NT${result['price']}")
        if args.commit:
            connection.commit()
            print(f"已寫入 {len(results)} 筆，狀態 pending")
        else:
            connection.rollback()
            for result in results:
                for record in result["manifest"]:
                    (UPLOADS / record["filename"]).unlink(missing_ok=True)
            print(f"DRY RUN 通過 {len(results)} 筆，未寫入 DB")
        connection.close()
    except Exception:
        try:
            connection.rollback()
            connection.close()
        except Exception:
            pass
        for path in moved_files:
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ImportFailure as exc:
        print(f"匯入失敗：{exc}", file=sys.stderr)
        raise SystemExit(1)
