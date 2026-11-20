import logging
import os
from datetime import datetime, timezone

from supabase import create_client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://gbrbakxybodxprcnfpxi.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_KEY else None


def _require_supabase():
    if supabase is None:
        logger.error("Supabase is not configured. Set SUPABASE_SERVICE_KEY or SUPABASE_KEY.")
        return False
    return True


def article_exists(source_url):
    if not source_url or not _require_supabase():
        return False

    try:
        response = (
            supabase.table("news_articles")
            .select("id")
            .eq("source_url", source_url)
            .limit(1)
            .execute()
        )
        return bool(response.data)
    except Exception as error:
        logger.exception("Could not check article: %s", error)
        return False


def save_article(article):
    source_url = article.get("source_url")

    if not source_url:
        logger.warning("Article has no source URL.")
        return False

    if not article.get("source_name"):
        logger.warning("Article has no source name: %s", source_url)
        return False

    if not _require_supabase():
        return False

    try:
        record = {
            "source_name": article.get("source_name"),
            "source_url": source_url,
            "original_title": article.get("original_title") or article.get("title"),
            "original_content": article.get("original_content") or article.get("content"),
            "title_en": article.get("title_en"),
            "content_en": article.get("content_en"),
            "event_location_en": article.get("event_location_en"),
            "image_url": article.get("image_url") or None,
            "image_credit": article.get("image_credit") or None,
            "event_date": article.get("event_date") or None,
            "event_location": article.get("event_location") or None,
            "status": "pending",
        }

        supabase.table("news_articles").insert(record).execute()
        return True
    except Exception as error:
        logger.exception("Could not save article %s: %s", source_url, error)
        return False


def get_unprocessed_articles(limit=10):
    if not _require_supabase():
        return []

    try:
        response = (
            supabase.table("news_articles")
            .select("*")
            .eq("status", "pending")
            .order("collected_at", desc=False)
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception as error:
        logger.exception("Could not retrieve pending articles: %s", error)
        return []


def reprocess_incomplete_articles(limit=100):
    if not _require_supabase():
        return 0

    try:
        response = (
            supabase.table("news_articles")
            .select("id,source_url,headline,summary,article_content,category")
            .eq("status", "published")
            .limit(limit)
            .execute()
        )

        reset_count = 0
        for article in response.data or []:
            if all(article.get(field) for field in (
                "headline", "summary", "article_content", "category"
            )):
                continue

            supabase.table("news_articles").update({
                "status": "pending",
                "published_at": None,
                "headline": None,
                "summary": None,
                "article_content": None,
                "category": None,
            }).eq("id", article["id"]).execute()
            reset_count += 1

        return reset_count
    except Exception as error:
        logger.exception("Could not reset incomplete published articles: %s", error)
        return 0


def update_article(source_url, result):
    if not source_url:
        logger.warning("Article has no source URL; cannot update it.")
        return False

    if not _require_supabase():
        return False

    decision = result.get("decision")
    if decision not in ("publish", "reject", "pending"):
        logger.warning("Invalid article decision: %s", decision)
        return False

    try:
        record = {"status": "published" if decision == "publish" else decision}

        if decision == "publish":
            record.update({
                "headline": result.get("headline", "").strip(),
                "summary": result.get("summary", "").strip(),
                "article_content": result.get("article_content", "").strip(),
                "title_en": result.get("headline", "").strip(),
                "content_en": result.get("article_content", "").strip(),
                "category": result.get("category", "").strip(),
                "image_url": result.get("image_url") or None,
                "image_credit": result.get("image_credit") or None,
                "event_date": result.get("event_date") or None,
                "event_location": result.get("event_location") or None,
                "published_at": datetime.now(timezone.utc).isoformat(),
            })

        supabase.table("news_articles").update(record).eq("source_url", source_url).execute()
        return True
    except Exception as error:
        logger.exception("Could not update article %s: %s", source_url, error)
        return False
