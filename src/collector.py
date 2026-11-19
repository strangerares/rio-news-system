
import json
import logging
import re
from datetime import date, datetime
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

MAX_ARTICLES_PER_SOURCE = 12
TIMEOUT = (5, 20)
MIN_ARTICLE_LENGTH = 70

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
    "/orgaos_municipais/",
    "/municipal-agencies/",
    "/politica-de-privacidade",
    "/privacy-policy",
]

PORTUGUESE_MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "março": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


def load_sources():
    with open("sources.json", "r", encoding="utf-8") as file:
        return json.load(file)["sources"]


def fetch_page(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

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
    first = urlparse(url).netloc.lower().removeprefix("www.")
    second = urlparse(base_url).netloc.lower().removeprefix("www.")
    return first == second


def is_excluded_url(url):
    path = urlparse(url).path.lower()
    return any(item in path for item in EXCLUDED_PATHS)


def is_cancelled(text):
    normalized = clean_text(text).lower()
    return any(term in normalized for term in CANCELLED_TERMS)



def extract_date(text):
    """Extract a clearly identified event date, never a ticket-sale date."""

    if not text:
        return ""

    text = re.sub(r"\s+", " ", text).strip().lower()

    today = datetime.now().date()

    months = {
        "janeiro": 1,
        "fevereiro": 2,
        "março": 3,
        "marco": 3,
        "abril": 4,
        "maio": 5,
        "junho": 6,
        "julho": 7,
        "agosto": 8,
        "setembro": 9,
        "outubro": 10,
        "novembro": 11,
        "dezembro": 12,
    }

    sale_terms = (
        "pré-venda",
        "pre-venda",
        "pre venda",
        "venda de ingressos",
        "vendas de ingressos",
        "ingressos à venda",
        "ingressos a venda",
        "compra de ingressos",
        "comprar ingressos",
        "bilheteria",
        "abertura das vendas",
        "início das vendas",
        "inicio das vendas",
    )

    event_terms = (
        "no dia",
        "dia ",
        "acontece em",
        "acontecerá em",
        "acontecera em",
        "será realizado em",
        "sera realizado em",
        "apresentação será",
        "apresentacao sera",
        "show será",
        "show sera",
        "shows acontecem",
    )

    pattern = (
        r"\b(?:dia\s+)?(\d{1,2})\s+de\s+"
        r"(janeiro|fevereiro|março|marco|abril|maio|"
        r"junho|julho|agosto|setembro|outubro|novembro|dezembro)"
        r"(?:\s+de\s+(\d{4}))?\b"
    )

    for match in re.finditer(pattern, text):
        day = int(match.group(1))
        month_name = match.group(2)
        year_text = match.group(3)

        month = months[month_name]

        start = max(0, match.start() - 90)
        end = min(len(text), match.end() + 90)
        context = text[start:end]

        # Never interpret a ticket-sale date as an event date.
        if any(term in context for term in sale_terms):
            continue

        # Require language indicating an actual event date.
        if not any(term in context for term in event_terms):
            continue

        if year_text:
            year = int(year_text)
        else:
            year = today.year

        try:
            event_date = datetime(year, month, day).date()
        except ValueError:
            continue

        # If the date has no explicit year and has already passed,
        # do not automatically assume it belongs to next year.
        if not year_text and event_date < today:
            continue

        return event_date.isoformat()

    return ""

def extract_location(text):
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
    if not container:
        return ""

    image = container.find("img")

    if not image:
        return ""

    image_url = (
        image.get("data-src")
        or image.get("data-lazy-src")
        or image.get("data-original")
        or image.get("src")
    )

    if not image_url:
        return ""

    if image_url.startswith("data:"):
        return ""

    return normalize_url(image_url, page_url)


def extract_page_image(soup, page_url):
    for property_name in ("og:image", "twitter:image"):
        image_tag = soup.find(
            "meta",
            attrs={"property": property_name},
        ) or soup.find(
            "meta",
            attrs={"name": property_name},
        )

        if image_tag and image_tag.get("content"):
            return normalize_url(
                image_tag["content"], page_url
            )

    return ""


def extract_article(url):
    html = fetch_page(url)

    if not html:
        return "", ""

    soup = BeautifulSoup(html, "html.parser")
    image_url = extract_page_image(soup, url)

    for element in soup([
        "script", "style", "nav", "footer",
        "header", "aside", "form", "noscript",
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
        for element in article.find_all(
            ["p", "h2", "h3", "li"]
        )
    ]

    content = "\n\n".join(
        paragraph
        for paragraph in paragraphs
        if len(paragraph) > 35
    )

    return content[:15000], image_url


def extract_jsonld_events(soup, source):
    """
    Read structured Event data when a website provides it.
    This is a safe fallback for sites with JavaScript-heavy pages.
    """

    results = []

    def inspect(value):
        if isinstance(value, list):
            for item in value:
                inspect(item)
            return

        if not isinstance(value, dict):
            return

        event_type = value.get("@type", "")
        types = event_type if isinstance(event_type, list) else [event_type]

        if any(
            str(item).lower() == "event"
            for item in types
        ):
            title = clean_text(value.get("name", ""))
            description = clean_text(
                value.get("description", "")
            )

            event_url = normalize_url(
                value.get("url", ""),
                source["url"],
            )

            image = value.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url", "")

            image_url = normalize_url(image, source["url"])

            start_date = clean_text(
                value.get("startDate", "")
            )
            event_date = extract_date(start_date)

            location = value.get("location", "")
            if isinstance(location, dict):
                location = (
                    location.get("name")
                    or location.get("address")
                    or ""
                )
            if isinstance(location, dict):
                location = clean_text(
                    str(location)
                )

            content = description or title

            article = build_article(
                source,
                title,
                event_url,
                content,
                image_url,
                event_date,
                clean_text(str(location)),
            )

            if article:
                results.append(article)

        for key, child in value.items():
            if key not in (
                "@context", "@type", "name",
                "description", "url", "image",
            ):
                if isinstance(child, (dict, list)):
                    inspect(child)

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"},
    ):
        raw = script.string or script.get_text()

        try:
            inspect(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue

    return results


def build_article(
    source,
    title,
    detail_url,
    content,
    image_url="",
    event_date="",
    event_location="",
):
    if not title or not detail_url:
        return None

    if is_excluded_url(detail_url):
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
        "original_title": clean_text(title),
        "original_content": content[:15000],
        "image_url": image_url,
        "image_credit": source["name"] if image_url else "",
        "event_date": event_date or extract_date(content),
        "event_location": (
            event_location or extract_location(content)
        ),
        "category": source.get("category", "Tourism"),
    }


def get_event_card(heading):
    """
    Find the nearest parent that contains exactly one h2 event.
    This prevents a heading from being matched to a neighboring event.
    """

    current = heading

    for _ in range(7):
        if current.parent is None:
            break

        current = current.parent

        headings = current.find_all("h2")
        if len(headings) != 1:
            continue

        text = clean_text(current.get_text(" ", strip=True))

        if 25 <= len(text) <= 3500:
            return current

    return None


def get_card_detail_link(card, base_url, link_texts=None):
    if not card:
        return ""

    link_texts = link_texts or [
        "saiba mais",
        "mais informações",
        "mais informacoes",
        "learn more",
        "ver evento",
        "detalhes",
    ]

    matches = []

    for link in card.find_all("a", href=True):
        label = clean_text(
            link.get_text(" ", strip=True)
        ).lower()

        if not any(term in label for term in link_texts):
            continue

        candidate = normalize_url(
            link["href"], base_url
        )

        if (
            candidate
            and same_domain(candidate, base_url)
            and not is_excluded_url(candidate)
        ):
            if candidate not in matches:
                matches.append(candidate)

    # Never guess when the card contains multiple detail links.
    if len(matches) == 1:
        return matches[0]

    return ""


def collect_vivo_rio(source):
    """
    Vivo Rio uses the artist as h2 and the show subtitle as h3.
    Both belong to one event card.
    """

    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    headings = soup.find_all("h2")

    for heading in headings:
        artist = clean_text(
            heading.get_text(" ", strip=True)
        )

        if len(artist) < 2 or len(artist) > 120:
            continue

        card = get_event_card(heading)

        if not card:
            continue

        card_headings = card.find_all("h2")
        if len(card_headings) != 1:
            continue

        subtitle = ""

        for subheading in card.find_all("h3"):
            candidate = clean_text(
                subheading.get_text(" ", strip=True)
            )

            if candidate and candidate.lower() != artist.lower():
                subtitle = candidate
                break

        title = artist
        if subtitle:
            title = f"{artist} — {subtitle}"

        card_text = clean_text(
            card.get_text(" ", strip=True)
        )

        detail_url = get_card_detail_link(
            card,
            source["url"],
        )

        if not detail_url or detail_url in seen:
            continue

        seen.add(detail_url)

        content, image_url = extract_article(detail_url)

        if len(content) < MIN_ARTICLE_LENGTH:
            content = card_text

        if not image_url:
            image_url = extract_image(
                card, source["url"]
            )

        article = build_article(
            source,
            title,
            detail_url,
            content,
            image_url,
            extract_date(card_text + " " + content),
            extract_location(card_text + " " + content),
        )

        if article:
            articles.append(article)
            logger.info(
                "Vivo Rio event found: %s",
                title,
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s extractor found %s events",
        source["name"],
        len(articles),
    )

    return articles


def collect_city_hall(source):
    """
    Find actual news links rather than treating navigation headings
    such as 'The City Hall' as news articles.
    """

    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    for link in soup.find_all("a", href=True):
        title = clean_text(
            link.get_text(" ", strip=True)
        )

        if len(title) < 30 or len(title) > 250:
            continue

        detail_url = normalize_url(
            link["href"], source["url"]
        )

        if (
            not detail_url
            or not same_domain(detail_url, source["url"])
            or is_excluded_url(detail_url)
            or detail_url.rstrip("/") == source["url"].rstrip("/")
            or detail_url in seen
        ):
            continue

        # City Hall news articles use individual article pages.
        path = urlparse(detail_url).path.strip("/")

        if not path or len(path.split("/")) < 1:
            continue

        seen.add(detail_url)

        content, image_url = extract_article(detail_url)

        if len(content) < MIN_ARTICLE_LENGTH:
            logger.info(
                "Skipping City Hall link with insufficient content: %s",
                title,
            )
            continue

        # Prefer the actual article heading over the archive link label.
        detail_html = fetch_page(detail_url)
        if detail_html:
            detail_soup = BeautifulSoup(
                detail_html, "html.parser"
            )
            page_heading = detail_soup.find("h1")

            if page_heading:
                page_title = clean_text(
                    page_heading.get_text(" ", strip=True)
                )
                if len(page_title) >= 12:
                    title = page_title

        article = build_article(
            source,
            title,
            detail_url,
            content,
            image_url,
            extract_date(content),
            extract_location(content),
        )

        if article:
            articles.append(article)
            logger.info(
                "City Hall news found: %s",
                title,
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s extractor found %s articles",
        source["name"],
        len(articles),
    )

    return articles


def collect_fundicao(source):
    """
    Fundição Progresso event cards use 'SAIBA MAIS' links.
    Only accepts a link when its nearby card has one clear event title.
    """

    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    for link in soup.find_all("a", href=True):
        label = clean_text(
            link.get_text(" ", strip=True)
        ).lower()

        if "saiba mais" not in label:
            continue

        detail_url = normalize_url(
            link["href"], source["url"]
        )

        if (
            not detail_url
            or not same_domain(detail_url, source["url"])
            or is_excluded_url(detail_url)
            or detail_url in seen
        ):
            continue

        card = link
        for _ in range(6):
            if card.parent is None:
                break

            card = card.parent
            text = clean_text(
                card.get_text(" ", strip=True)
            )
            headings = card.find_all(
                ["h1", "h2", "h3", "h4"]
            )

            if (
                headings
                and 30 <= len(text) <= 1800
                and len(card.find_all("a", href=True)) <= 5
            ):
                break

        headings = card.find_all(
            ["h1", "h2", "h3", "h4"]
        )

        if not headings:
            logger.info(
                "Fundição card has no clear title; skipping"
            )
            continue

        title = clean_text(
            headings[0].get_text(" ", strip=True)
        )

        if len(title) < 3:
            continue

        seen.add(detail_url)

        content, image_url = extract_article(detail_url)
        card_text = clean_text(
            card.get_text(" ", strip=True)
        )

        if len(content) < MIN_ARTICLE_LENGTH:
            content = card_text

        if not image_url:
            image_url = extract_image(
                card, source["url"]
            )

        article = build_article(
            source,
            title,
            detail_url,
            content,
            image_url,
            extract_date(card_text + " " + content),
            extract_location(card_text + " " + content),
        )

        if article:
            articles.append(article)
            logger.info(
                "Fundição event found: %s",
                title,
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s extractor found %s events",
        source["name"],
        len(articles),
    )

    return articles


def collect_qualistage(source):
    """
    Qualistage displays event titles and descriptions on its events page.
    Prefer a unique event-specific ticket/detail URL from each section.
    """

    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    structured = extract_jsonld_events(soup, source)

    if structured:
        logger.info(
            "Qualistage JSON-LD found %s events",
            len(structured),
        )
        return structured[:MAX_ARTICLES_PER_SOURCE]

    articles = []
    seen = set()

    for heading in soup.find_all("h1"):
        title = clean_text(
            heading.get_text(" ", strip=True)
        )

        if len(title) < 2 or len(title) > 150:
            continue

        card = heading
        for _ in range(6):
            if card.parent is None:
                break

            card = card.parent

            headings = card.find_all("h1")
            text = clean_text(
                card.get_text(" ", strip=True)
            )

            if len(headings) == 1 and 100 <= len(text) <= 12000:
                break

        if len(card.find_all("h1")) != 1:
            continue

        card_text = clean_text(
            card.get_text(" ", strip=True)
        )

        links = []

        for link in card.find_all("a", href=True):
            candidate = normalize_url(
                link["href"], source["url"]
            )

            if not candidate or candidate in links:
                continue

            label = clean_text(
                link.get_text(" ", strip=True)
            ).lower()

            if (
                "ingresso" in label
                or "ticket" in label
                or "saiba mais" in label
            ):
                links.append(candidate)

        # A separate event link is required.
        if len(links) != 1:
            continue

        detail_url = links[0]

        if detail_url in seen:
            continue

        seen.add(detail_url)

        content = card_text
        image_url = extract_image(
            card, source["url"]
        )

        article = build_article(
            source,
            title,
            detail_url,
            content,
            image_url,
            extract_date(card_text),
            extract_location(card_text),
        )

        if article:
            articles.append(article)
            logger.info(
                "Qualistage event found: %s",
                title,
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s extractor found %s events",
        source["name"],
        len(articles),
    )

    return articles


def collect_generic_events(source):
    """
    Conservative fallback for event sites.
    Uses structured Event data or clearly linked event cards.
    """

    html = fetch_page(source["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    structured = extract_jsonld_events(soup, source)

    if structured:
        logger.info(
            "%s JSON-LD found %s events",
            source["name"],
            len(structured),
        )
        return structured[:MAX_ARTICLES_PER_SOURCE]

    logger.info(
        "%s: no structured Event data found; "
        "checking clearly linked event cards",
        source["name"],
    )

    articles = []
    seen = set()

    for heading in soup.find_all(
        ["h1", "h2", "h3"]
    ):
        title = clean_text(
            heading.get_text(" ", strip=True)
        )

        if len(title) < 3 or len(title) > 150:
            continue

        current = heading
        card = None

        for _ in range(5):
            if current.parent is None:
                break

            current = current.parent

            text = clean_text(
                current.get_text(" ", strip=True)
            )

            headings = current.find_all(
                ["h1", "h2", "h3"]
            )

            if (
                len(headings) == 1
                and 50 <= len(text) <= 2500
            ):
                card = current
                break

        if not card:
            continue

        detail_url = get_card_detail_link(
            card, source["url"]
        )

        if not detail_url or detail_url in seen:
            continue

        seen.add(detail_url)

        content, image_url = extract_article(
            detail_url
        )

        card_text = clean_text(
            card.get_text(" ", strip=True)
        )

        if len(content) < MIN_ARTICLE_LENGTH:
            content = card_text

        if not image_url:
            image_url = extract_image(
                card, source["url"]
            )

        article = build_article(
            source,
            title,
            detail_url,
            content,
            image_url,
            extract_date(card_text + " " + content),
            extract_location(card_text + " " + content),
        )

        if article:
            articles.append(article)
            logger.info(
                "%s event found: %s",
                source["name"],
                title,
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s generic extractor found %s events",
        source["name"],
        len(articles),
    )

    return articles


def collect_event_source(source):
    name = source["name"].lower()

    if "vivo rio" in name:
        return collect_vivo_rio(source)

    if "fundição" in name or "fundicao" in name:
        return collect_fundicao(source)

    if "qualistage" in name:
        return collect_qualistage(source)

    return collect_generic_events(source)


def collect_news():
    all_articles = []

    for source in load_sources():
        logger.info("=" * 60)
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
                    source_type,
                )
                articles = []

        except Exception:
            logger.exception(
                "Unexpected error collecting from %s",
                source["name"],
            )
            articles = []

        logger.info(
            "%s: collected %s items",
            source["name"],
            len(articles),
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

    logger.info("=" * 60)
    logger.info(
        "Collected %s unique articles and events",
        len(unique_articles),
    )

    return unique_articles


if __name__ == "__main__":
    articles = collect_news()

    print(
        json.dumps(
            articles,
            ensure_ascii=False,
            indent=2,
        )
    )
