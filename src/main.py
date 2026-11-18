
import logging

from collector import collect_news
from gemini_processor import process_article, GeminiQuotaExceeded
from supabase_client import save_article, article_exists

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Rio News Collector")

    articles = collect_news()

    logger.info("Found %s articles", len(articles))

    accepted = 0
    saved = 0
    duplicates = 0
    quota_exhausted = False

    for article in articles:
        source_url = article.get("source_url", "")

        if article_exists(source_url):
            logger.info("Skipping duplicate: %s", source_url)
            duplicates += 1
            continue

        try:
            result = process_article(article)

        except GeminiQuotaExceeded:
            logger.error(
                "Gemini quota exhausted. "
                "Stopping AI processing. Remaining articles will be skipped."
            )
            quota_exhausted = True
            break

        if result is None:
            continue

        accepted += 1

        if save_article(result):
            saved += 1

    logger.info("News collection completed")
    logger.info("Duplicates skipped: %s", duplicates)
    logger.info("Accepted by Gemini: %s", accepted)
    logger.info("Saved for approval: %s", saved)

    if quota_exhausted:
        logger.warning(
            "AI processing was interrupted because the Gemini quota was exhausted."
        )


if __name__ == "__main__":
    main()
