
import logging

from collector import collect_news
from gemini_processor import process_article
from supabase_client import save_article

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Rio News Collector")

    articles = collect_news()

    logger.info("Found %s articles", len(articles))

    accepted = 0
    saved = 0

    for article in articles:
        result = process_article(article)

        if result is None:
            continue

        accepted += 1

        if save_article(result):
            saved += 1

    logger.info("News collection completed")
    logger.info("Accepted by Gemini: %s", accepted)
    logger.info("Saved for approval: %s", saved)


if __name__ == "__main__":
    main()
