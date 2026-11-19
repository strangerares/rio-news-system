import json
import logging
import re
import time
import urllib.robotparser

from datetime import date
from pathlib import Path
from urllib.parse import (
    urljoin,
    urlparse,
    urldefrag,
    urlunparse,
    parse_qsl,
    urlencode,
)

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from urllib3.util.retry import Retry

try:
    import trafilatura
except ImportError:
    trafilatura = None


# ---------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCES_FILE = BASE_DIR / "sources.json"

# The browser UA is used for actual HTTP requests, but robots.txt
# matching uses a distinct bot identity. RobotFileParser splits the
# UA on "/" and lowercases the first token, so passing the browser UA
# would make every check match the "mozilla" rule instead of our bot.
ROBOTS_UA = "RioNewsCollector"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36 "
        f"{ROBOTS_UA}/1.0"
    )
}

TIMEOUT = (8, 25)
MAX_ARTICLES_PER_SOURCE = 12
MIN_CONTENT_LENGTH = 45
REQUEST_DELAY = 0.8

EXCLUDED_PATHS = (
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
    "/privacy-policy",
    "/politica-de-privacidade",
    "/orgaos_municipais/",
    "/municipal-agencies/",
)

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
    "_ga",
    "_gl",
}

MONTHS = {
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
    "jan": 1,
    "fev": 2,
    "mar": 3,
    "abr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "set": 9,
    "out": 10,
    "nov": 11,
    "dez": 12,
}

# ---------------------------------------------------------
# HTTP SESSION, RETRIES, ROBOTS AND RATE LIMITING
# ---------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

retry_strategy = Retry(
    total=3,
    connect=3,
    read=2,
    status=3,
    backoff_factor=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods={"GET"},
    respect_retry_after_header=True,
)

adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=10,
    pool_maxsize=10,
)

SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)

ROBOTS_CACHE = {}
LAST_REQUEST_TIME = {}


def host_key(url):
    return urlparse(url).netloc.lower()


def wait_for_host(url):
    host = host_key(url)
    now = time.monotonic()

    previous = LAST_REQUEST_TIME.get(host)

    if previous is not None:
        elapsed = now - previous

        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)

    LAST_REQUEST_TIME[host] = time.monotonic()


def robots_allowed(url):
    """
    Check robots.txt before accessing a page.

    Follows RFC 9309:
      - 2xx                    -> parse and honour
      - 4xx except 429         -> unavailable, therefore allowed
      - 429 / 5xx / net errors -> unreachable, therefore disallowed
    """

    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if host in ROBOTS_CACHE:
        parser, allowed = ROBOTS_CACHE[host]

        if parser is None:
            return allowed

        return parser.can_fetch(ROBOTS_UA, url)

    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    try:
        wait_for_host(robots_url)

        response = SESSION.get(
            robots_url,
            timeout=TIMEOUT,
        )

        status = response.status_code

        if 200 <= status < 300:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())

            ROBOTS_CACHE[host] = (parser, True)

            return parser.can_fetch(ROBOTS_UA, url)

        if 400 <= status < 500 and status != 429:
            # Unavailable -> treat as no restrictions.
            ROBOTS_CACHE[host] = (None, True)
            return True

        # 429 and 5xx -> unreachable, be conservative.
        ROBOTS_CACHE[host] = (None, False)
        logger.warning(
            "Could not verify robots.txt for %s (status %s)",
            host,
            status,
        )
        return False

    except requests.RequestException as error:
        logger.warning(
            "Could not check robots.txt for %s: %s",
            host,
            error,
        )
        ROBOTS_CACHE[host] = (None, False)
        return False


# ---------------------------------------------------------
# TEXT AND URL HELPERS
# ---------------------------------------------------------

def clean_text(value):
    return re.sub(
        r"\s+",
        " ",
        str(value or ""),
    ).strip()


def clean_content(value):
    """
    Clean content without destroying paragraph structure.
    """

    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n\n", text)

    return text.strip()


