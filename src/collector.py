
import json
import logging
import requests

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "RioNewsCollector/1.0 (tourism information project)"
}

def load_sources():
    with open("sources.json", "r", encoding="utf-8") as file:
        return json.load(file)["sources"]


def fetch_page(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=25
        )
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    except requests.RequestException as error:
        logger.warning("Could not fetch %s: %s", url, error)
        return None


def discover_articles(source):
    source_url = source["url"]
    source_domain = urlparse(source_url).netloc

    soup = fetch_page(source_url)

    if soup is None:
        return []

    articles = []
    seen_urls = set()

    for link in soup.find_all("a", href=True):
        title = link.get_text(" ", strip=True)
        article_url = urljoin(source_url, link["href"])

        parsed_url = urlparse(article_url)

        if parsed_url.netloc != source_domain:
            continue

        if not title or len(title) < 15:
            continue

        if article_url in seen_urls:
            continue

        if any(
            item in parsed_url.path.lower()
            for item in [
                "/category/",
                "/tag/",
                "/author/",
                "/wp-content/",
                "/wp-admin/"
            ]
        ):
            continue

        if parsed_url.path in ["", "/"]:
            continue

        seen_urls.add(article_url)

        articles.append({
            "source_name": source["name"],
            "source_url": article_url,
            "original_title": title,
            "original_content": ""
        })

    return articles


def extract_article(url):
    soup = fetch_page(url)

    if soup is None:
        return ""

    for element in soup([
        "script",
        "style",
        "nav",
        "footer",
        "header",
        "aside"
    ]):
        element.decompose()

    article = soup.find("article") or soup.find("main")

    if article is None:
        article = soup.body

    if article is None:
        return ""

    paragraphs = [
        paragraph.get_text(" ", strip=True)
        for paragraph in article.find_all(["p", "h2", "h3"])
    ]

    return "\n\n".join(
        text for text in paragraphs if len(text) > 40
    )[:15000]


def collect_news():
    all_articles = []

    for source in load_sources():
        logger.info("Checking: %s", source["name"])

        articles = discover_articles(source)

        for article in articles[:15]:
            article["original_content"] = extract_article(
                article["source_url"]
            )

            if article["original_content"]:
                all_articles.append(article)

    logger.info("Collected %s articles", len(all_articles))

    return all_articles


if __name__ == "__main__":
    articles = collect_news()

    print(json.dumps(
        articles,
        ensure_ascii=False,
        indent=2
    ))
