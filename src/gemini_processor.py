
import json
import logging
import os
import re
import time

from google import genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL = "gemini-3.8-flash"
MAX_RETRIES = 3

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


class GeminiQuotaExceeded(Exception):
    """Raised when Gemini API quota is exhausted."""
    pass


def extract_json(text):
    """Extract JSON from Gemini's response."""

    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    return json.loads(text)


def is_quota_error(error):
    """Identify quota exhaustion errors."""

    message = str(error).lower()

    return (
        "429" in message
        or "resource_exhausted" in message
        or "quota exceeded" in message
        or "quota limit" in message
    )


def is_temporary_error(error):
    """Identify temporary service errors."""

    message = str(error).lower()

    return (
        "503" in message
        or "unavailable" in message
        or "500" in message
        or "internal server error" in message
    )


def process_article(article):
    """
    Process one collected article with Gemini.

    Returns a processed article dictionary,
    or None if the article cannot be processed.
    Raises GeminiQuotaExceeded if the API quota is exhausted.
    """

    title = (
        article.get("original_title")
        or article.get("title")
        or ""
    )

    content = (
        article.get("original_content")
        or article.get("content")
        or ""
    )

    if not title or not content:
        logger.warning("Skipping article with missing title or content.")
        return None

    prompt = f"""
You are an editor for an English-language Rio de Janeiro
news and events website aimed at international visitors.

Evaluate the following article or event.

If it is relevant to tourists, international visitors,
or people interested in Rio de Janeiro, produce an
English-language version.

If it is not relevant, return:

{{"relevant": false}}

If it is relevant, return ONLY valid JSON with these fields:

{{
    "relevant": true,
    "headline": "Short, attractive English headline",
    "summary": "A concise summary of approximately 40-60 words",
    "article_content": "A clear English-language article",
    "category": "News, Events, Culture, Music, Tourism, or Other"
}}

Do not invent facts, dates, prices, locations, or details.
Preserve the meaning of the original information.
Do not include Markdown formatting.

ORIGINAL TITLE:
{title}

ORIGINAL CONTENT:
{content}
"""

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            logger.info(
                "Sending article to Gemini: %s (attempt %s/%s)",
                title,
                attempt,
                MAX_RETRIES
            )

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt
            )

            if not response.text:
                logger.warning("Gemini returned an empty response.")
                return None

            result = extract_json(response.text)

            if not result.get("relevant", False):
                logger.info("Article rejected by Gemini: %s", title)
                return None

            processed_article = {
                "source_url": article.get("source_url") or article.get("url"),
                "source_name": article.get("source_name") or article.get("source"),
                "original_title": title,
                "original_content": content,
                "headline": result.get("headline", title),
                "summary": result.get("summary", ""),
                "article_content": result.get("article_content", ""),
                "category": result.get("category", "Other"),
                "image_url": article.get("image_url"),
                "image_credit": article.get("image_credit"),
                "event_date": article.get("event_date"),
                "event_location": article.get("event_location"),
            }

            logger.info("Article accepted by Gemini: %s", title)

            return processed_article

        except Exception as error:

            if is_quota_error(error):
                logger.error(
                    "Gemini quota exhausted. Stopping all processing. "
                    "Article: %s | Error: %s",
                    title,
                    error
                )

                raise GeminiQuotaExceeded(
                    "Gemini API quota exhausted."
                ) from error

            if is_temporary_error(error):
                if attempt < MAX_RETRIES:
                    wait_time = 5 * (2 ** (attempt - 1))

                    logger.warning(
                        "Temporary Gemini error. "
                        "Retrying in %s seconds. Error: %s",
                        wait_time,
                        error
                    )

                    time.sleep(wait_time)
                    continue

            logger.error(
                "Gemini processing failed for article '%s': %s",
                title,
                error
            )

            return None

    return None
