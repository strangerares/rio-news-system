import json
import logging
import re
from datetime import date
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
    )
}

TIMEOUT = (8, 25)
MAX_ARTICLES_PER_SOURCE = 12
MIN_CONTENT_LENGTH = 45

EXCLUDED_PATHS = (
    "/category/", "/categoria/", "/tag/", "/author/",
    "/page/", "/wp-content/", "/wp-admin/", "/wp-json/",
    "/feed/", "/search/", "/privacy-policy",
    "/politica-de-privacidade", "/orgaos_municipais/",
    "/municipal-agencies/",
)

MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
    "jan": 1, "fev": 2, "mar": 3, "abr": 4,
    "mai": 5, "jun": 6, "jul": 7, "ago": 8,
    "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_url(url, base_url):
    if not url:
        return ""

    url = urljoin(base_url, str(url).strip())
    url, _ = urldefrag(url)

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return ""

    if parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
        return ""

    return url


def same_domain(url, base_url):
    first = urlparse(url).netloc.lower().removeprefix("www.")
    second = urlparse(base_url).netloc.lower().removeprefix("www.")
    return first == second


def is_excluded_url(url):
    path = urlparse(url).path.lower()
    return any(part in path for part in EXCLUDED_PATHS)


def fetch_page(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and "html" not in content_type:
            logger.warning("Not an HTML page: %s", url)
            return None

        return response.text

    except requests.RequestException as error:
        logger.warning("Could not fetch %s: %s", url, error)
        return None


def get_soup(url):
    html = fetch_page(url)
    return BeautifulSoup(html, "html.parser") if html else None


def extract_image(container, base_url):
    if not container:
        return ""

    for image in container.find_all("img"):
        candidates = [
            image.get("data-src"),
            image.get("data-lazy-src"),
            image.get("data-original"),
            image.get("data-original-src"),
            image.get("src"),
        ]

        srcset = image.get("srcset") or image.get("data-srcset")
        if srcset:
            first = srcset.split(",")[0].strip().split(" ")[0]
            candidates.insert(0, first)

        for candidate in candidates:
            if candidate and not candidate.startswith("data:"):
                result = normalize_url(candidate, base_url)
                if result:
                    return result

    return ""


def extract_page_image(soup, page_url):
    for key in ("og:image", "twitter:image"):
        tag = (
            soup.find("meta", property=key)
            or soup.find("meta", attrs={"name": key})
        )
        if tag and tag.get("content"):
            return normalize_url(tag["content"], page_url)

    return ""


def extract_location(text):
    patterns = [
        r"(?:local|venue|onde)\s*:\s*([^|.;]{3,100})",
        r"(?:endereço|endereco)\s*:\s*([^|.;]{3,100})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text or "", re.IGNORECASE)
        if match:
            return clean_text(match.group(1))

    known_venues = [
        "Qualistage", "Vivo Rio", "Circo Voador",
        "Fundição Progresso", "Fundicao Progresso",
        "Maracanã", "Maracana", "Cidade das Artes",
        "Farmasi Arena", "Jeunesse Arena",
    ]

    for venue in known_venues:
        if venue.lower() in (text or "").lower():
            return venue

    return ""


def extract_date(text):
    """
    Extract an explicitly stated date.
    Does not convert old dates into dates in the following year.
    Avoids dates close to ticket-sale or presale wording.
    """
    if not text:
        return ""

    original = clean_text(text)
    normalized = original.lower()
    today = date.today()

    sale_terms = (
        "pré-venda", "pre-venda", "pre venda",
        "venda de ingressos", "vendas de ingressos",
        "ingressos à venda", "ingressos a venda",
        "compra de ingressos", "comprar ingressos",
        "bilheteria", "abertura das vendas",
        "início das vendas", "inicio das vendas",
    )

    def is_sale_context(match):
        start = max(0, match.start() - 100)
        end = min(len(normalized), match.end() + 100)
        context = normalized[start:end]
        return any(term in context for term in sale_terms)

    # DD/MM/YYYY or DD-MM-YYYY
    for match in re.finditer(
        r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b",
        normalized,
    ):
        if is_sale_context(match):
            continue
        try:
            return date(
                int(match.group(3)),
                int(match.group(2)),
                int(match.group(1)),
            ).isoformat()
        except ValueError:
            continue

    # YYYY-MM-DD
    for match in re.finditer(r"\b20\d{2}-\d{2}-\d{2}\b", normalized):
        if is_sale_context(match):
            continue
        try:
            return date.fromisoformat(match.group()).isoformat()
        except ValueError:
            continue

    # Portuguese dates: 16 de fevereiro de 2027
    pattern = (
        r"\b(?:dia\s+)?(\d{1,2})\s+de\s+"
        r"(janeiro|fevereiro|março|marco|abril|maio|junho|"
        r"julho|agosto|setembro|outubro|novembro|dezembro)"
        r"(?:\s+de\s+(20\d{2}))?\b"
    )

    for match in re.finditer(pattern, normalized):
        if is_sale_context(match):
            continue

        # A date must be near wording that describes the event,
        # not merely a date mentioned somewhere in the article.
        start = max(0, match.start() - 90)
        end = min(len(normalized), match.end() + 90)
        context = normalized[start:end]

        event_words = (
            "no dia", "dia ", "acontece", "acontecerá",
            "acontecera", "show", "apresentação",
            "apresentacao", "realizado", "será", "sera",
            "no qualistage", "no rio de janeiro",
        )

        if not any(word in context for word in event_words):
            continue

        day_number = int(match.group(1))
        month_number = MONTHS[match.group(2)]
        year_number = int(match.group(3)) if match.group(3) else today.year

        try:
            candidate = date(year_number, month_number, day_number)
        except ValueError:
            continue

        if not match.group(3) and candidate < today:
            continue

        return candidate.isoformat()

    # Compact event-calendar format, e.g. 20 | nov
    short_pattern = (
        r"\b(\d{1,2})\s*[|/]\s*"
        r"(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\b"
    )

    for match in re.finditer(short_pattern, normalized):
        if is_sale_context(match):
            continue

        try:
            candidate = date(
                today.year,
                MONTHS[match.group(2)],
                int(match.group(1)),
            )
        except ValueError:
            continue

        if candidate >= today:
            return candidate.isoformat()

    return ""


def extract_article(url):
    soup = get_soup(url)
    if not soup:
        return "", ""

    image_url = extract_page_image(soup, url)

    # Prefer common WordPress and editorial content containers.
    selectors = [
        ".entry-content",
        ".post-content",
        ".article-content",
        ".elementor-widget-theme-post-content",
        ".content-area article",
        "article",
        "main",
    ]

    content_area = None

    for selector in selectors:
        content_area = soup.select_one(selector)
        if content_area:
            break

    if not content_area:
        content_area = soup.body

    if not content_area:
        return "", image_url

    for element in content_area.find_all([
        "script", "style", "nav", "footer", "header",
        "aside", "form", "noscript", "iframe",
    ]):
        element.decompose()

    paragraphs = []

    for element in content_area.find_all(["p", "h2", "h3", "li"]):
        text = clean_text(element.get_text(" ", strip=True))

        if len(text) >= 30 and text not in paragraphs:
            paragraphs.append(text)

    content = "\n\n".join(paragraphs)

    if len(content) < MIN_CONTENT_LENGTH:
        content = clean_text(content_area.get_text(" ", strip=True))

    return content[:15000], image_url


def build_article(
    source,
    title,
    detail_url,
    content,
    image_url="",
    event_date="",
    event_location="",
):
    title = clean_text(title)
    content = clean_text(content)

    if not title or not detail_url:
        return None

    if is_excluded_url(detail_url):
        return None

    if len(title) < 3 or len(title) > 250:
        return None

    if len(content) < MIN_CONTENT_LENGTH:
        logger.info("Skipping short item: %s", title)
        return None

    cancellation_terms = (
        "evento cancelado", "event cancelled",
        "event canceled", "show cancelado",
        "apresentação cancelada", "apresentacao cancelada",
    )

    combined = (title + " " + content).lower()
    if any(term in combined for term in cancellation_terms):
        logger.info("Skipping cancelled item: %s", title)
        return None

    return {
        "source_name": source["name"],
        "source_url": detail_url,
        "original_title": title,
        "original_content": content[:15000],
        "image_url": image_url or "",
        "image_credit": source["name"] if image_url else "",
        "event_date": event_date or extract_date(content),
        "event_location": event_location or extract_location(content),
        "category": source.get("category", "Tourism"),
    }


def extract_jsonld_events(soup, source):
    results = []

    def inspect(value):
        if isinstance(value, list):
            for item in value:
                inspect(item)
            return

        if not isinstance(value, dict):
            return

        event_type = value.get("@type", [])
        if not isinstance(event_type, list):
            event_type = [event_type]

        if any(str(item).lower() == "event" for item in event_type):
            title = clean_text(value.get("name", ""))
            description = clean_text(value.get("description", ""))

            event_url = normalize_url(
                value.get("url", ""),
                source["url"],
            ) or source["url"]

            image = value.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url", "")

            image_url = normalize_url(image, source["url"])

            start_date = clean_text(value.get("startDate", ""))
            event_date = ""

            if start_date:
                try:
                    event_date = start_date[:10]
                    date.fromisoformat(event_date)
                except ValueError:
                    event_date = ""

            location = value.get("location", "")
            if isinstance(location, dict):
                address = location.get("address", "")
                if isinstance(address, dict):
                    address = address.get("streetAddress", "")
                location = location.get("name") or address or ""

            article = build_article(
                source,
                title,
                event_url,
                description or title,
                image_url,
                event_date,
                clean_text(location),
            )

            if article:
                results.append(article)

        for key, child in value.items():
            if key not in (
                "@context", "@type", "name", "description",
                "url", "image",
            ) and isinstance(child, (dict, list)):
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


def get_card_for_heading(heading):
    """
    Find a reasonably small parent container around a heading.
    The card should contain one main heading and useful text.
    """
    current = heading

    for _ in range(7):
        if current.parent is None:
            break

        current = current.parent
        headings = current.find_all(["h1", "h2", "h3", "h4"])
        text = clean_text(current.get_text(" ", strip=True))

        if (
            len(headings) == 1
            and 35 <= len(text) <= 3500
        ):
            return current

    return None


def get_card_link(card, base_url):
    if not card:
        return ""

    links = []

    for link in card.find_all("a", href=True):
        candidate = normalize_url(link["href"], base_url)

        if not candidate or is_excluded_url(candidate):
            continue

        label = clean_text(link.get_text(" ", strip=True)).lower()

        # Avoid social media, navigation and unrelated links.
        ignored = (
            "instagram", "facebook", "youtube", "whatsapp",
            "twitter", "tiktok", "privacy", "política",
            "politica", "contato", "contact",
        )

        if any(word in label for word in ignored):
            continue

        if candidate not in links:
            links.append(candidate)

    # Prefer a same-site event/detail page.
    same_site = [link for link in links if same_domain(link, base_url)]

    if len(same_site) == 1:
        return same_site[0]

    # If no internal detail page exists, use a single ticket link.
    ticket_links = [
        link for link in links
        if any(
            domain in urlparse(link).netloc.lower()
            for domain in (
                "eventim", "ticketmaster", "livepass",
                "ingresse", "sympla",
            )
        )
    ]

    if len(ticket_links) == 1:
        return ticket_links[0]

    return ""


def collect_generic_events(source):
    soup = get_soup(source["url"])
    if not soup:
        return []

    structured = extract_jsonld_events(soup, source)
    if structured:
        logger.info(
            "%s: found %s structured events",
            source["name"], len(structured),
        )
        return structured[:MAX_ARTICLES_PER_SOURCE]

    articles = []
    seen = set()

    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        title = clean_text(heading.get_text(" ", strip=True))

        if len(title) < 3 or len(title) > 150:
            continue

        card = get_card_for_heading(heading)
        if not card:
            continue

        detail_url = get_card_link(card, source["url"])
        if not detail_url or detail_url in seen:
            continue

        seen.add(detail_url)

        card_text = clean_text(card.get_text(" ", strip=True))
        image_url = extract_image(card, source["url"])

        # Try the detail page, but keep the card text if it is useful.
        detail_content, detail_image = extract_article(detail_url)

        content = (
            detail_content
            if len(detail_content) >= len(card_text)
            else card_text
        )

        image_url = detail_image or image_url

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
                "%s: event found: %s",
                source["name"], title,
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s: generic extractor found %s events",
        source["name"], len(articles),
    )
    return articles


def collect_vivo_rio(source):
    soup = get_soup(source["url"])
    if not soup:
        return []

    articles = []
    seen = set()

    for heading in soup.find_all("h2"):
        artist = clean_text(heading.get_text(" ", strip=True))

        if len(artist) < 2 or len(artist) > 120:
            continue

        card = get_card_for_heading(heading)
        if not card:
            continue

        subtitle = ""
        for subheading in card.find_all("h3"):
            candidate = clean_text(subheading.get_text(" ", strip=True))
            if candidate and candidate.lower() != artist.lower():
                subtitle = candidate
                break

        title = f"{artist} — {subtitle}" if subtitle else artist
        detail_url = get_card_link(card, source["url"])

        if not detail_url or detail_url in seen:
            continue

        seen.add(detail_url)

        card_text = clean_text(card.get_text(" ", strip=True))
        content, image_url = extract_article(detail_url)

        if len(content) < MIN_CONTENT_LENGTH:
            content = card_text

        image_url = image_url or extract_image(card, source["url"])

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
            logger.info("Vivo Rio event found: %s", title)

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s: found %s events",
        source["name"], len(articles),
    )
    return articles


