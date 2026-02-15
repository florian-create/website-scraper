from urllib.parse import urlparse

# Pass 1: URL-path keywords (high confidence, exact segment matches)
URL_RULES: list[tuple[str, list[str]]] = [
    ("pricing", ["pricing", "plans", "tarif", "price", "cost", "subscription"]),
    ("product", ["product", "features", "solution", "use-case", "platform", "capabilities"]),
    ("about", ["about", "team", "a-propos", "qui-sommes", "our-story", "our-team", "leadership"]),
    ("contact", ["contact", "contact-us", "contactez"]),
    ("blog", ["blog", "articles", "news", "actualites", "insights", "resources"]),
    ("legal", ["privacy", "terms", "legal", "cgu", "cgv", "mentions-legales", "cookie", "gdpr", "imprint", "disclaimer"]),
    ("careers", ["careers", "jobs", "recrutement", "hiring", "open-positions", "join-us", "work-with-us"]),
    ("faq", ["faq", "help", "support", "help-center", "knowledge-base"]),
    ("partners", ["partner", "partenaire", "integrations", "marketplace", "ecosystem"]),
    ("case-study", ["case-stud", "temoignage", "success-stor", "customer-stor", "clients", "testimonial"]),
    ("press", ["press", "presse", "media", "newsroom", "in-the-news"]),
    ("investors", ["investor", "ir", "shareholders", "annual-report", "governance"]),
    ("security", ["security", "compliance", "trust", "certifications", "soc2", "iso27001"]),
    ("api", ["api", "docs", "documentation", "developer", "reference", "changelog", "sdk"]),
]

# Pass 2: Content keywords (lower confidence, matched against title/h1/headings/meta)
CONTENT_RULES: list[tuple[str, list[str]]] = [
    ("pricing", ["pricing", "price", "cost", "subscription", "free trial", "per month", "per year", "plan"]),
    ("product", ["product", "feature", "solution", "how it works", "capabilities", "platform"]),
    ("about", ["about us", "our team", "our story", "who we are", "our mission", "founded"]),
    ("contact", ["contact us", "get in touch", "reach out"]),
    ("blog", ["blog", "article", "news", "latest post", "insights"]),
    ("legal", ["privacy policy", "terms of service", "terms and conditions", "cookie policy", "legal notice"]),
    ("careers", ["careers", "open positions", "join our team", "we're hiring", "job opening"]),
    ("faq", ["frequently asked", "faq", "help center", "common questions"]),
    ("partners", ["partners", "integrations", "marketplace", "ecosystem"]),
    ("case-study", ["case study", "customer story", "success story", "testimonial"]),
    ("press", ["press release", "in the news", "media coverage", "newsroom"]),
    ("investors", ["investor relations", "shareholders", "annual report", "quarterly results"]),
    ("security", ["security", "compliance", "trust center", "certifications", "data protection"]),
    ("api", ["api reference", "documentation", "developer guide", "sdk", "api docs"]),
]

# Pass 3: URL segment heuristics
_PRODUCT_PREFIXES = ("why-", "how-it-works", "what-is-", "tour", "demo", "overview")


def categorize_page(url: str, content: dict) -> str:
    """Classify a page into a category using 3-pass matching."""
    path = urlparse(url).path.lower().strip("/")
    segments = path.split("/")

    # Homepage detection
    if path in ("", "home", "index", "index.html"):
        return "home"
    if len(path) <= 3 and path.isalpha():
        return "home"

    # Pass 1: URL path keywords
    for category, keywords in URL_RULES:
        for kw in keywords:
            if kw in path:
                return category

    # Pass 2: Content keywords (title, h1, headings, meta_description)
    h1 = (content.get("h1") or "").lower()
    title = (content.get("title") or "").lower()
    meta = (content.get("meta_description") or "").lower()
    headings_text = " ".join((content.get("headings") or [])).lower()
    searchable_content = f"{title} {h1} {meta} {headings_text}"

    for category, keywords in CONTENT_RULES:
        for kw in keywords:
            if kw in searchable_content:
                return category

    # Pass 3: URL segment heuristics
    for segment in segments:
        if any(segment.startswith(prefix) for prefix in _PRODUCT_PREFIXES):
            return "product"

    return "other"
