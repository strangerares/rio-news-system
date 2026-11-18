
import logging

from collector import collect_news
from gemini_processor import process_article, GeminiQuotaExceeded
from supabase_client import (
    save_article,
    article_exists,
    get_unprocessed_articles,
    update_article
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Rio News Collector")

    # STEP 1: Collect new events.
    articles = collect_news()

    logger.info("Collected %s events", len(articles))

    saved = 0
    duplicates = 0

    # STEP 2: Save original events before using Gemini.
    for article in articles:
        source_url = article.get("source_url", "")

        if article_exists(source_url):
            logger.info("Already stored: %s", source_url)
            duplicates += 1
            continue

        if save_article(article):
            saved += 1

    logger.info("New original events saved: %s", saved)
    logger.info("Duplicates skipped: %s", duplicates)

    # STEP 3: Retrieve all pending events without AI content.
    pending_articles = get_unprocessed_articles()

    processed = 0
    quota_exhausted = False

    # STEP 4: Process pending events with Gemini.
    for article in pending_articles:
        source_url = article.get("source_url", "")

        try:
            result = process_article(article)

        except GeminiQuotaExceeded:
            logger.error(
                "Gemini quota exhausted. "
                "Remaining events will stay saved for later processing."
            )
            quota_exhausted = True
            break

        if result is None:
            logger.warning(
                "Gemini did not process this event: %s",
                source_url
            )
            continue

        # STEP 5: Update the existing Supabase record.
        if update_article(source_url, result):
            processed += 1

    logger.info("News collection completed")
    logger.info("Events saved: %s", saved)
    logger.info("Events processed by Gemini: %s", processed)

    if quota_exhausted:
        logger.warning(
            "Some events remain pending and can be processed in a future run."
        )


if __name__ == "__main__":
    main()
