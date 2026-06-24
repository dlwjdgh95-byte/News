"""Feed URL registry.

Only official RSS/API endpoints are used — never HTML scraping, which gets
blocked or returns empty from cloud IPs. Every URL here is validated at fetch
time by ``http.fetch_feed`` before use; broken feeds are skipped.

Google News RSS is keyless and effectively unlimited. We use topic and search
feeds in Korean (hl=ko, gl=KR, ceid=KR:ko) for local relevance.
"""

from __future__ import annotations

# Base for Google News RSS in Korean locale.
GN_BASE = "https://news.google.com/rss"
GN_LOCALE = "hl=ko&gl=KR&ceid=KR:ko"


def gn_topic(topic: str) -> str:
    return f"{GN_BASE}/headlines/section/topic/{topic}?{GN_LOCALE}"


def gn_search(query: str) -> str:
    from urllib.parse import quote
    return f"{GN_BASE}/search?q={quote(query)}&{GN_LOCALE}"


# --- Source A: 시사·정치·국제·한국 -----------------------------------------
SOURCE_A_FEEDS = [
    ("Google News 정치", gn_topic("POLITICS")),
    ("Google News 세계", gn_topic("WORLD")),
    ("Google News 국내", gn_topic("NATION")),
    ("Google News 헤드라인", f"{GN_BASE}?{GN_LOCALE}"),
]

# Guardian sections used when an API key is present (quality boost).
GUARDIAN_SECTIONS_A = ["world", "politics", "global-development"]

# --- Source B: 경제·증시 (fallbacks if NewsData.io fails) ------------------
SOURCE_B_FALLBACK_FEEDS = [
    ("Google News 경제", gn_topic("BUSINESS")),
    ("Google News 증시", gn_search("증시 OR 코스피 OR 환율")),
]
GUARDIAN_SECTIONS_B = ["business"]

# --- Source C: 크립토·핀테크 -----------------------------------------------
# Official project/outlet RSS feeds only.
SOURCE_C_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Google News 크립토", gn_search("암호화폐 OR 비트코인 OR 핀테크")),
]
