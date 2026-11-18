
import json
import logging
import re
import requests

from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin, urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "RioNewsCollector/1.0 (tourism information project)"
}

MAX_ARTICLES_PER_SOURCE = 15
TIMEOUT = 25


def load_sources():
    with open("sources.json", "r", encoding="utf-8") as file:
        return json.load(file)["sources"]


def fetch_page(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT
        )
        response.raise_for_status()
        return response.text

    except requests.RequestException as error:
        logger.warning("Could not fetch %s: %s", url, error)
        return None


def clean_text(value):
    if not value:
        return ""

    if isinstance(value, list):
        value = " ".join(str(item) for item in value)

    if isinstance(value, dict):
        value = value.get("name", "")

    return re.sub(r"\s+", " ", str(value)).strip()


def extract_image(value):
    if isinstance(value, list):
        value = value[0] if value else ""

    if isinstance(value, dict):
        value = value.get("url", "")

    return clean_text(value)


def extract_jsonld_items(data):
    items = []

    if isinstance(data, list):
        for item in data:
            items.extend(extract_jsonld_items(item))

    elif isinstance(data, dict):
        if "@graph" in data:
            items.extend(extract_jsonld_items(data["@graph"]))

        else:
            items.append(data)

    return items


def collect_jsonld_events(source, html):
    soup = BeautifulSoup(html, "html.parser")
    articles = []

    for script in soup.find_all(
        "script",
        type="application/ld+json"
    ):
        try:
            data = json.loads(script.string or script.get_text())
            items = extract_jsonld_items(data)

            for item in items:
                item_type = item.get("@type", [])

                if isinstance(item_type, str):
                    item_type = [item_type]

                if not any(
                    "Event" in str(value)
                    for value in item_type
                ):
                    continue

                title = clean_text(item.get("name"))
                description = clean_text(item.get("description"))

                if not title:
                    continue

                event_url = item.get("url") or source["url"]
                event_url = urljoin(source["url"], event_url)

                location = item.get("location", {})
                if isinstance(location, list):
                    location = location[0] if location else {}

                if isinstance(location, dict):
                    location = (
                        location.get("name")
                        or location.get("address")
                        or ""
                    )

                image_url = extract_image(item.get("image"))

                start_date = clean_text(item.get("startDate"))
                event_date = ""

                if start_date:
                    try:
                        event_date = datetime.fromisoformat(
                            start_date.replace("Z", "+00:00")
                        ).date().isoformat()
                    except ValueError:
                        event_date = ""

                articles.append({
                    "source_name": source["name"],
                    "source_url": event_url,
                    "original_title": title,
                    "original_content": description,
                    "image_url": urljoin(source["url"], image_url)
                        if image_url else "",
                    "image_credit": source["name"] if image_url else "",
                    "event_date": event_date,
                    "event_location": clean_text(location),
                    "category": source.get("category", "Events")
                })

        except (json.JSONDecodeError, TypeError, ValueError) as error:
            logger.warning(
                "Could not parse event structured data from %s: %s",
                source["name"],
                error
            )

    return articles


def collect_wordpress_articles(source):
    base_url = source["url"].rstrip("/")
    api_url = base_url + "/wp-json/wp/v2/posts"

    try:
        response = requests.get(
            api_url,
            headers=HEADERS,
            params={
                "per_page": MAX_ARTICLES_PER_SOURCE,
                "_embed": "true"
            },
            timeout=TIMEOUT
        )
        response.raise_for_status()

        posts = response.json()
        articles = []

        for post in posts:
            title = BeautifulSoup(
                post.get("title", {}).get("rendered", ""),
                "html.parser"
            ).get_text(" ", strip=True)

            content = BeautifulSoup(
                post.get("content", {}).get("rendered", ""),
                "html.parser"
            ).get_text("\n\n", strip=True)

            if not title or len(content) < 100:
                continue

            image_url = ""

            embedded = post.get("_embedded", {})
            media = embedded.get("wp:featuredmedia", [])

            if media:
                image_url = media[0].get("source_url", "")

            articles.append({
                "source_name": source["name"],
                "source_url": post.get("link", ""),
                "original_title": title,
                "original_content": content[:15000],
                "image_url": image_url,
                "image_credit": source["name"] if image_url else "",
                "event_date": "",
                "event_location": "",
                "category": source.get("category", "Tourism")
            })

        return articles

    except (requests.RequestException, ValueError) as error:
        logger.warning(
            "WordPress collection failed for %s: %s",
            source["name"],
            error
        )
        return []


def collect_event_source(source):
    html = fetch_page(source["url"])

    if not html:
        return []

    articles = collect_jsonld_events(source, html)

    if articles:
        logger.info(
            "Found %s structured events from %s",
            len(articles),
            source["name"]
        )
        return articles[:MAX_ARTICLES_PER_SOURCE]

    logger.warning(
        "No structured event data found on %s. "
        "This source may need a custom extractor.",
        source["name"]
    )

    return []


def collect_news():
    all_articles = []

    for source in load_sources():
        logger.info("Checking: %s", source["name"])

        source_type = source.get("type", "web")

        if source_type == "wordpress":
            articles = collect_wordpress_articles(source)

        elif source_type == "events":
            articles = collect_event_source(source)

        else:
            logger.warning(
                "Unsupported source type '%s' for %s",
                source_type,
                source["name"]
            )
            articles = []

        all_articles.extend(articles)

    unique_articles = []
    seen_urls = set()

    for article in all_articles:
        url = article.get("source_url", "").strip()

        if not url or url in seen_urls:
            continue

        seen_urls.add(url)
        unique_articles.append(article)

    logger.info(
        "Collected %s unique articles and events",
        len(unique_articles)
    )

    return unique_articles


if __name__ == "__main__":
    articles = collect_news()

    print(json.dumps(
        articles,
        ensure_ascii=False,
        indent=2
    ))
