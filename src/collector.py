
import json
import logging
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RioNewsCollector/1.0; "
        "+https://rioprivatedriver.com)"
    )
}

MAX_ARTICLES_PER_SOURCE = 8
TIMEOUT = (5, 15)
MIN_ARTICLE_LENGTH = 100

CANCELLED_TERMS = [
    "evento cancelado",
    "event cancelled",
    "event canceled",
    "show cancelado",
    "cancelled",
    "canceled",
    "cancelado",
    "cancelada",
]

EXCLUDED_PATHS = [
    "/category/",
    "/categoria/",
    "/tag/",
    "/author/",
    "/page/",
    "/wp-content/",
    "/wp-admin/",
    "/wp-json/",
    "/feed/",
    "/search/",
    "/politica-de-privacidade",
    "/privacy-policy",
]

PORTUGUESE_MONTHS = {
    "janeiro": "01",
    "fevereiro": "02",
    "marco": "03",
    "março": "03",
    "abril": "04",
    "maio": "05",
    "junho": "06",
    "julho": "07",
    "agosto": "08",
    "setembro": "09",
    "outubro": "10",
    "novembro": "11",
    "dezembro": "12",
}


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

        content_type = response.headers.get("Content-Type", "").lower()

        if "text/html" not in content_type:
            logger.warning("Not an HTML page: %s", url)
            return None

        return response.text

    except requests.RequestException as error:
        logger.warning("Could not fetch %s: %s", url, error)
        return None


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_url(url, base_url):
    if not url:
        return ""

    url = urljoin(base_url, url.strip())
    url, _ = urldefrag(url)

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return ""

    return url


def same_domain(url, base_url):
    return (
        urlparse(url).netloc.lower().removeprefix("www.")
        == urlparse(base_url).netloc.lower().removeprefix("www.")
    )


def is_excluded_url(url):
    path = urlparse(url).path.lower()

    return any(item in path for item in EXCLUDED_PATHS)


def is_cancelled(text):
    normalized = clean_text(text).lower()

    return any(term in normalized for term in CANCELLED_TERMS)