def normalize_url(url, base_url):
    if not url:
        return ""

    url = urljoin(base_url, str(url).strip())
    url, _ = urldefrag(url)

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return ""

    if parsed.path.lower().endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")
    ):
        return ""

    return url


def canonical_url(url):
    """
    Normalize a URL for duplicate detection.

    Trailing slashes are ignored. Tracking-only query parameters
    are dropped, but other query parameters are preserved, since
    many ticket/event sites use them to identify distinct events.
    """

    parsed = urlparse(url)

    path = parsed.path.rstrip("/") or "/"

    kept = [
        (key, value)
        for key, value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if key.lower() not in TRACKING_PARAMS
    ]

    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        "",
        urlencode(kept),
        "",
    ))


def same_domain(url, base_url):
    first = urlparse(url).netloc.lower().removeprefix("www.")
    second = urlparse(base_url).netloc.lower().removeprefix("www.")

    return first == second


def is_excluded_url(url):
    path = urlparse(url).path.lower()

    return any(part in path for part in EXCLUDED_PATHS)


# ---------------------------------------------------------
# HTTP AND HTML FETCHING
# ---------------------------------------------------------

def fetch_page(url):
    if not robots_allowed(url):
        logger.info("Skipping disallowed page: %s", url)
        return None

    try:
        wait_for_host(url)

        response = SESSION.get(
            url,
            timeout=TIMEOUT,
        )

        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        if content_type and "html" not in content_type:
            logger.warning("Not an HTML page: %s", url)
            return None

        if (
            response.encoding is None
            or response.encoding.lower() == "iso-8859-1"
        ):
            response.encoding = response.apparent_encoding

        return response.text

    except requests.RequestException as error:
        logger.warning(
            "Could not fetch %s: %s",
            url,
            error,
        )
        return None


def parse_html(html):
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def get_soup(url):
    html = fetch_page(url)

    if not html:
        return None

    return parse_html(html)


# ---------------------------------------------------------
# IMAGE EXTRACTION
# ---------------------------------------------------------

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

        srcset = (
            image.get("srcset")
            or image.get("data-srcset")
        )

        if srcset:
            entries = []

            for entry in srcset.split(","):
                parts = entry.strip().split()

                if not parts:
                    continue

                width = 0

                if len(parts) > 1:
                    descriptor = parts[1].lower()

                    try:
                        if descriptor.endswith("w"):
                            width = int(descriptor[:-1])
                        elif descriptor.endswith("x"):
                            width = int(
                                float(descriptor[:-1]) * 1000
                            )
                    except ValueError:
                        width = 0

                entries.append((width, parts[0]))

            entries.sort(key=lambda item: item[0])

            if entries:
                candidates.insert(
                    0,
                    entries[-1][1],
                )

        for candidate in candidates:
            if candidate and not candidate.startswith("data:"):
                result = normalize_url(candidate, base_url)

                if result:
                    return result

    return ""


def extract_page_image(soup, page_url):
    if not soup:
        return ""

    for key in ("og:image", "twitter:image"):
        tag = (
            soup.find("meta", property=key)
            or soup.find("meta", attrs={"name": key})
        )

        if tag and tag.get("content"):
            return normalize_url(
                tag["content"],
                page_url,
            )

    return ""


# ---------------------------------------------------------
# LOCATION AND DATE EXTRACTION
# ---------------------------------------------------------

def extract_location(text):
    patterns = [
        r"(?:local|venue|onde)\s*:\s*([^|.;]{3,100})",
        r"(?:endereço|endereco)\s*:\s*([^|.;]{3,100})",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text or "",
            re.IGNORECASE,
        )

        if match:
            return clean_text(match.group(1))

    known_venues = [
        "Qualistage",
        "Vivo Rio",
        "Circo Voador",
        "Fundição Progresso",
        "Fundicao Progresso",
        "Maracanã",
        "Maracana",
        "Cidade das Artes",
        "Farmasi Arena",
        "Jeunesse Arena",
    ]

    for venue in known_venues:
        if venue.lower() in (text or "").lower():
            return venue

    return ""