def collect_qualistage(source):
    # Keep the working Qualistage-specific approach, with a
    # generic-card fallback if its page structure changes.
    soup = get_soup(source["url"])
    if not soup:
        return []

    structured = extract_jsonld_events(soup, source)
    if structured:
        logger.info(
            "Qualistage JSON-LD found %s events",
            len(structured),
        )
        return structured[:MAX_ARTICLES_PER_SOURCE]

    articles = []
    seen = set()

    for heading in soup.find_all(["h1", "h2", "h3"]):
        title = clean_text(heading.get_text(" ", strip=True))

        if len(title) < 2 or len(title) > 150:
            continue

        card = get_card_for_heading(heading)
        if not card:
            continue

        card_text = clean_text(card.get_text(" ", strip=True))
        links = []

        for link in card.find_all("a", href=True):
            candidate = normalize_url(link["href"], source["url"])
            label = clean_text(link.get_text(" ", strip=True)).lower()

            if (
                candidate
                and candidate not in links
                and (
                    "ingresso" in label
                    or "ticket" in label
                    or "saiba mais" in label
                    or "comprar" in label
                )
            ):
                links.append(candidate)

        if len(links) != 1:
            continue

        detail_url = links[0]
        if detail_url in seen:
            continue

        seen.add(detail_url)

        image_url = extract_image(card, source["url"])
        article = build_article(
            source,
            title,
            detail_url,
            card_text,
            image_url,
            extract_date(card_text),
            extract_location(card_text),
        )

        if article:
            articles.append(article)
            logger.info("Qualistage event found: %s", title)

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "Qualistage: found %s events",
        len(articles),
    )
    return articles