def extract_date(text):
    """
    Extract common Brazilian or international date formats.
    Returns YYYY-MM-DD when a clear date is found.
    """

    text = clean_text(text).lower()

    # DD/MM/YYYY or DD-MM-YYYY
    match = re.search(
        r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b",
        text
    )

    if match:
        day, month, year = match.groups()

        try:
            return datetime(
                int(year), int(month), int(day)
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # YYYY-MM-DD
    match = re.search(
        r"\b(20\d{2})-(\d{2})-(\d{2})\b",
        text
    )

    if match:
        try:
            return datetime.strptime(
                match.group(0), "%Y-%m-%d"
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Portuguese: 19 de novembro de 2026
    match = re.search(
        r"\b(\d{1,2})\s+de\s+"
        r"(janeiro|fevereiro|março|marco|abril|maio|junho|"
        r"julho|agosto|setembro|outubro|novembro|dezembro)"
        r"\s+de\s+(20\d{2})\b",
        text
    )

    if match:
        day, month_name, year = match.groups()
        month = PORTUGUESE_MONTHS.get(month_name)

        if month:
            try:
                return datetime.strptime(
                    f"{year}-{month}-{int(day):02d}",
                    "%Y-%m-%d"
                ).strftime("%Y-%m-%d")
            except ValueError:
                pass

    return ""


def extract_location(text):
    """
    Conservative location extraction.
    This is only a hint for Gemini, not a verified venue.
    """

    text = clean_text(text)

    patterns = [
        r"(?:local|venue|onde)\s*:\s*([^|.;]{3,100})",
        r"(?:endereço|endereco)\s*:\s*([^|.;]{3,100})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return clean_text(match.group(1))

    return ""


def extract_image(container, page_url):
    image = container.find("img", src=True)

    if image:
        image_url = (
            image.get("data-src")
            or image.get("data-lazy-src")
            or image.get("src")
        )

        if image_url:
            return normalize_url(image_url, page_url)

    image = container.find("img", attrs={"data-src": True})

    if image:
        return normalize_url(image.get("data-src"), page_url)

    return ""


def extract_article(url):
    html = fetch_page(url)

    if not html:
        return "", ""

    soup = BeautifulSoup(html, "html.parser")

    image_url = ""

    for property_name in [
        "og:image",
        "twitter:image",
    ]:
        image_tag = soup.find(
            "meta",
            attrs={"property": property_name}
        ) or soup.find(
            "meta",
            attrs={"name": property_name}
        )

        if image_tag and image_tag.get("content"):
            image_url = normalize_url(
                image_tag["content"],
                url
            )
            break

    for element in soup([
        "script", "style", "nav", "footer",
        "header", "aside", "form", "noscript"
    ]):
        element.decompose()

    article = (
        soup.find("article")
        or soup.find("main")
        or soup.body
    )

    if article is None:
        return "", image_url

    paragraphs = [
        clean_text(element.get_text(" ", strip=True))
        for element in article.find_all([
            "p", "h2", "h3", "li"
        ])
    ]

    content = "\n\n".join(
        paragraph
        for paragraph in paragraphs
        if len(paragraph) > 35
    )

    return content[:15000], image_url


def get_card_container(element):
    """
    Find a nearby event/article card without jumping
    too far up the page and mixing different events.
    """

    current = element

    for _ in range(6):
        if current.parent is None:
            break

        current = current.parent

        text = clean_text(current.get_text(" ", strip=True))
        links = current.find_all("a", href=True)

        if (
            len(text) >= 30
            and len(text) <= 2500
            and links
        ):
            return current

    return element.parent or element


def find_detail_url(heading, container, source_url):
    """
    Prefer the link containing the heading.
    Otherwise use a clear event-detail or learn-more link
    from the same card.
    """

    heading_link = heading.find_parent("a", href=True)

    if heading_link:
        candidate = normalize_url(
            heading_link["href"],
            source_url
        )

        if candidate and same_domain(candidate, source_url):
            return candidate

    links = container.find_all("a", href=True)

    # Prefer explicit event detail links.
    for link in links:
        link_text = clean_text(
            link.get_text(" ", strip=True)
        ).lower()

        if any(term in link_text for term in [
            "saiba mais",
            "mais informações",
            "mais informacoes",
            "learn more",
            "ver evento",
            "detalhes",
            "ingressos",
            "comprar"
        ]):
            candidate = normalize_url(
                link["href"],
                source_url
            )

            if (
                candidate
                and same_domain(candidate, source_url)
                and not is_excluded_url(candidate)
            ):
                return candidate

    # If there is exactly one internal link, it is a
    # reasonable fallback for this individual card.
    internal_links = []

    for link in links:
        candidate = normalize_url(
            link["href"],
            source_url
        )

        if (
            candidate
            and same_domain(candidate, source_url)
            and not is_excluded_url(candidate)
            and candidate.rstrip("/") != source_url.rstrip("/")
        ):
            if candidate not in internal_links:
                internal_links.append(candidate)

    if len(internal_links) == 1:
        return internal_links[0]

    return ""


def build_article(source, title, detail_url, content,
                  image_url="", event_date="", event_location=""):

    if not title or not detail_url:
        return None

    if is_cancelled(title + " " + content):
        logger.info("Skipping cancelled event: %s", title)
        return None

    if len(content) < MIN_ARTICLE_LENGTH:
        logger.info("Skipping short content: %s", title)
        return None

    return {
        "source_name": source["name"],
        "source_url": detail_url,
        "original_title": title,
        "original_content": content[:15000],
        "image_url": image_url,
        "image_credit": source["name"] if image_url else "",
        "event_date": event_date or extract_date(content),
        "event_location": event_location or extract_location(content),
        "category": source.get("category", "Tourism")
    }


def collect_city_hall(source):
    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    blocks = soup.find_all("article")

    if not blocks:
        blocks = soup.find_all(
            ["div", "li"],
            class_=re.compile(
                r"post|article|card|news|noticia",
                re.IGNORECASE
            )
        )

    for block in blocks:
        heading = block.find(["h1", "h2", "h3", "h4"])

        if not heading:
            continue

        title = clean_text(heading.get_text(" ", strip=True))

        if len(title) < 12:
            continue

        detail_url = find_detail_url(
            heading,
            block,
            source["url"]
        )

        if not detail_url or detail_url in seen:
            continue

        seen.add(detail_url)

        content, image_url = extract_article(detail_url)

        if not image_url:
            image_url = extract_image(block, source["url"])

        article = build_article(
            source,
            title,
            detail_url,
            content,
            image_url,
            extract_date(content),
            extract_location(content)
        )

        if article:
            articles.append(article)
            logger.info("City Hall article found: %s", title)

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s extractor found %s articles",
        source["name"],
        len(articles)
    )

    return articles


def collect_event_source(source):
    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    headings = soup.find_all(["h2", "h3", "h4"])

    for heading in headings:
        title = clean_text(heading.get_text(" ", strip=True))

        if len(title) < 3 or len(title) > 180:
            continue

        container = get_card_container(heading)

        card_text = clean_text(
            container.get_text(" ", strip=True)
        )

        if len(card_text) < 30:
            continue

        if is_cancelled(title + " " + card_text):
            logger.info("Cancelled event skipped: %s", title)
            continue

        detail_url = find_detail_url(
            heading,
            container,
            source["url"]
        )

        if not detail_url or detail_url in seen:
            continue

        seen.add(detail_url)

        content, image_url = extract_article(detail_url)

        if not image_url:
            image_url = extract_image(
                container,
                source["url"]
            )

        # Use the card text if the detail page is too short.
        if len(content) < MIN_ARTICLE_LENGTH:
            content = card_text

        event_date = extract_date(
            card_text + " " + content
        )

        event_location = extract_location(
            card_text + " " + content
        )

        article = build_article(
            source,
            title,
            detail_url,
            content,
            image_url,
            event_date,
            event_location
        )

        if article:
            articles.append(article)
            logger.info(
                "%s event found: %s",
                source["name"],
                title
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s extractor found %s events",
        source["name"],
        len(articles)
    )

    return articles


def collect_news():
    all_articles = []

    for source in load_sources():
        logger.info("Checking source: %s", source["name"])

        source_type = source.get("type", "web")

        try:
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

        except Exception:
            logger.exception(
                "Unexpected error collecting from %s",
                source["name"]
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
