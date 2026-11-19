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

ROBOTS_UA = "RioNewsCollector"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

TIMEOUT = (8, 25)
ROBOTS_TIMEOUT = (4, 6)          # short — robots.txt must not block the run
MAX_ARTICLES_PER_SOURCE = 12
MIN_CONTENT_LENGTH = 45
MIN_EVENT_CONTENT_LENGTH = 120   # events need real content, not chrome
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
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "_ga", "_gl",
}

CATEGORY_BY_PATH = {
    "saude": "Health",
    "saúde": "Health",
    "ordem-publica": "Public Order",
    "ordem-pública": "Public Order",
    "forca-municipal": "Public Order",
    "força-municipal": "Public Order",
    "cidade": "City",
    "cet-rio": "Transport",
    "rioluz": "Infrastructure",
    "turismo": "Tourism",
    "riotur": "Tourism",
    "cultura": "Culture",
    "educacao": "Education",
    "educação": "Education",
    "procurador-do-municipio": "Finance",
    "mobilidade": "Transport",
}

DEFAULT_LOCATION = {
    "Vivo Rio": "Vivo Rio",
    "Qualistage": "Qualistage",
}

# Exact class / id tokens that unambiguously mean navigation.
BOILERPLATE_CLASS_TOKENS = frozenset({
    "navbar",
    "menu",
    "navigation",
    "breadcrumb",
    "site-header",
    "site-footer",
    "masthead",
    "topbar",
    "top-bar",
    "main-menu",
    "primary-menu",
    "top-menu",
    "footer-menu",
    "header-menu",
    "skip-link",
})

BOILERPLATE_TAGS = ("nav", "header", "footer")
CONTENT_TAGS = ("main", "article")

# First-line words that are never artist names.
ARTIST_BLACKLIST = frozenset({
    "SHOW", "EVENTO", "EVENTOS", "INGRESSO", "INGRESSOS",
    "DIA", "HOJE", "AMANHA", "AMANHÃ",
    "DESCRICAO", "DESCRIÇÃO", "SOBRE", "TICKETS",
})

MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

# ---------------------------------------------------------
# HTTP SESSIONS
# ---------------------------------------------------------

# Main session: retries, backoff, generous timeout.
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

# Robots session: no retries, short timeout, fail fast.
# Repeated 25s timeouts on robots.txt used to consume ~2.5 minutes
# per run. We only need to know "is it there or not?" — one try.
ROBOTS_SESSION = requests.Session()
ROBOTS_SESSION.headers.update({"User-Agent": HEADERS["User-Agent"]})

ROBOTS_CACHE = {}
LAST_REQUEST_TIME = {}

# Hosts that have already returned 403 in this run. Once a host is
# blocked, skip all further requests to it — no point retrying.
BLOCKED_HOSTS = set()


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

        response = ROBOTS_SESSION.get(
            robots_url,
            timeout=ROBOTS_TIMEOUT,
        )
        status = response.status_code

        if 200 <= status < 300:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            ROBOTS_CACHE[host] = (parser, True)
            return parser.can_fetch(ROBOTS_UA, url)

        if 400 <= status < 500 and status != 429:
            ROBOTS_CACHE[host] = (None, True)
            return True

        ROBOTS_CACHE[host] = (None, False)
        logger.warning(
            "robots.txt unreachable for %s (status %s) — skipping host",
            host, status,
        )
        return False

    except requests.RequestException as error:
        logger.warning(
            "robots.txt check failed for %s: %s — skipping host",
            host, error,
        )
        ROBOTS_CACHE[host] = (None, False)
        return False


# ---------------------------------------------------------
# TEXT AND URL HELPERS
# ---------------------------------------------------------

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_content(value):
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
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    kept = [
        (key, value)
        for key, value in parse_qsl(
            parsed.query, keep_blank_values=True,
        )
        if key.lower() not in TRACKING_PARAMS
    ]
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path, "",
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


