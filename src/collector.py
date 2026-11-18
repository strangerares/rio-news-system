
import json
import logging
import re

import requests

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RioNewsCollector/1.0)"
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
    return re.sub(r"\s+", " ", value or "").strip()


def extract_article(url):
    html = fetch_page(url)

    if not html:
        return "", ""

    soup = BeautifulSoup(html, "html.parser")

    image_url = ""

    image_tag = soup.find(
        "meta",
        property="og:image"
    )

    if image_tag:
        image_url = image_tag.get("content", "")

    if not image_url:
        image = soup.find("img", src=True)
        if image:
            image_url = image["src"]

    for element in soup([
        "script", "style", "nav", "footer",
        "header", "aside", "form"
    ]):
        element.decompose()

    article = soup.find("article") or soup.find("main")

    if article is None:
        article = soup.body

    if article is None:
        return "", image_url

    paragraphs = [
        clean_text(p.get_text(" ", strip=True))
        for p in article.find_all(["p", "h2", "h3"])
    ]

    content = "\n\n".join(
        p for p in paragraphs if len(p) > 35
    )

    return content[:15000], image_url


def collect_city_hall(source):
    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    domain = urlparse(source["url"]).netloc

    for link in soup.find_all("a", href=True):
        title = clean_text(link.get_text(" ", strip=True))
        article_url = urljoin(source["url"], link["href"])

        parsed = urlparse(article_url)

        if parsed.netloc != domain:
            continue

        if len(title) < 20:
            continue

        if article_url in seen:
            continue

        if any(
            path in parsed.path.lower()
            for path in [
                "/category/", "/tag/", "/author/",
                "/wp-content/", "/wp-admin/"
            ]
        ):
            continue

        seen.add(article_url)

        content, image_url = extract_article(article_url)

        if len(content) < 100:
            continue

        articles.append({
            "source_name": source["name"],
            "source_url": article_url,
            "original_title": title,
            "original_content": content,
            "image_url": image_url,
            "image_credit": source["name"] if image_url else "",
            "event_date": "",
            "event_location": "",
            "category": source.get("category", "Tourism")
        })

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    return articles


def collect_vivo_rio(source):
    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    for heading in soup.find_all(["h2", "h3"]):
        title = clean_text(heading.get_text(" ", strip=True))

        if len(title) < 3:
            continue

        # Find the event card containing this heading.
        container = heading

        for _ in range(6):
            if container.parent:
                container = container.parent

            text = clean_text(container.get_text(" ", strip=True))

            if (
                len(text) > len(title) + 15
                and container.find("a", href=True)
            ):
                break

        card_text = clean_text(
            container.get_text(" ", strip=True)
        )

        # Find the event details link.
        detail_url = ""

        for link in container.find_all("a", href=True):
            link_text = clean_text(
                link.get_text(" ", strip=True)
            ).lower()

            if "saiba mais" in link_text:
                detail_url = urljoin(
                    source["url"],
                    link["href"]
                )
                break

        if not detail_url:
            continue

        if detail_url in seen:
            continue

        seen.add(detail_url)

        # Extract the event description from its detail page.
        content, image_url = extract_article(detail_url)

        if not image_url:
            image = container.find("img", src=True)
            if image:
                image_url = urljoin(
                    source["url"],
                    image["src"]
                )

        # Use the card text if the detail page has little text.
        if len(content) < 100:
            content = card_text

        if len(content) < 30:
            continue

        articles.append({
            "source_name": source["name"],
            "source_url": detail_url,
            "original_title": title,
            "original_content": content[:15000],
            "image_url": image_url,
            "image_credit": source["name"] if image_url else "",
            "event_date": "",
            "event_location": "",
            "category": source.get("category", "Music")
        })

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "Vivo Rio extractor found %s events",
        len(articles)
    )

    return articles


def collect_event_source(source):
    if source["name"] == "Vivo Rio":
        return collect_vivo_rio(source)

    logger.warning(
        "Custom extractor not yet implemented for %s",
        source["name"]
    )

    return []


def collect_news():
    all_articles = []

    for source in load_sources():
        logger.info("Checking: %s", source["name"])

        source_type = source.get("type", "web")

        if source_type == "wordpress":
            articles = collect_city_hall(source)

        elif source_type == "events":
            articles = collect_event_source(source)

        else:
            logger.warning(
                "Unsupported source type: %s",
                source_type
            )
            articles = []

        logger.info(
            "%s: collected %s items",
            source["name"],
            len(articles)
        )

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
