import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# Browser-like headers to avoid bot detection
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Alternative headers for retry (Googlebot-like)
HEADERS_ALT = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 15
MAX_PAGES = 15

# Zero-width and invisible unicode characters — replaced with SPACE (not empty)
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u200e\u200f\u00ad]")
# Collapse whitespace
_WHITESPACE_RE = re.compile(r"[ \t\u00a0]+")

# Boilerplate phrases (line-level removal if line < 80 chars)
_BOILERPLATE_PHRASES = [
    "get started", "learn more", "read more", "sign up", "start free trial",
    "book a demo", "request a demo", "schedule a demo", "try for free",
    "contact sales", "talk to sales", "watch demo", "see it in action",
    "start now", "join now", "subscribe now", "download now",
    "accept all cookies", "cookie policy", "we use cookies",
    "accept cookies", "manage cookies", "cookie settings",
    "skip to main content", "skip to footer", "skip to navigation",
    "skip to content", "toggle navigation", "close menu", "open menu",
    "register now", "sign in", "log in", "create account",
]

# Short standalone CTA phrases to strip even inline (exact match boundaries)
_INLINE_CTA_RE = re.compile(
    r"\b(?:Get Started|Learn More|Read More|Sign Up|Book a Demo|Request a Demo|"
    r"Register Now|Start Free Trial|Try for Free|Contact Sales|Talk to Sales|"
    r"Watch Demo|See it in Action|Download Now|Subscribe Now|Start Now|"
    r"Join Now|Skip to main content|Skip to footer|Skip to navigation|"
    r"Skip to content)\b",
    re.IGNORECASE,
)


def _build_session() -> requests.Session:
    """Build a requests session with retry logic."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _clean_text(text: str) -> str:
    """Remove invisible chars (replaced with space), collapse whitespace, strip JS artifacts."""
    # Replace invisible chars with space (prevents word merging)
    text = _INVISIBLE_RE.sub(" ", text)
    # Collapse whitespace
    text = _WHITESPACE_RE.sub(" ", text)
    # Collapse blank lines
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    # Strip JS placeholder artifacts
    text = re.sub(r"\bundefined\b", "", text)
    text = re.sub(r"\bnull\b(?!\s+(?:and|or|pointer|value|check|safety))", "", text)
    return text.strip()


def _remove_boilerplate(text: str) -> str:
    """Remove CTA, cookie-banner, and navigation boilerplate."""
    # Line-level removal
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        lower = line.lower().strip()
        if not lower:
            continue
        if any(bp == lower or (bp in lower and len(lower) < 80) for bp in _BOILERPLATE_PHRASES):
            continue
        cleaned.append(line)
    text = "\n".join(cleaned)

    # Inline CTA removal
    text = _INLINE_CTA_RE.sub("", text)
    # Clean up resulting double spaces
    text = _WHITESPACE_RE.sub(" ", text)

    return text.strip()


def _extract_structured_data(soup: BeautifulSoup) -> dict:
    """Extract JSON-LD schema.org and OpenGraph metadata."""
    structured = {}

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0] if data else {}
            if isinstance(data, dict):
                if data.get("description"):
                    structured["schema_description"] = _clean_text(data["description"])
                if data.get("name"):
                    structured["schema_name"] = _clean_text(data["name"])
                if data.get("@type"):
                    structured["schema_type"] = data["@type"]
        except (json.JSONDecodeError, TypeError, IndexError):
            continue

    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        structured["og_description"] = _clean_text(og_desc["content"])

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        structured["og_title"] = _clean_text(og_title["content"])

    og_site = soup.find("meta", property="og:site_name")
    if og_site and og_site.get("content"):
        structured["og_site_name"] = _clean_text(og_site["content"])

    return structured


def _same_domain(base_url: str, link: str) -> bool:
    return urlparse(link).netloc == urlparse(base_url).netloc


def _absolute(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


def _extract_nav_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract internal links from nav, header, and top-level anchor tags."""
    seen: set[str] = set()
    links: list[str] = []

    containers = soup.select("nav, header, [role='navigation']")
    if not containers:
        containers = [soup]

    for container in containers:
        for a in container.find_all("a", href=True):
            href = _absolute(base_url, a["href"])
            href = href.split("#")[0].rstrip("/")
            if not href or href in seen:
                continue
            if not href.startswith(("http://", "https://")):
                continue
            if not _same_domain(base_url, href):
                continue
            seen.add(href)
            links.append(href)

    return links


