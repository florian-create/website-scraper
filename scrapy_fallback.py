"""Standalone Scrapy spider for fallback scraping.

Run via subprocess from scraper.py when requests-based scraping fails.
Usage: python scrapy_fallback.py <url> <output_path>
"""

import json
import re
import sys
from urllib.parse import urljoin, urlparse

import scrapy
from scrapy.crawler import CrawlerProcess
from bs4 import BeautifulSoup

MAX_PAGES = 15

_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u200e\u200f]")
_WHITESPACE_RE = re.compile(r"[ \t\u00a0]+")
_BOILERPLATE_PHRASES = [
    "get started", "learn more", "read more", "sign up", "start free trial",
    "book a demo", "request a demo", "schedule a demo", "try for free",
    "contact sales", "talk to sales", "watch demo", "see it in action",
    "start now", "join now", "subscribe now", "download now",
    "accept all cookies", "cookie policy", "we use cookies",
    "accept cookies", "manage cookies", "cookie settings",
    "skip to main content", "skip to footer", "skip to navigation",
    "toggle navigation", "close menu", "open menu",
]


def clean_text(text):
    text = _INVISIBLE_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    text = re.sub(r"\bundefined\b", "", text)
    return text.strip()


def remove_boilerplate(text):
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        lower = line.lower().strip()
        if not lower:
            continue
        if any(bp == lower or (bp in lower and len(lower) < 80) for bp in _BOILERPLATE_PHRASES):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def extract_structured_data(soup):
    structured = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0] if data else {}
            if isinstance(data, dict):
                if data.get("description"):
                    structured["schema_description"] = clean_text(data["description"])
                if data.get("name"):
                    structured["schema_name"] = clean_text(data["name"])
                if data.get("@type"):
                    structured["schema_type"] = data["@type"]
        except (json.JSONDecodeError, TypeError, IndexError):
            continue
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        structured["og_description"] = clean_text(og_desc["content"])
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        structured["og_title"] = clean_text(og_title["content"])
    og_site = soup.find("meta", property="og:site_name")
    if og_site and og_site.get("content"):
        structured["og_site_name"] = clean_text(og_site["content"])
    return structured


def extract_page_data(html, url):
    """Extract page data from raw HTML using BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")

    structured_data = extract_structured_data(soup)

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    meta_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = ""
    if meta_tag and meta_tag.get("content"):
        meta_description = clean_text(meta_tag["content"])

    h1_tag = soup.find("h1")
    h1 = clean_text(h1_tag.get_text(strip=True)) if h1_tag else ""

    headings = [
        clean_text(h.get_text(strip=True))
        for h in soup.find_all(["h2", "h3"])
        if h.get_text(strip=True)
    ]

    # Decompose noisy elements
    for tag in soup.find_all(["script", "style", "nav", "footer", "aside", "header"]):
        tag.decompose()
    for sel in ["[role='navigation']", "[class*='cookie']", "[id*='cookie']",
                "[class*='banner']", "[class*='popup']", "[class*='modal']"]:
        for tag in soup.select(sel):
            tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.body
    text_preview = ""
    if main:
        raw_text = main.get_text(separator=" ", strip=True)
        text_preview = remove_boilerplate(clean_text(raw_text))

    return {
        "url": url,
        "title": clean_text(title),
        "meta_description": meta_description,
        "h1": h1,
        "headings": headings,
        "text_preview": text_preview,
        "structured_data": structured_data,
    }


class SiteSpider(scrapy.Spider):
    name = "site_spider"

    custom_settings = {
        "USER_AGENT": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "ROBOTSTXT_OBEY": False,
        "DOWNLOAD_TIMEOUT": 20,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429, 403],
        "LOG_LEVEL": "ERROR",
        "CONCURRENT_REQUESTS": 4,
        "DOWNLOAD_DELAY": 0.5,
        "COOKIES_ENABLED": True,
        "REDIRECT_ENABLED": True,
        "REDIRECT_MAX_TIMES": 5,
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
    }

    def __init__(self, target_url=None, output_path=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_url = target_url
        self.output_path = output_path
        self.domain = urlparse(target_url).netloc
        self.base_url = f"{urlparse(target_url).scheme}://{self.domain}"
        self.pages = []
        self.scraped_urls = set()

    def start_requests(self):
        yield scrapy.Request(
            self.target_url,
            callback=self.parse_homepage,
            errback=self.handle_error,
            dont_filter=True,
        )

    def parse_homepage(self, response):
        if response.status != 200:
            return

        page_data = extract_page_data(response.text, response.url)
        if page_data:
            self.pages.append(page_data)
            self.scraped_urls.add(response.url.rstrip("/"))

        # Discover nav links
        nav_selectors = [
            "nav a::attr(href)",
            "header a::attr(href)",
            "[role='navigation'] a::attr(href)",
        ]
        nav_links = set()
        for sel in nav_selectors:
            for href in response.css(sel).getall():
                full_url = urljoin(response.url, href).split("#")[0].rstrip("/")
                if urlparse(full_url).netloc == self.domain:
                    nav_links.add(full_url)

        # If no nav links found, try all links on the page
        if not nav_links:
            for href in response.css("a::attr(href)").getall():
                full_url = urljoin(response.url, href).split("#")[0].rstrip("/")
                if urlparse(full_url).netloc == self.domain:
                    nav_links.add(full_url)

        for link in nav_links:
            normalized = link.rstrip("/")
            if normalized in self.scraped_urls:
                continue
            if len(self.pages) >= MAX_PAGES:
                break
            self.scraped_urls.add(normalized)
            yield scrapy.Request(
                link,
                callback=self.parse_page,
                errback=self.handle_error,
            )

    def parse_page(self, response):
        if len(self.pages) >= MAX_PAGES:
            return
        if response.status != 200:
            return

        page_data = extract_page_data(response.text, response.url)
        if page_data:
            self.pages.append(page_data)

    def handle_error(self, failure):
        pass

    def closed(self, reason):
        result = {
            "domain": self.domain,
            "pages": self.pages,
        }
        with open(self.output_path, "w") as f:
            json.dump(result, f, ensure_ascii=False)


def run_spider(url, output_path):
    process = CrawlerProcess()
    process.crawl(SiteSpider, target_url=url, output_path=output_path)
    process.start()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python scrapy_fallback.py <url> <output_path>", file=sys.stderr)
        sys.exit(1)
    run_spider(sys.argv[1], sys.argv[2])