def derive_category(url, default=None):
    path = urlparse(url).path.lower()
    for segment in path.split("/"):
        if segment in CATEGORY_BY_PATH:
            return CATEGORY_BY_PATH[segment]
    return default


# ---------------------------------------------------------
# HTTP AND HTML FETCHING
# ---------------------------------------------------------

def fetch_page(url):
    host = host_key(url)

    if host in BLOCKED_HOSTS:
        logger.debug("Skipping blocked host: %s", host)
        return None

    if not robots_allowed(url):
        logger.info("Skipping disallowed page: %s", url)
        return None

    try:
        wait_for_host(url)
        response = SESSION.get(url, timeout=TIMEOUT)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and "html" not in content_type:
            logger.warning("Not an HTML page: %s", url)
            return None

        if (
            response.encoding is None
            or response.encoding.lower() == "iso-8859-1"
        ):
            response.encoding = response.apparent_encoding

        return response.text

    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 403:
            BLOCKED_HOSTS.add(host)
            logger.warning(
                "Host returned 403 — blocking %s for the rest of this run",
                host,
            )
        else:
            logger.warning("Could not fetch %s: %s", url, error)
        return None

    except requests.RequestException as error:
        logger.warning("Could not fetch %s: %s", url, error)
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

def _parse_srcset(srcset):
    """Return the widest URL from a srcset string."""
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
                    width = int(float(descriptor[:-1]) * 1000)
            except ValueError:
                width = 0
        entries.append((width, parts[0]))
    entries.sort(key=lambda item: item[0])
    return entries[-1][1] if entries else ""


def extract_image(container, base_url):
    if not container:
        return ""

    # 1. Video posters (some event pages use <video> posters).
    for video in container.find_all("video"):
        poster = video.get("poster")
        if poster:
            result = normalize_url(poster, base_url)
            if result:
                return result

    # 2. <source> tags inside <picture> (often higher-quality than <img>).
    for source in container.find_all("source"):
        for attr in ("srcset", "data-srcset"):
            srcset = source.get(attr)
            if srcset:
                widest = _parse_srcset(srcset)
                if widest:
                    result = normalize_url(widest, base_url)
                    if result:
                        return result

    # 3. Inline / lazy CSS background-image.
    for styled in container.find_all(style=True):
        style = styled.get("style") or ""
        if "background-image" not in style.lower():
            continue
        match = re.search(
            r'url\(\s*["\']?([^"\')]+)',
            style, re.IGNORECASE,
        )
        if match:
            result = normalize_url(match.group(1), base_url)
            if result:
                return result

    # 4. data-background / data-bg attributes (common on lazy-loaders).
    for el in container.find_all(attrs=True):
        for attr in ("data-background", "data-bg", "data-background-image"):
            value = el.get(attr)
            if value and isinstance(value, str) and "url(" not in value:
                result = normalize_url(value, base_url)
                if result:
                    return result

    # 5. <img> with srcset / data-src / src.
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
            widest = _parse_srcset(srcset)
            if widest:
                candidates.insert(0, widest)

        for candidate in candidates:
            if candidate and not candidate.startswith("data:"):
                result = normalize_url(candidate, base_url)
                if result:
                    return result

    return ""


def extract_card_image(card, base_url, max_levels_up=4):
    """
    Try the card, then walk up parents.

    Walks 4 levels now (was 2) because event listing sites often
    place posters on a wrapper ancestor rather than inside the
    smallest heading container.
    """
    if not card:
        return ""

    image = extract_image(card, base_url)
    if image:
        return image

    parent = card.parent
    for _ in range(max_levels_up):
        if not parent:
            break
        image = extract_image(parent, base_url)
        if image:
            return image
        parent = parent.parent

    return ""


def extract_page_image(soup, page_url):
    if not soup:
        return ""

    for key in ("og:image", "twitter:image", "twitter:image:src"):
        tag = (
            soup.find("meta", property=key)
            or soup.find("meta", attrs={"name": key})
        )
        if tag and tag.get("content"):
            return normalize_url(tag["content"], page_url)

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