def extract_date(text, event_calendar=False):
    """
    Extract an explicitly stated date.

    Normal article dates are not automatically moved
    into the following year.

    Compact calendar dates such as '20 | jan' can roll
    into the next year when event_calendar=True.
    """

    if not text:
        return ""

    normalized = clean_text(text).lower()
    today = date.today()

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

    def is_sale_context(match):
        start = max(0, match.start() - 100)
        end = min(len(normalized), match.end() + 100)

        context = normalized[start:end]

        return any(
            term in context
            for term in sale_terms
        )

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
    for match in re.finditer(
        r"\b20\d{2}-\d{2}-\d{2}\b",
        normalized,
    ):
        if is_sale_context(match):
            continue

        try:
            return date.fromisoformat(
                match.group()
            ).isoformat()

        except ValueError:
            continue

    # Portuguese dates, e.g. 16 de fevereiro de 2027
    pattern = (
        r"\b(?:dia\s+)?(\d{1,2})\s+de\s+"
        r"(janeiro|fevereiro|março|marco|abril|maio|junho|"
        r"julho|agosto|setembro|outubro|novembro|dezembro)"
        r"(?:\s+de\s+(20\d{2}))?\b"
    )

    for match in re.finditer(pattern, normalized):
        if is_sale_context(match):
            continue

        start = max(0, match.start() - 90)
        end = min(len(normalized), match.end() + 90)

        context = normalized[start:end]

        event_words = (
            "no dia",
            "dia ",
            "acontece",
            "acontecerá",
            "acontecera",
            "show",
            "apresentação",
            "apresentacao",
            "realizado",
            "será",
            "sera",
            "no qualistage",
            "no rio de janeiro",
        )

        if not any(word in context for word in event_words):
            continue

        day_number = int(match.group(1))
        month_number = MONTHS[match.group(2)]

        year_number = (
            int(match.group(3))
            if match.group(3)
            else today.year
        )

        try:
            candidate = date(
                year_number,
                month_number,
                day_number,
            )

        except ValueError:
            continue

        if not match.group(3) and candidate < today:
            continue

        return candidate.isoformat()

    # Compact calendar format: 20 | jan
    short_pattern = (
        r"\b(\d{1,2})\s*[|/]\s*"
        r"(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\b"
    )

    for match in re.finditer(short_pattern, normalized):
        if is_sale_context(match):
            continue

        month_number = MONTHS[match.group(2)]
        day_number = int(match.group(1))

        try:
            candidate = date(
                today.year,
                month_number,
                day_number,
            )

        except ValueError:
            continue

        if candidate < today:
            days_old = (today - candidate).days

            if event_calendar and days_old > 30:
                try:
                    candidate = date(
                        today.year + 1,
                        month_number,
                        day_number,
                    )
                except ValueError:
                    continue
            else:
                continue

        return candidate.isoformat()

    return ""


# ---------------------------------------------------------
# ARTICLE EXTRACTION
# ---------------------------------------------------------

def extract_article_from_soup(soup, page_url):
    """
    Extract article text and image from an already-fetched page.
    """

    if not soup:
        return "", ""

    image_url = extract_page_image(soup, page_url)

    # First try Trafilatura.
    if trafilatura:
        try:
            extracted = trafilatura.extract(
                str(soup),
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )

            extracted = clean_content(extracted)

            if len(extracted) >= MIN_CONTENT_LENGTH:
                return extracted[:15000], image_url

        except Exception as error:
            logger.debug(
                "Trafilatura extraction failed: %s",
                error,
            )

    # Fallback: common WordPress/editorial containers.
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
        # Fragments parsed without a surrounding document may have
        # no <body>; fall back to the soup itself so we never lose
        # content when lxml is unavailable.
        content_area = soup.body or soup

    if not content_area:
        return "", image_url

    content_area = parse_html(str(content_area))

    for element in content_area.find_all([
        "script",
        "style",
        "nav",
        "footer",
        "header",
        "aside",
        "form",
        "noscript",
        "iframe",
    ]):
        element.decompose()

    paragraphs = []

    for element in content_area.find_all([
        "p",
        "h2",
        "h3",
        "li",
    ]):
        text = clean_text(
            element.get_text(" ", strip=True)
        )

        if len(text) >= 30 and text not in paragraphs:
            paragraphs.append(text)

    content = "\n\n".join(paragraphs)

    if len(content) < MIN_CONTENT_LENGTH:
        content = clean_content(
            content_area.get_text("\n", strip=True)
        )

    return content[:15000], image_url


