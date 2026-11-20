
import json
import logging
import os
import re
import time

from google import genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_RETRIES = 1

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


def build_prompt(title, content, source_name):
    """Build the editorial prompt for Gemini."""

    return f"""
You are the senior editor of Rio Tips, an English-language
tourism information website for American visitors to
Rio de Janeiro, Brazil.

Your job is to identify genuinely useful information
for tourists and international visitors.

SOURCE:
{source_name}

ORIGINAL TITLE:
{title}

ORIGINAL CONTENT:
{content}

EDITORIAL POLICY

A. PUBLISH

Select information that is genuinely useful or interesting
to visitors, including:

- Concerts and live music performances.
- Music festivals and cultural festivals.
- Public celebrations and street parties.
- Carnival events and seasonal celebrations.
- Public holidays and special commemorative days.
- Theatre, dance, exhibitions and cultural performances.
- Tourist attractions and special visitor experiences.
- Important tourism announcements and useful visitor information.
- Special events taking place in Rio de Janeiro.

B. REJECT

Reject information that is not useful to tourists, including:

- Crime reports and police incidents.
- Ordinary political news.
- Routine government administration.
- Routine traffic reports and road maintenance.
- Ordinary business advertisements.
- Unrelated local announcements.
- Duplicate or promotional content without useful visitor information.

C. PENDING

Use pending if the information appears potentially useful
but important facts are missing, contradictory or unclear.

Examples:
- An event is announced but its date cannot be established.
- The venue is unclear.
- The article contains too little information to describe
  the event accurately.
- The source information is contradictory.

IMPORTANT EDITORIAL RULES

1. Write in natural, fluent American English.
2. Do not translate literally if a more natural English
   expression communicates the same meaning.
3. Explain Brazilian cultural references briefly when
   necessary for an American reader.
4. Preserve names of performers, venues and organizations.
5. Never invent dates, times, prices, addresses or performers.
6. Never assume an event is upcoming simply because it
   appears on a website.
7. If the event date is in the past, do not publish it
   as an upcoming event.
8. Do not claim that an event is confirmed if the source
   does not establish this.
9. Keep the article informative, useful and factual.
10. Retain uncertainty when the source itself is uncertain.

OUTPUT

Return ONLY valid JSON.

If the article should be published:

{{
    "decision": "publish",
    "headline": "Natural English headline",
    "summary": "Approximately 40-60 words in American English",
    "article_content": "A well-written English article containing
    the useful information from the original source",
    "category": "Events, Music, Festivals, Culture, Tourism,
    Public Holidays, or News",
    "reason": "Brief explanation of why this is useful to tourists"
}}

If the article should be rejected:

{{
    "decision": "reject",
    "reason": "Brief explanation"
}}

If the article requires further checking:

{{
    "decision": "pending",
    "reason": "Explain what important information is missing
    or uncertain"
}}

Do not include Markdown or text outside the JSON.
"""


def process_article(article):
    """
    Process one collected article with Gemini.

    Returns:
        A dictionary containing the editorial decision
        and, if accepted, the English article.

    Returns None if processing fails.
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

    source_name = (
        article.get("source_name")
        or article.get("source")
        or "Unknown source"
    )

    if not title or not content:
        logger.warning(
            "Skipping article with missing title or content."
        )

        return {
            "decision": "pending",
            "reason": "Original title or content is missing."
        }

    prompt = build_prompt(title, content, source_name)

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

            decision = result.get("decision")

            if decision not in ("publish", "reject", "pending"):
                logger.warning(
                    "Gemini returned an invalid decision: %s",
                    decision
                )
                return None

            if decision == "reject":
                logger.info(
                    "Article rejected by Gemini: %s | %s",
                    title,
                    result.get("reason", "")
                )

                return {
                    "decision": "reject",
                    "reason": result.get("reason", "")
                }

            if decision == "pending":
                logger.info(
                    "Article requires further checking: %s | %s",
                    title,
                    result.get("reason", "")
                )

                return {
                    "decision": "pending",
                    "reason": result.get("reason", "")
                }

            headline = result.get("headline", "").strip()
            summary = result.get("summary", "").strip()
            article_content = result.get("article_content", "").strip()
            category = result.get("category", "").strip()

            if not all([
                headline,
                summary,
                article_content,
                category
            ]):
                logger.warning(
                    "Gemini returned incomplete English content: %s",
                    title
                )

                return {
                    "decision": "pending",
                    "reason": "AI-generated article content is incomplete."
                }

            processed_article = {
                "decision": "publish",
                "reason": result.get("reason", ""),
                "source_url": article.get("source_url") or article.get("url"),
                "source_name": source_name,
                "original_title": title,
                "original_content": content,
                "headline": headline,
                "summary": summary,
                "article_content": article_content,
                "category": category,
                "image_url": article.get("image_url"),
                "image_credit": article.get("image_credit"),
                "event_date": article.get("event_date"),
                "event_location": article.get("event_location"),
            }

            logger.info(
                "Article accepted for automatic publishing: %s",
                headline
            )

            return processed_article

        except Exception as error:

            if "404" in str(error):
                logger.error(
                    "Gemini model is unavailable: %s. Configure GEMINI_MODEL with a model available to this API key.",
                    MODEL,
                )
                raise GeminiQuotaExceeded(
                    "Configured Gemini model is unavailable"
                ) from error

            if is_quota_error(error):
                logger.error(
                    "Gemini quota exhausted. Article: %s | Error: %s",
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
                        "Temporary Gemini error. Retrying in %s seconds.",
                        wait_time
                    )

                    time.sleep(wait_time)
                    continue

            logger.exception(
                "Gemini processing failed for article '%s'.",
                title
            )

            return None

    return None
