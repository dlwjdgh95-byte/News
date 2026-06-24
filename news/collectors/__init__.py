"""News collectors — one module per source (A/B/C).

Each collector exposes ``collect() -> list[Article]`` and is responsible ONLY
for fetching from its sources and mapping the raw items onto the shared
``news.model.Article`` contract. Dedup, filtering, translation and
summarization are downstream stages and must NOT happen here.
"""