def extract_article(url):
    soup = get_soup(url)

    if not soup:
        return "", "", None

    content, image_url = extract_article_from_soup(
        soup,
        url,
    )

    return content, image_url, soup


# ---------------------------------------------------------
# ARTICLE VALIDATION AND CREATION
# ---------------------------------------------------------

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
    content = clean_content(content)

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
        "evento cancelado",
        "event cancelled",
        "event canceled",
        "show cancelado",
        "apresentação cancelada",
        "apresentacao cancelada",
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
        "event_location": (
            event_location
            or extract_location(content)
        ),
        "category": source.get("category", "Tourism"),
    }


# ---------------------------------------------------------
# JSON-LD STRUCTURED EVENT EXTRACTION
# ---------------------------------------------------------

def extract_jsonld_events(soup, source):
    results = []
    seen = set()

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

        is_event = any(
            str(item).lower() == "event"
            for item in event_type
        )

        if is_event:
            title = clean_text(value.get("name", ""))
            description = clean_content(
                value.get("description", "")
            )

            # An event must have its own URL. Using the listing page
            # for every URL-less event would make unrelated events look
            # like duplicates and send visitors to the wrong destination.
            raw_event_url = value.get("url") or value.get("@id") or ""
            event_url = normalize_url(
                raw_event_url,
                source["url"],
            )

            if not event_url or canonical_url(event_url) == canonical_url(source["url"]):
                logger.info(
                    "Skipping structured event without its own URL: %s",
                    title or "(untitled)",
                )
                event_url = ""

            image = value.get("image", "")

            if isinstance(image, list):
                image = image[0] if image else ""

            if isinstance(image, dict):
                image = image.get("url", "")

            image_url = normalize_url(
                image,
                source["url"],
            )

            start_date = clean_text(
                value.get("startDate", "")
            )

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
                    address = (
                        address.get("streetAddress", "")
                        or address.get("addressLocality", "")
                    )

                location = (
                    location.get("name")
                    or address
                    or ""
                )

            # If the structured description is missing,
            # try to retrieve the actual event page.
            if len(description) < MIN_CONTENT_LENGTH:
                if (
                    event_url != source["url"]
                    and same_domain(event_url, source["url"])
                ):
                    detail_content, detail_image, _ = (
                        extract_article(event_url)
                    )

                    if len(detail_content) > len(description):
                        description = detail_content

                    image_url = image_url or detail_image

            if not event_url:
                # No event-specific destination: do not create a misleading record.
                pass
            else:
                article = build_article(
                    source,
                    title,
                    event_url,
                    description,
                    image_url,
                    event_date,
                    clean_text(location),
                )

                if article:
                    dedup_key = canonical_url(
                        article["source_url"]
                    )

                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        results.append(article)

        for child_key, child in value.items():
            if child_key not in (
                "@context",
                "@type",
                "name",
                "description",
                "url",
                "image",
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


# ---------------------------------------------------------
# EVENT CARD IDENTIFICATION
# ---------------------------------------------------------

def get_card_for_heading(
    heading,
    max_headings=3,
    min_text=35,
    max_text=3500,
):
    """
    Find the smallest sensible parent container around a heading
    that also contains a usable link.

    Multiple headings are allowed so cards containing an H2
    artist name and an H3 subtitle can still be identified.

    If no ancestor within the search window contains a link,
    the largest qualifying container is returned so the caller
    can still use it (e.g. for title, image, or content).
    """

    current = heading
    candidates = []

    for _ in range(9):
        if current.parent is None:
            break

        current = current.parent

        headings = current.find_all([
            "h1",
            "h2",
            "h3",
            "h4",
        ])

        text = clean_text(
            current.get_text(" ", strip=True)
        )

        if (
            1 <= len(headings) <= max_headings
            and min_text <= len(text) <= max_text
        ):
            candidates.append(current)

    if not candidates:
        return None

    # Prefer the smallest candidate that carries a link.
    for candidate in candidates:
        if candidate.find("a", href=True):
            return candidate

    # Otherwise fall back to the largest candidate, which gives
    # the caller as much surrounding context as possible.
    return candidates[-1]


def get_card_link(card, base_url, title=""):
    if not card:
        return ""

    candidates = []

    ignored = (
        "instagram",
        "facebook",
        "youtube",
        "whatsapp",
        "twitter",
        "tiktok",
        "privacy",
        "política",
        "politica",
        "contato",
        "contact",
    )

    ticket_domains = (
        "eventim",
        "ticketmaster",
        "livepass",
        "ingresse",
        "sympla",
    )

    for link in card.find_all("a", href=True):
        candidate = normalize_url(
            link["href"],
            base_url,
        )

        if not candidate or is_excluded_url(candidate):
            continue

        label = clean_text(
            link.get_text(" ", strip=True)
        ).lower()

        if any(word in label for word in ignored):
            continue

        candidates.append({
            "url": candidate,
            "canonical": canonical_url(candidate),
            "label": label,
            "same_site": same_domain(candidate, base_url),
            "ticket": any(
                domain in urlparse(candidate).netloc.lower()
                for domain in ticket_domains
            ),
        })

    # Remove duplicate links, including query-string variations.
    unique = {}

    for item in candidates:
        key = item["canonical"]

        if key not in unique:
            unique[key] = item

    candidates = list(unique.values())

    if not candidates:
        return ""

    title_lower = clean_text(title).lower()

    # Prefer a link whose label contains the event title.
    if title_lower:
        matching = [
            item for item in candidates
            if item["label"]
            and (
                title_lower in item["label"]
                or item["label"] in title_lower
            )
        ]

        if matching:
            candidates = matching

    same_site = [
        item for item in candidates
        if item["same_site"]
    ]

    if same_site:
        # Prefer the shortest relevant internal URL.
        same_site.sort(
            key=lambda item: len(
                urlparse(item["url"]).path
            )
        )

        return same_site[0]["url"]

    ticket_links = [
        item for item in candidates
        if item["ticket"]
    ]

    if len(ticket_links) == 1:
        return ticket_links[0]["url"]

    if len(candidates) == 1:
        return candidates[0]["url"]

    return ""


# ---------------------------------------------------------
# GENERIC EVENT CARD COLLECTOR
# ---------------------------------------------------------

def collect_generic_events(source):
    soup = get_soup(source["url"])

    if not soup:
        return []

    structured = extract_jsonld_events(soup, source)

    if structured:
        logger.info(
            "%s: found %s structured events",
            source["name"],
            len(structured),
        )
        return structured[:MAX_ARTICLES_PER_SOURCE]

    articles = []
    seen = set()

    for heading in soup.find_all([
        "h1",
        "h2",
        "h3",
        "h4",
    ]):
        title = clean_text(
            heading.get_text(" ", strip=True)
        )

        if len(title) < 3 or len(title) > 150:
            continue

        card = get_card_for_heading(
            heading,
            max_headings=3,
        )

        if not card:
            continue

        detail_url = get_card_link(
            card,
            source["url"],
            title,
        )

        if not detail_url:
            continue

        key = canonical_url(detail_url)

        if key in seen:
            continue

        seen.add(key)

        card_text = clean_content(
            card.get_text("\n", strip=True)
        )

        image_url = extract_image(
            card,
            source["url"],
        )

        detail_content, detail_image, _ = extract_article(
            detail_url
        )

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
            extract_date(
                card_text + " " + content,
                event_calendar=True,
            ),
            extract_location(
                card_text + " " + content
            ),
        )

        if article:
            articles.append(article)

            logger.info(
                "%s: event found: %s",
                source["name"],
                title,
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "%s: generic extractor found %s events",
        source["name"],
        len(articles),
    )

    return articles


