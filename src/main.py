import logging

from collector import collect_news
from gemini_processor import process_article, GeminiQuotaExceeded
from supabase_client import (
    save_article,
    article_exists,
    get_unprocessed_articles,
    reprocess_incomplete_articles,
    update_article
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Rio News Collector")

    # STEP 1: Collect new events and news.
    articles = collect_news()

    logger.info("Collected %s events", len(articles))

    saved = 0
    duplicates = 0

    # STEP 2: Save original information before AI processing.
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

    # STEP 3: Reset incomplete published rows, then retrieve pending work.
    reprocessed = reprocess_incomplete_articles()
    if reprocessed:
        logger.info("Reset %s incomplete articles for AI reprocessing", reprocessed)

    pending_articles = get_unprocessed_articles()

    pending_articles = pending_articles[:10]

    published = 0
    rejected = 0
    needs_review = 0
    failed = 0
    quota_exhausted = False

    # STEP 4: Process each article with Gemini.
    for article in pending_articles:
        source_url = article.get("source_url", "")

        try:
            result = process_article(article)

        except GeminiQuotaExceeded:
            logger.error(
                "Gemini quota exhausted. "
                "Remaining articles will stay pending."
            )
            quota_exhausted = True
            break

        if result is None:
            logger.warning(
                "AI processing failed. Article remains pending: %s",
                source_url
            )
            failed += 1
            continue

        decision = result.get("decision")

        # STEP 5: Apply Gemini's editorial decision.
        if update_article(source_url, result):

            if decision == "publish":
                published += 1

            elif decision == "reject":
                rejected += 1

            elif decision == "pending":
                needs_review += 1

        else:
            logger.error(
                "Could not update article in Supabase: %s",
                source_url
            )
            failed += 1

    # STEP 6: Report results.
    logger.info("===================================")
    logger.info("RIO NEWS COLLECTOR - FINAL REPORT")
    logger.info("===================================")
    logger.info("New events saved: %s", saved)
    logger.info("Duplicates skipped: %s", duplicates)
    logger.info("Automatically published: %s", published)
    logger.info("Rejected: %s", rejected)
    logger.info("Held for review: %s", needs_review)
    logger.info("Processing failures: %s", failed)

    if quota_exhausted:
        logger.warning(
            "Some articles remain pending because Gemini's quota "
            "was exhausted."
        )

    logger.info("News collection completed.")


if __name__ == "__main__":
    main()
