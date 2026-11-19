
import logging
import os
from datetime import datetime, timezone

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

    if not article.get("source_name"):
        logger.warning("Article has no source name: %s", source_url)
        return False

    try:
        record = {
            "source_name": article.get("source_name"),
            "source_url": source_url,
            "original_title": article.get("original_title"),
            "original_content": article.get("original_content"),
            "image_url": article.get("image_url"),
            "image_credit": article.get("image_credit"),
            "event_date": article.get("event_date") or None,
            "event_location": article.get("event_location"),
            "status": "pending"
        }

        supabase.table("news_articles").insert(record).execute()

        logger.info(
            "Saved original event for AI processing: %s",
            article.get("original_title")
        )

        return True

    except Exception as error:
        logger.exception("Supabase save failed: %s", error)
        return False


def get_unprocessed_articles():
    try:
        result = (
            supabase.table("news_articles")
            .select("*")
            .eq("status", "pending")
            .is_("headline", "null")
            .order("collected_at")
            .execute()
        )

        logger.info(
            "Found %s events waiting for AI processing.",
            len(result.data)
        )

        return result.data

    except Exception as error:
        logger.exception(
            "Could not retrieve unprocessed articles: %s",
            error
        )
        return []


def update_article(source_url, article):
    if not source_url:
        logger.warning("Cannot update article without a source URL.")
        return False

    decision = article.get("decision")

    if decision not in ("publish", "reject", "pending"):
        logger.warning(
            "Invalid editorial decision for %s: %s",
            source_url,
            decision
        )
        return False

    try:
        if decision == "publish":
            headline = article.get("headline", "").strip()
            summary = article.get("summary", "").strip()
            article_content = article.get("article_content", "").strip()
            category = article.get("category", "").strip()

            if not all([
                headline,
                summary,
                article_content,
                category
            ]):
                logger.warning(
                    "Cannot publish incomplete article: %s",
                    source_url
                )
                return False

            new_status = "published"

        elif decision == "reject":
            new_status = "rejected"

        else:
            # Keep uncertain articles out of the public website
            # and prevent repeated automatic AI processing.
            new_status = "needs_review"

        record = {
            "status": new_status,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        if decision == "publish":
            record.update({
                "headline": article.get("headline"),
                "summary": article.get("summary"),
                "article_content": article.get("article_content"),
                "category": article.get("category")
            })

        # Preserve source and original content.
        # Update event and image information when supplied.
        optional_fields = [
            "image_url",
            "image_credit",
            "event_date",
            "event_location"
        ]

        if decision == "publish":
            for field in optional_fields:
                if field in article:
                    record[field] = article.get(field) or None

        result = (
            supabase.table("news_articles")
            .update(record)
            .eq("source_url", source_url)
            .execute()
        )

        if not result.data:
            logger.warning(
                "No database record was updated: %s",
                source_url
            )
            return False

        logger.info(
            "Article status updated to '%s': %s",
            new_status,
            article.get("headline") or source_url
        )

        return True

    except Exception as error:
        logger.exception("Supabase update failed: %s", error)
        return False