def extract_date(text, event_calendar=False):
    if not text:
        return ""

    normalized = clean_text(text).lower()
    today = date.today()

    sale_terms = (
        "pré-venda", "pre-venda", "pre venda",
        "venda de ingressos", "vendas de ingressos",
        "ingressos à venda", "ingressos a venda",
        "compra de ingressos", "comprar ingressos",
        "bilheteria", "abertura das vendas",
        "início das vendas", "inicio das vendas",
    )

    publication_terms = (
        "published on", "publicado em", "publicado no dia",
        "published:", "updated on", "atualizado em",
        "atualizado:", "posted on", "posted:",
    )

    def is_sale_context(match):
        start = max(0, match.start() - 100)
        end = min(len(normalized), match.end() + 100)
        return any(t in normalized[start:end] for t in sale_terms)

    def is_publication_context(match):
        start = max(0, match.start() - 60)
        end = min(len(normalized), match.end() + 60)
        return any(t in normalized[start:end] for t in publication_terms)

    def usable(match):
        return (
            not is_sale_context(match)
            and not is_publication_context(match)
        )

    # DD/MM/YYYY or DD-MM-YYYY
    for match in re.finditer(
        r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b",
        normalized,
    ):
        if not usable(match):
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
        if not usable(match):
            continue
        try:
            return date.fromisoformat(match.group()).isoformat()
        except ValueError:
            continue

    # Portuguese long form: 16 de fevereiro de 2027
    pattern = (
        r"\b(?:dia\s+)?(\d{1,2})\s+de\s+"
        r"(janeiro|fevereiro|março|marco|abril|maio|junho|"
        r"julho|agosto|setembro|outubro|novembro|dezembro)"
        r"(?:\s+de\s+(20\d{2}))?\b"
    )

    for match in re.finditer(pattern, normalized):
        if not usable(match):
            continue

        start = max(0, match.start() - 90)
        end = min(len(normalized), match.end() + 90)
        context = normalized[start:end]

        event_words = (
            "no dia", "dia ", "acontece", "acontecerá",
            "acontecera", "show", "apresentação", "apresentacao",
            "realizado", "será", "sera",
            "no qualistage", "no rio de janeiro",
        )
        if not any(w in context for w in event_words):
            continue

        day = int(match.group(1))
        month = MONTHS[match.group(2)]
        year = int(match.group(3)) if match.group(3) else today.year

        try:
            candidate = date(year, month, day)
        except ValueError:
            continue

        if not match.group(3) and candidate < today:
            continue

        return candidate.isoformat()

    # Compact calendar form — 3 variants:
    #   20 | jan   /   20/jan   /   20 jan
    short_patterns = (
        r"\b(\d{1,2})\s*[|/]\s*"
        r"(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\b",
        r"\b(\d{1,2})\s+"
        r"(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\b",
    )

    for pattern in short_patterns:
        for match in re.finditer(pattern, normalized):
            if not usable(match):
                continue

            month = MONTHS[match.group(2)]
            day = int(match.group(1))

            try:
                candidate = date(today.year, month, day)
            except ValueError:
                continue

            if candidate < today:
                days_old = (today - candidate).days
                if event_calendar and days_old > 30:
                    try:
                        candidate = date(
                            today.year + 1, month, day,
                        )
                    except ValueError:
                        continue
                else:
                    continue

            return candidate.isoformat()

    return ""


def extract_date_from_time_tags(container):
    """
    Extract a date from <time datetime="..."> or <time>text</time>.

    The datetime attribute is authoritative when present.
    """
    if not container:
        return ""

    for time_tag in container.find_all("time"):
        dt = time_tag.get("datetime", "")
        if dt:
            match = re.search(r"\d{4}-\d{2}-\d{2}", str(dt))
            if match:
                try:
                    return date.fromisoformat(match.group()).isoformat()
                except ValueError:
                    pass

        text = clean_text(time_tag.get_text(" ", strip=True))
        parsed = extract_date(text, event_calendar=True)
        if parsed:
            return parsed

    return ""