# ---------------------------------------------------------
# VIVO RIO
# ---------------------------------------------------------

def collect_vivo_rio(source):
    soup = get_soup(source["url"])

    if not soup:
        return []

    structured = extract_jsonld_events(soup, source)

    if structured:
        logger.info(
            "Vivo Rio JSON-LD found %s events",
            len(structured),
        )
        return structured[:MAX_ARTICLES_PER_SOURCE]

    articles = []
    seen = set()

    for heading in soup.find_all("h2"):
        artist = clean_text(
            heading.get_text(" ", strip=True)
        )

        if len(artist) < 2 or len(artist) > 120:
            continue

        card = get_card_for_heading(
            heading,
            max_headings=3,
        )

        if not card:
            continue

        subtitle = ""

        for subheading in card.find_all("h3"):
            candidate = clean_text(
                subheading.get_text(" ", strip=True)
            )

            if candidate and candidate.lower() != artist.lower():
                subtitle = candidate
                break

        title = (
            f"{artist} — {subtitle}"
            if subtitle
            else artist
        )

        detail_url = get_card_link(
            card,
            source["url"],
            artist,
        )

        if not detail_url:
            continue

        key = canonical_url(detail_url)

        if key in seen:
            continue

        seen.add(key)

        card_text = clean_content(
            card.get_text("\n", strip=True)
        )

        content, image_url, _ = extract_article(
            detail_url
        )

        if len(content) < MIN_CONTENT_LENGTH:
            content = card_text

        image_url = (
            image_url
            or extract_image(card, source["url"])
        )

        article = build_article(
            source,
            title,
            detail_url,
            content,
            image_url,
            extract_date(
                card_text + " " + content,
                event_calendar=True,
            ),
            extract_location(
                card_text + " " + content
            ),
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
        "%s: found %s events",
        source["name"],
        len(articles),
    )

    return articles