def collect_city_hall(source):
    """
    Collect news links from the City Hall WordPress site.
    Retrieve the article text from the actual article page.
    """
    soup = get_soup(source["url"])
    if not soup:
        return []

    articles = []
    seen = set()

    # First try WordPress REST API discovery.
    base = source["url"].rstrip("/")
    parsed = urlparse(base)
    api_url = f"{parsed.scheme}://{parsed.netloc}/wp-json/wp/v2/posts"

    try:
        response = requests.get(
            api_url,
            params={"per_page": 20, "_embed": "1"},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        posts = response.json()

        for post in posts:
            title = clean_text(
                BeautifulSoup(
                    post.get("title", {}).get("rendered", ""),
                    "html.parser",
                ).get_text(" ", strip=True)
            )

            detail_url = normalize_url(
                post.get("link", ""),
                source["url"],
            )

            content_html = post.get("content", {}).get("rendered", "")
            content = clean_text(
                BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)
            )

            image_url = ""
            embedded = post.get("_embedded", {})
            media = embedded.get("wp:featuredmedia", [])

            if media:
                image_url = normalize_url(
                    media[0].get("source_url", ""),
                    source["url"],
                )

            if (
                not title
                or not detail_url
                or detail_url in seen
                or not same_domain(detail_url, source["url"])
            ):
                continue

            seen.add(detail_url)

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
                logger.info("City Hall news found: %s", title)

            if len(articles) >= MAX_ARTICLES_PER_SOURCE:
                break

    except (requests.RequestException, ValueError, TypeError) as error:
        logger.info(
            "WordPress API unavailable; using page links instead: %s",
            error,
        )

    # Fallback: discover article links on the public news page.
    if not articles:
        for link in soup.find_all("a", href=True):
            title = clean_text(link.get_text(" ", strip=True))

            if len(title) < 30 or len(title) > 250:
                continue

            detail_url = normalize_url(link["href"], source["url"])

            if (
                not detail_url
                or not same_domain(detail_url, source["url"])
                or is_excluded_url(detail_url)
                or detail_url in seen
                or detail_url.rstrip("/") == source["url"].rstrip("/")
            ):
                continue

            seen.add(detail_url)

            content, image_url = extract_article(detail_url)

            if len(content) < MIN_CONTENT_LENGTH:
                continue

            detail_soup = get_soup(detail_url)
            if detail_soup:
                heading = detail_soup.find("h1")
                if heading:
                    page_title = clean_text(heading.get_text(" ", strip=True))
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
                logger.info("City Hall news found: %s", title)

            if len(articles) >= MAX_ARTICLES_PER_SOURCE:
                break

    logger.info(
        "%s: found %s articles",
        source["name"], len(articles),
    )
    return articles


def collect_event_source(source):
    name = source["name"].lower()

    if "vivo rio" in name:
        return collect_vivo_rio(source)

    if "qualistage" in name:
        return collect_qualistage(source)

    return collect_generic_events(source)


def load_sources():
    with open("sources.json", "r", encoding="utf-8") as file:
        return json.load(file)["sources"]


def collect_news():
    all_articles = []

    for source in load_sources():
        logger.info("=" * 60)
        logger.info("Checking source: %s", source["name"])

        try:
            source_type = source.get("type", "web")

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
            source["name"], len(articles),
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