def _fetch_page(session: requests.Session, url: str, headers: dict) -> requests.Response | None:
    """Fetch a page with given session and headers."""
    try:
        resp = session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        # Skip non-HTML responses
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None
        return resp
    except requests.RequestException:
        return None


def _scrape_page(session: requests.Session, url: str) -> dict | None:
    """Scrape a single page and return structured content."""
    # Try primary headers first, then alt headers
    resp = _fetch_page(session, url, HEADERS)
    if resp is None:
        resp = _fetch_page(session, url, HEADERS_ALT)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract structured data before decomposing elements
    structured_data = _extract_structured_data(soup)

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = ""
    if meta_desc_tag and meta_desc_tag.get("content"):
        meta_description = _clean_text(meta_desc_tag["content"])

    h1_tag = soup.find("h1")
    h1 = _clean_text(h1_tag.get_text(strip=True)) if h1_tag else ""

    headings = [
        _clean_text(h.get_text(strip=True))
        for h in soup.find_all(["h2", "h3"])
        if h.get_text(strip=True)
    ]

    # Decompose noisy elements before extracting body text
    for tag in soup.find_all(["script", "style", "nav", "footer", "aside", "header", "noscript"]):
        tag.decompose()
    # Decompose by role/class patterns
    for selector in [
        "[role='navigation']", "[role='banner']", "[role='contentinfo']",
        "[class*='cookie']", "[id*='cookie']",
        "[class*='banner']", "[class*='popup']", "[class*='modal']",
        "[class*='mega-menu']", "[class*='nav-']", "[class*='dropdown-menu']",
    ]:
        for tag in soup.select(selector):
            tag.decompose()

    # Main text: prefer <main>, then <article>, then <body>
    main = soup.find("main") or soup.find("article") or soup.body
    text_preview = ""
    if main:
        raw_text = main.get_text(separator=" ", strip=True)
        text_preview = _remove_boilerplate(_clean_text(raw_text))

    return {
        "url": url,
        "title": _clean_text(title),
        "meta_description": meta_description,
        "h1": h1,
        "headings": headings,
        "text_preview": text_preview,
        "structured_data": structured_data,
    }


def _scrape_site_requests(url: str) -> dict:
    """Primary scraper using requests with retry logic."""
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc

    session = _build_session()

    # Fetch homepage
    resp = _fetch_page(session, url, HEADERS)
    if resp is None:
        resp = _fetch_page(session, url, HEADERS_ALT)
    if resp is None:
        raise RuntimeError(f"Cannot reach {url}")

    soup = BeautifulSoup(resp.text, "html.parser")

    nav_links = _extract_nav_links(soup, base_url)
    homepage = base_url.rstrip("/")
    if homepage not in nav_links:
        nav_links.insert(0, homepage)

    pages: list[dict] = []
    scraped_urls: set[str] = set()

    for link in nav_links:
        normalized = link.rstrip("/")
        if normalized in scraped_urls:
            continue
        scraped_urls.add(normalized)

        page = _scrape_page(session, link)
        if page:
            pages.append(page)
        if len(pages) >= MAX_PAGES:
            break

    return {
        "domain": domain,
        "pages": pages,
    }


def _scrape_site_scrapy(url: str) -> dict:
    """Fallback scraper using Scrapy via subprocess."""
    domain = urlparse(url).netloc
    spider_script = Path(__file__).parent / "scrapy_fallback.py"

    if not spider_script.exists():
        return {"domain": domain, "pages": []}

    fd, output_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)

    try:
        subprocess.run(
            [sys.executable, str(spider_script), url, output_path],
            capture_output=True,
            timeout=90,
        )
        with open(output_path) as f:
            result = json.load(f)
        return result
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
        return {"domain": domain, "pages": []}
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def scrape_site(url: str) -> dict:
    """Scrape a website: try requests first, fall back to Scrapy.

    Returns a dict with domain and scraped pages.
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Strategy 1: requests with retry
    try:
        result = _scrape_site_requests(url)
        if result["pages"]:
            return result
    except Exception:
        pass

    # Strategy 2: Scrapy fallback (different engine, better cookie/redirect handling)
    try:
        result = _scrape_site_scrapy(url)
        if result["pages"]:
            return result
    except Exception:
        pass

    # Both failed
    domain = urlparse(url).netloc
    raise RuntimeError(f"Cannot scrape {url}: all strategies failed (requests + Scrapy)")