def extract_event_date(card, content, event_calendar=True):
    """
    Try card → parent → content for a date.

    Events often have a date badge in a sibling element or the
    parent wrapper, which get_card_for_heading may not include.
    """
    if card:
        date_from_time = extract_date_from_time_tags(card)
        if date_from_time:
            return date_from_time

        card_text = clean_content(card.get_text("\n", strip=True))
        combined = card_text + " " + (content or "")

        result = extract_date(combined, event_calendar=event_calendar)
        if result:
            return result

        parent = card.parent
        if parent:
            parent_text = clean_content(
                parent.get_text("\n", strip=True)
            )[:1000]
            result = extract_date(
                parent_text + " " + combined,
                event_calendar=event_calendar,
            )
            if result:
                return result

    return extract_date(content or "", event_calendar=event_calendar)


# ---------------------------------------------------------
# ARTICLE EXTRACTION
# ---------------------------------------------------------

def extract_article_from_soup(soup, page_url):
    if not soup:
        return "", ""

    image_url = extract_page_image(soup, page_url)

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
            logger.debug("Trafilatura extraction failed: %s", error)

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
        content_area = soup.body or soup

    if not content_area:
        return "", image_url

    content_area = parse_html(str(content_area))

    for element in content_area.find_all([
        "script", "style", "nav", "footer", "header",
        "aside", "form", "noscript", "iframe",
    ]):
        element.decompose()

    if not image_url:
        inline_image = extract_image(content_area, page_url)
        if inline_image:
            image_url = inline_image

    paragraphs = []
    for element in content_area.find_all(["p", "h2", "h3", "li"]):
        text = clean_text(element.get_text(" ", strip=True))
        if len(text) >= 30 and text not in paragraphs:
            paragraphs.append(text)

    content = "\n\n".join(paragraphs)

    if len(content) < MIN_CONTENT_LENGTH:
        content = clean_content(content_area.get_text("\n", strip=True))

    return content[:15000], image_url


