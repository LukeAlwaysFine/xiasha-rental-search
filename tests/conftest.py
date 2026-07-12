"""测试共享 fixtures — mock 外部依赖 + 标准 listing 工厂"""

import pytest
import sqlite3
import os

# 确保所有外部 API Key 在测试时为空，防止意外调用真实 API
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("AMAP_API_KEY", "")
os.environ.setdefault("FIRECRAWL_API_KEY", "")
os.environ.setdefault("AMAP_JS_KEY", "")


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    """SQLite in-memory 数据库，含完整的 listings 表。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price REAL,
            house_type TEXT,
            address TEXT,
            lng REAL,
            lat REAL,
            area REAL,
            rent_type TEXT,
            landlord_type TEXT,
            images TEXT,
            source_url TEXT UNIQUE,
            source_platform TEXT,
            contact TEXT,
            poster_id TEXT,
            publish_time TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            is_sublet INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_listings_lng_lat ON listings(lng, lat);
        CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
        CREATE INDEX IF NOT EXISTS idx_listings_publish_time ON listings(publish_time);
        CREATE INDEX IF NOT EXISTS idx_listings_poster ON listings(poster_id);
        CREATE INDEX IF NOT EXISTS idx_listings_contact ON listings(contact);
        ALTER TABLE listings ADD COLUMN _llm_score REAL;
        ALTER TABLE listings ADD COLUMN _llm_reason TEXT;
    """)
    return conn


def make_listing(**overrides) -> dict:
    """标准 listing dict 工厂，默认值覆盖易测试的场景。"""
    defaults = {
        "title": "未来科技城精装两居室",
        "price": 2500.0,
        "house_type": "两居",
        "address": "杭州余杭区未来科技城EFC",
        "lng": 120.02,
        "lat": 30.28,
        "area": 85.0,
        "rent_type": "整租",
        "landlord_type": "个人",
        "images": '["https://example.com/img1.jpg"]',
        "source_url": "https://www.douban.com/group/topic/12345",
        "source_platform": "豆瓣",
        "contact": "wx123",
        "poster_id": "user001",
        "publish_time": "2026-07-08T00:00:00",
        "is_sublet": 0,
    }
    defaults.update(overrides)
    return defaults
