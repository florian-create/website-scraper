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
            # Strip fragment
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

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = ""
    if meta_desc_tag and meta_desc_tag.get("content"):
        meta_description = meta_desc_tag["content"].strip()

    h1_tag = soup.find("h1")
    h1 = h1_tag.get_text(strip=True) if h1_tag else ""

    headings = [
        h.get_text(strip=True)
        for h in soup.find_all(["h2", "h3"])
        if h.get_text(strip=True)
    ]

    # Main text: prefer <main>, then <article>, then <body>
    main = soup.find("main") or soup.find("article") or soup.body
    text_preview = ""
    if main:
        text_preview = main.get_text(separator=" ", strip=True)

    return {
        "url": url,
        "title": title,
        "meta_description": meta_description,
        "h1": h1,
        "headings": headings,
        "text_preview": text_preview,
        "images_count": len(soup.find_all("img", src=True)),
        "links_count": len(soup.find_all("a", href=True)),
    }


def scrape_site(url: str) -> dict:
    """Scrape a website's main pages starting from the given URL.

    Returns a dict with all scraped pages and metadata.
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc

    # 1. Fetch homepage
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc}") from exc

    soup = BeautifulSoup(resp.text, "html.parser")

    # 2. Discover nav links
    nav_links = _extract_nav_links(soup, base_url)
    # Always include the homepage itself
    homepage = base_url.rstrip("/")
    if homepage not in nav_links:
        nav_links.insert(0, homepage)

    # 3. Scrape each page
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