def extract_article(url):
    soup = get_soup(url)
    if not soup:
        return "", "", None
    content, image_url = extract_article_from_soup(soup, url)
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
    category=None,
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

    # Event sources need real content, not just chrome like
    # "SEAL | (8) Descrição ingressos sobre o evento".
    if (
        source.get("type") == "events"
        and len(content) < MIN_EVENT_CONTENT_LENGTH
    ):
        logger.info("Skipping thin event content: %s", title)
        return None

    cancellation_terms = (
        "evento cancelado", "event cancelled", "event canceled",
        "show cancelado", "apresentação cancelada",
        "apresentacao cancelada",
    )

    combined = (title + " " + content).lower()
    if any(t in combined for t in cancellation_terms):
        logger.info("Skipping cancelled item: %s", title)
        return None

    final_category = (
        category
        or derive_category(detail_url)
        or source.get("category")
        or "News"
    )

    final_location = (
        event_location
        or extract_location(content)
        or DEFAULT_LOCATION.get(source.get("name", ""), "")
    )

    return {
        "source_name": source["name"],
        "source_url": detail_url,
        "original_title": title,
        "original_content": content[:15000],
        "image_url": image_url or "",
        "image_credit": source["name"] if image_url else "",
        "event_date": event_date or extract_date(content),
        "event_location": final_location,
        "category": final_category,
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
            str(item).lower() == "event" for item in event_type
        )

        if is_event:
            title = clean_text(value.get("name", ""))
            description = clean_content(value.get("description", ""))

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
                    address = (
                        address.get("streetAddress", "")
                        or address.get("addressLocality", "")
                    )
                location = location.get("name") or address or ""

            if len(description) < MIN_CONTENT_LENGTH:
                if (
                    event_url != source["url"]
                    and same_domain(event_url, source["url"])
                ):
                    detail_content, detail_image, _ = extract_article(event_url)
                    if len(detail_content) > len(description):
                        description = detail_content
                    image_url = image_url or detail_image

            article = build_article(
                source, title, event_url, description,
                image_url, event_date, clean_text(location),
            )

            if article:
                key = canonical_url(article["source_url"])
                if key not in seen:
                    seen.add(key)
                    results.append(article)

        for child_key, child in value.items():
            if child_key not in (
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


# ---------------------------------------------------------
# EVENT CARD IDENTIFICATION
# ---------------------------------------------------------

def _inside_boilerplate(element, max_depth=6):
    depth = 0
    for ancestor in element.parents:
        if depth > max_depth:
            break

        tag = (ancestor.name or "").lower()
        if tag in CONTENT_TAGS:
            return False
        if tag in BOILERPLATE_TAGS:
            return True

        tokens = set()
        for cls in ancestor.get("class") or []:
            tokens.update(str(cls).lower().split())

        element_id = str(ancestor.get("id") or "").lower()
        if element_id:
            tokens.add(element_id)

        if tokens & BOILERPLATE_CLASS_TOKENS:
            return True

        depth += 1

    return False


def get_card_for_heading(
    heading,
    max_headings=3,
    min_text=35,
    max_text=3500,
):
    if _inside_boilerplate(heading):
        return None

    current = heading
    candidates = []

    for _ in range(9):
        if current.parent is None:
            break

        current = current.parent

        headings = current.find_all(["h1", "h2", "h3", "h4"])
        text = clean_text(current.get_text(" ", strip=True))

        if (
            1 <= len(headings) <= max_headings
            and min_text <= len(text) <= max_text
        ):
            candidates.append(current)

    if not candidates:
        return None

    for candidate in candidates:
        if candidate.find("a", href=True):
            return candidate

    return candidates[-1]


def get_card_link(card, base_url, title=""):
    if not card:
        return ""

    candidates = []

    ignored = (
        "instagram", "facebook", "youtube", "whatsapp",
        "twitter", "tiktok", "privacy", "política",
        "politica", "contato", "contact",
    )

    ticket_domains = (
        "eventim", "ticketmaster", "livepass", "ingresse", "sympla",
    )

    for link in card.find_all("a", href=True):
        candidate = normalize_url(link["href"], base_url)
        if not candidate or is_excluded_url(candidate):
            continue

        label = clean_text(link.get_text(" ", strip=True)).lower()
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

    unique = {}
    for item in candidates:
        key = item["canonical"]
        if key not in unique:
            unique[key] = item

    candidates = list(unique.values())
    if not candidates:
        return ""

    title_lower = clean_text(title).lower()

    if title_lower:
        matching = [
            item for item in candidates
            if title_lower in item["label"]
            or item["label"] in title_lower
        ]
        if matching:
            candidates = matching

    same_site = [item for item in candidates if item["same_site"]]
    if same_site:
        same_site.sort(
            key=lambda item: len(urlparse(item["url"]).path)
        )
        return same_site[0]["url"]

    ticket_links = [item for item in candidates if item["ticket"]]
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
            source["name"], len(structured),
        )
        return structured[:MAX_ARTICLES_PER_SOURCE]

    articles = []
    seen = set()

    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        title = clean_text(heading.get_text(" ", strip=True))
        if len(title) < 3 or len(title) > 150:
            continue

        card = get_card_for_heading(heading, max_headings=3)
        if not card:
            logger.debug("No card for heading: %s", title[:60])
            continue

        detail_url = get_card_link(card, source["url"], title)
        if not detail_url:
            logger.debug("No link for heading: %s", title[:60])
            continue

        key = canonical_url(detail_url)
        if key in seen:
            continue
        seen.add(key)

        card_text = clean_content(card.get_text("\n", strip=True))
        image_url = extract_card_image(card, source["url"])

        detail_content, detail_image, _ = extract_article(detail_url)

        content = (
            detail_content
            if len(detail_content) >= len(card_text)
            else card_text
        )
        image_url = detail_image or image_url

        article = build_article(
            source, title, detail_url, content, image_url,
            extract_event_date(card, content),
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


# ---------------------------------------------------------
# VIVO RIO
# ---------------------------------------------------------

def _looks_like_subtitle(text):
    t = clean_text(text).lower()
    return (
        t.startswith((
            "apresenta", "apresentação", "apresentacao",
            "show:", "evento:", "tour:",
        ))
        or "apresenta:" in t
        or len(text) > 100
    )


def _extract_artist_from_content(content):
    """
    If content's first non-empty line is a short ALL-CAPS name,
    treat it as the artist. Covers layouts where the H2 holds
    the show name and the artist appears above the description
    as a bold uppercase word (e.g. TARJA).
    """
    if not content:
        return ""

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue

        if 2 <= len(line) <= 60 and line.isupper():
            if line in ARTIST_BLACKLIST:
                return ""
            if not any(c.isalpha() for c in line):
                return ""
            return line

        return ""

    return ""


def _find_artist_from_detail(detail_soup):
    if not detail_soup:
        return ""

    h1 = detail_soup.find("h1")
    if h1:
        text = clean_text(h1.get_text(" ", strip=True))
        if 2 <= len(text) <= 120:
            return text

    title_tag = detail_soup.find("title")
    if title_tag:
        text = clean_text(title_tag.get_text(" ", strip=True))
        text = re.split(r"\s+[-|–—]\s+", text, maxsplit=1)[0]
        if 2 <= len(text) <= 120:
            return text

    return ""


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
        heading_text = clean_text(heading.get_text(" ", strip=True))
        if len(heading_text) < 2 or len(heading_text) > 150:
            continue

        card = get_card_for_heading(heading, max_headings=3)
        if not card:
            logger.debug(
                "Vivo Rio: no card for %s", heading_text[:60],
            )
            continue

        subtitle = ""
        for subheading in card.find_all("h3"):
            candidate = clean_text(
                subheading.get_text(" ", strip=True)
            )
            if candidate and candidate.lower() != heading_text.lower():
                subtitle = candidate
                break

        detail_url = get_card_link(card, source["url"], heading_text)
        if not detail_url:
            logger.debug(
                "Vivo Rio: no link for %s", heading_text[:60],
            )
            continue

        key = canonical_url(detail_url)
        if key in seen:
            continue
        seen.add(key)

        card_text = clean_content(card.get_text("\n", strip=True))
        content, image_url, detail_soup = extract_article(detail_url)

        if len(content) < MIN_CONTENT_LENGTH:
            content = card_text

        image_url = image_url or extract_card_image(card, source["url"])

        # Title construction — try three strategies in order:
        # 1. ALL-CAPS artist at the top of the content.
        # 2. Subtitle heuristics + detail page H1.
        # 3. Heading + subtitle.
        content_artist = _extract_artist_from_content(content)

        if (
            content_artist
            and content_artist.lower() != heading_text.lower()
        ):
            title = f"{content_artist} — {heading_text}"
        elif _looks_like_subtitle(heading_text):
            artist = _find_artist_from_detail(detail_soup)
            if not artist:
                artist = heading_text
            title = f"{artist} — {heading_text}"
        else:
            artist = heading_text
            title = (
                f"{artist} — {subtitle}"
                if subtitle
                else artist
            )

        article = build_article(
            source, title, detail_url, content, image_url,
            extract_event_date(card, content),
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


# ---------------------------------------------------------
# QUALISTAGE
# ---------------------------------------------------------

def collect_qualistage(source):
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

        card = get_card_for_heading(heading, max_headings=3)
        if not card:
            continue

        card_text = clean_content(card.get_text("\n", strip=True))

        links = []
        link_keys = set()

        for link in card.find_all("a", href=True):
            candidate = normalize_url(link["href"], source["url"])
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

            link_key = canonical_url(candidate)
            if link_key in link_keys:
                continue
            link_keys.add(link_key)
            links.append(candidate)

        detail_url = (
            links[0]
            if links
            else get_card_link(card, source["url"], title)
        )

        if not detail_url:
            continue

        key = canonical_url(detail_url)
        if key in seen:
            continue
        seen.add(key)

        # Try fetching the ticket detail page (image, content, date).
        # On many hosts this will fail (403, robots.txt timeout) —
        # the caller will fall back to card data, which is fine.
        detail_content, detail_image, _ = extract_article(detail_url)

        content = (
            detail_content
            if len(detail_content) >= len(card_text)
            else card_text
        )

        image_url = (
            detail_image
            or extract_card_image(card, source["url"])
        )

        combined_text = card_text + "\n\n" + (detail_content or "")

        article = build_article(
            source, title, detail_url, content, image_url,
            extract_event_date(card, combined_text),
            extract_location(combined_text),
        )

        if article:
            articles.append(article)
            logger.info("Qualistage event found: %s", title)
        else:
            logger.debug(
                "Qualistage: rejected %s (url=%s)",
                title, detail_url,
            )

        if len(articles) >= MAX_ARTICLES_PER_SOURCE:
            break

    logger.info(
        "Qualistage: found %s events", len(articles),
    )
    return articles


# ---------------------------------------------------------
# CITY HALL WORDPRESS NEWS
# ---------------------------------------------------------

def collect_city_hall(source):
    soup = get_soup(source["url"])
    if not soup:
        return []

    articles = []
    seen = set()

    parsed = urlparse(source["url"])
    api_url = (
        f"{parsed.scheme}://{parsed.netloc}/wp-json/wp/v2/posts"
    )

    try:
        if robots_allowed(api_url):
            wait_for_host(api_url)

            response = SESSION.get(
                api_url,
                params={"per_page": 20, "_embed": "1"},
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

                content_html = post.get(
                    "content", {},
                ).get("rendered", "")

                content_soup = parse_html(content_html)
                content, _ = extract_article_from_soup(
                    content_soup,
                    detail_url or source["url"],
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
                    or not same_domain(detail_url, source["url"])
                ):
                    continue

                key = canonical_url(detail_url)
                if key in seen:
                    continue
                seen.add(key)

                article = build_article(
                    source, title, detail_url, content,
                    image_url,
                    "",  # news, not events
                    extract_location(content),
                )

                if article:
                    articles.append(article)
                    logger.info(
                        "City Hall news found: %s", title,
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

    if not articles:
        structured = extract_jsonld_events(soup, source)
        if structured:
            articles.extend(
                structured[:MAX_ARTICLES_PER_SOURCE]
            )

    if not articles:
        for link in soup.find_all("a", href=True):
            title = clean_text(link.get_text(" ", strip=True))
            if len(title) < 30 or len(title) > 250:
                continue

            detail_url = normalize_url(
                link["href"], source["url"],
            )

            if (
                not detail_url
                or not same_domain(detail_url, source["url"])
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

            if detail_soup:
                heading = detail_soup.find("h1")
                if heading:
                    page_title = clean_text(
                        heading.get_text(" ", strip=True)
                    )
                    if len(page_title) >= 12:
                        title = page_title

            article = build_article(
                source, title, detail_url, content,
                image_url, "",
                extract_location(content),
            )

            if article:
                articles.append(article)
                logger.info(
                    "City Hall news found: %s", title,
                )

            if len(articles) >= MAX_ARTICLES_PER_SOURCE:
                break

    logger.info(
        "%s: found %s articles",
        source["name"], len(articles),
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
    with open(SOURCES_FILE, "r", encoding="utf-8") as file:
        return json.load(file)["sources"]


# ---------------------------------------------------------
# MAIN COLLECTION
# ---------------------------------------------------------

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
    print(json.dumps(articles, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
