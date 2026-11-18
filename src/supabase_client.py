
import logging
import os

from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY
)

def article_exists(source_url):
    if not source_url:
        return False

    try:
        existing = (
            supabase.table("news_articles")
            .select("id")
            .eq("source_url", source_url)
            .limit(1)
            .execute()
        )

        return bool(existing.data)

    except Exception as error:
        logger.exception("Duplicate check failed: %s", error)
        return True
def save_article(article):
    source_url = article.get("source_url")

    if not source_url:
        logger.warning("Article has no source URL.")
        return False

    try:
        # Check whether this article already exists.
        existing = (
            supabase.table("news_articles")
            .select("id")
            .eq("source_url", source_url)
            .limit(1)
            .execute()
        )

        if existing.data:
            logger.info("Already stored: %s", source_url)
            return False

        event_date = article.get("event_date") or None

        record = {
            "source_name": article.get("source_name"),
            "source_url": source_url,
            "original_title": article.get("original_title"),
            "original_content": article.get("original_content"),
            "headline": article.get("headline"),
            "summary": article.get("summary"),
            "article_content": article.get("article_content"),
            "category": article.get("category"),
            "image_url": article.get("image_url"),
            "image_credit": article.get("image_credit"),
            "event_date": event_date,
            "event_location": article.get("event_location"),
            "status": "pending"
        }

        supabase.table("news_articles").insert(
            record
        ).execute()

        logger.info("Saved for approval: %s", article.get("headline"))
        return True

    except Exception as error:
        logger.exception("Supabase save failed: %s", error)
        return False
