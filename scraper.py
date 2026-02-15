import json
import re

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; WebsiteScraper/1.0; +https://example.com/bot)"
    )
}
TIMEOUT = 15
MAX_PAGES = 15

# Zero-width and invisible unicode characters
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
# Collapse whitespace (spaces, tabs, non-breaking spaces)
_WHITESPACE_RE = re.compile(r"[ \t\u00a0]+")

# Common CTA / boilerplate phrases to strip
_BOILERPLATE_PHRASES = [
    "get started", "learn more", "read more", "sign up", "start free trial",
    "book a demo", "request a demo", "schedule a demo", "try for free",
    "contact sales", "talk to sales", "watch demo", "see it in action",
    "start now", "join now", "subscribe now", "download now",
    "accept all cookies", "cookie policy", "we use cookies",
    "accept cookies", "manage cookies", "cookie settings",
]


def _clean_text(text: str) -> str:
    """Remove invisible chars and collapse whitespace."""
    text = _INVISIBLE_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    return text.strip()


def _remove_boilerplate(text: str) -> str:
    """Remove common CTA and cookie-banner sentences."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        lower = line.lower().strip()
        if any(bp in lower for bp in _BOILERPLATE_PHRASES) and len(lower) < 80:
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _extract_structured_data(soup: BeautifulSoup) -> dict:
    """Extract JSON-LD schema.org and OpenGraph metadata."""
    structured = {}

    # JSON-LD
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

    # OpenGraph
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


def _scrape_page(url: str) -> dict | None:
    """Scrape a single page and return structured content."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
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
    for tag in soup.find_all(["script", "style", "nav", "footer", "aside"]):
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


def scrape_site(url: str) -> dict:
    """Scrape a website's main pages starting from the given URL."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc}") from exc

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

        page = _scrape_page(link)
        if page:
            pages.append(page)
        if len(pages) >= MAX_PAGES:
            break

    return {
        "domain": domain,
        "pages": pages,
    }