# ---------------------------------------------------------
# QUALISTAGE
# ---------------------------------------------------------

def collect_qualistage(source):
    """
    Keep the Qualistage-specific ticket-link approach,
    with JSON-LD and card extraction support.
    """

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

    for heading in soup.find_all([
        "h1",
        "h2",
        "h3",
    ]):
        title = clean_text(
            heading.get_text(" ", strip=True)
        )

        if len(title) < 2 or len(title) > 150:
            continue

        card = get_card_for_heading(
            heading,
            max_headings=3,
        )

        if not card:
            continue

        card_text = clean_content(
            card.get_text("\n", strip=True)
        )

        links = []
        link_keys = set()

        for link in card.find_all("a", href=True):
            candidate = normalize_url(
                link["href"],
                source["url"],
            )

            if not candidate:
                continue

            label = clean_text(
                link.get_text(" ", strip=True)
            ).lower()

            if not (
                "ingresso" in label
                or "ticket" in label
                or "saiba mais" in label
                or "comprar" in label
            ):
                continue

            key = canonical_url(candidate)

            if key in link_keys:
                continue

            link_keys.add(key)
            links.append(candidate)

        if not links:
            detail_url = get_card_link(
                card,
                source["url"],
                title,
            )
        else:
            # Prefer the first unique ticket/detail link.
            detail_url = links[0]

        if not detail_url:
            continue

        key = canonical_url(detail_url)

        if key in seen:
            continue

        seen.add(key)

        image_url = extract_image(
            card,
            source["url"],
        )

        article = build_article(
            source,
            title,
            detail_url,
            card_text,
            image_url,
            extract_date(
                card_text,
                event_calendar=True,
            ),
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
        "Qualistage: found %s events",
        len(articles),
    )

    return articles


# ---------------------------------------------------------
# CITY HALL WORDPRESS NEWS
# ---------------------------------------------------------

