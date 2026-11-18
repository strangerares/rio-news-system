
import json
import logging
import os

from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"]
)

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_relevant": {
            "type": "BOOLEAN"
        },
        "relevance_reason": {
            "type": "STRING"
        },
        "headline": {
            "type": "STRING"
        },
        "summary": {
            "type": "STRING"
        },
        "article_content": {
            "type": "STRING"
        },
        "category": {
            "type": "STRING"
        },
        "event_date": {
            "type": "STRING"
        },
        "event_location": {
            "type": "STRING"
        }
    },
    "required": [
        "is_relevant",
        "relevance_reason",
        "headline",
        "summary",
        "article_content",
        "category",
        "event_date",
        "event_location"
    ]
}


def process_article(article):
    original_title = article.get("original_title", "")
    original_content = article.get("original_content", "")

    if len(original_content.strip()) < 100:
        logger.warning(
            "Insufficient article content: %s",
            article.get("source_url")
        )
        return None

    prompt = f"""
You are the English-language editor of a tourism news website
for international visitors planning to visit Rio de Janeiro, Brazil.

Evaluate the following article.

OUR AUDIENCE:
Tourists, especially visitors from the USA, Canada and Europe.

ACCEPT:
- Concerts, festivals and cultural events.
- Exhibitions, museums and attractions.
- Restaurants, food festivals and gastronomy.
- Tourism news and visitor experiences.
- Beaches, public events and leisure activities.
- Transport information that affects tourists.
- Important visitor safety and practical travel information.

REJECT:
- Government administration and internal politics.
- Job vacancies and recruitment.
- Administrative announcements unrelated to tourists.
- Routine government procedures.
- Articles with no meaningful visitor interest.

EDITORIAL RULES:

1. Only accept genuinely useful or interesting stories.
2. Write natural, fluent English.
3. Do not translate Portuguese word-for-word.
4. Create a short, attractive headline.
5. Write a summary of approximately 50-80 words.
6. Write an informative article of approximately 150-300 words.
7. Preserve important facts, names, dates and locations.
8. Never invent dates, prices, ticket information or addresses.
9. If information is unavailable, leave the relevant field empty.
10. Do not present an event as confirmed unless the source confirms it.
11. Do not invent quotations or additional facts.
12. Use one category from:
    Events, Music, Culture, Food, Tourism, Beaches,
    Transport, Attractions, Safety, Other.

EVENT DATE:
Use YYYY-MM-DD only when an actual event date is clearly
provided. Otherwise return an empty string.

Return your editorial assessment for this source:

SOURCE: {article.get("source_name", "")}

ORIGINAL TITLE:
{original_title}

ORIGINAL ARTICLE:
{original_content[:12000]}
"""

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=RESPONSE_SCHEMA,
                temperature=0.2,
                max_output_tokens=1800
            )
        )

        result = json.loads(response.text)

        if not result.get("is_relevant"):
            logger.info(
                "Rejected: %s — %s",
                original_title,
                result.get("relevance_reason", "")
            )
            return None

        result["source_name"] = article.get("source_name", "")
        result["source_url"] = article.get("source_url", "")
        result["original_title"] = original_title
        result["original_content"] = original_content

        logger.info("Accepted: %s", result["headline"])

        return result

    except Exception as error:
        logger.exception(
            "Gemini processing failed for %s: %s",
            article.get("source_url"),
            error
        )
        return None


if __name__ == "__main__":
    test_article = {
        "source_name": "Test source",
        "source_url": "https://example.com/test",
        "original_title": "Example Rio tourism article",
        "original_content": (
            "This is an example article with enough text "
            "to demonstrate the processing function. "
            "It is not a real news story."
        ) * 10
    }

    result = process_article(test_article)

    if result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("Article rejected or processing failed.")