def collect_city_hall(source):
    """
    Collect City Hall news.

    Priority:
    1. WordPress REST API
    2. JSON-LD on the listing page
    3. Article links from the listing page
    """

    soup = get_soup(source["url"])

    if not soup:
        return []

    articles = []
    seen = set()

    parsed = urlparse(source["url"])

    api_url = (
        f"{parsed.scheme}://{parsed.netloc}"
        "/wp-json/wp/v2/posts"
    )

    # First try WordPress REST API.
    try:
        if robots_allowed(api_url):
            wait_for_host(api_url)

            response = SESSION.get(
                api_url,
                params={
                    "per_page": 20,
                    "_embed": "1",
                },
                timeout=TIMEOUT,
            )

            response.raise_for_status()
            posts = response.json()

            for post in posts:
                title = clean_text(
                    BeautifulSoup(
                        post.get("title", {}).get(
                            "rendered",
                            "",
                        ),
                        "html.parser",
                    ).get_text(" ", strip=True)
                )

                detail_url = normalize_url(
                    post.get("link", ""),
                    source["url"],
                )

                content_html = post.get(
                    "content",
                    {},
                ).get("rendered", "")

                content_soup = parse_html(content_html)

                content, _ = extract_article_from_soup(
                    content_soup,
                    detail_url or source["url"],
                )

                image_url = ""

                embedded = post.get("_embedded", {})
                media = embedded.get(
                    "wp:featuredmedia",
                    [],
                )

                if media:
                    image_url = normalize_url(
                        media[0].get("source_url", ""),
                        source["url"],
                    )

                if (
                    not title
                    or not detail_url
                    or not same_domain(
                        detail_url,
                        source["url"],
                    )
                ):
                    continue

                key = canonical_url(detail_url)

                if key in seen:
                    continue

                seen.add(key)

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

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as error:
        logger.info(
            "WordPress API unavailable; using page links instead: %s",
            error,
        )

    # If the API didn't return usable articles,
    # try structured event/article data.
    if not articles:
        structured = extract_jsonld_events(
            soup,
            source,
        )

        if structured:
            articles.extend(
                structured[:MAX_ARTICLES_PER_SOURCE]
            )

    # Final fallback: discover article links on the page.
    if not articles:
        for link in soup.find_all("a", href=True):
            title = clean_text(
                link.get_text(" ", strip=True)
            )

            if len(title) < 30 or len(title) > 250:
                continue

            detail_url = normalize_url(
                link["href"],
                source["url"],
            )

            if (
                not detail_url
                or not same_domain(
                    detail_url,
                    source["url"],
                )
                or is_excluded_url(detail_url)
                or detail_url.rstrip("/")
                == source["url"].rstrip("/")
            ):
                continue

            key = canonical_url(detail_url)

            if key in seen:
                continue

            seen.add(key)

            content, image_url, detail_soup = (
                extract_article(detail_url)
            )

            if len(content) < MIN_CONTENT_LENGTH:
                continue

            # Reuse the already-fetched soup.
            if detail_soup:
                heading = detail_soup.find("h1")

                if heading:
                    page_title = clean_text(
                        heading.get_text(" ", strip=True)
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
        "%s: found %s articles",
        source["name"],
        len(articles),
    )

    return articles


# ---------------------------------------------------------
# SOURCE ROUTING
# ---------------------------------------------------------

def collect_event_source(source):
    name = source["name"].lower()

    if "vivo rio" in name:
        return collect_vivo_rio(source)

    if "qualistage" in name:
        return collect_qualistage(source)

    return collect_generic_events(source)


def load_sources():
    with open(
        SOURCES_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)["sources"]


# ---------------------------------------------------------
# MAIN COLLECTION
# ---------------------------------------------------------

def collect_news():
    all_articles = []

    for source in load_sources():
        logger.info("=" * 60)
        logger.info(
            "Checking source: %s",
            source["name"],
        )

        try:
            source_type = source.get(
                "type",
                "web",
            )

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

        if not url:
            continue

        key = canonical_url(url)

        if key in seen_urls:
            continue

        seen_urls.add(key)
        unique_articles.append(article)

    logger.info("=" * 60)
    logger.info(
        "Collected %s unique articles and events",
        len(unique_articles),
    )

    return unique_articles


def main():
    articles = collect_news()

    print(
        json.dumps(
            articles,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
