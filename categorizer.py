from urllib.parse import urlparse

RULES: list[tuple[str, list[str]]] = [
    ("pricing", ["pricing", "plans", "tarif"]),
    ("product", ["product", "features", "solution", "use-case"]),
    ("about", ["about", "team", "a-propos", "qui-sommes"]),
    ("contact", ["contact"]),
    ("blog", ["blog", "articles", "news", "actualites"]),
    ("legal", ["privacy", "terms", "legal", "cgu", "cgv", "mentions-legales"]),
    ("careers", ["careers", "jobs", "recrutement"]),
    ("faq", ["faq", "help", "support"]),
    ("partners", ["partner", "partenaire"]),
    ("case-study", ["case-stud", "temoignage", "success-stor"]),
    ("press", ["press", "presse", "media"]),
]


def categorize_page(url: str, content: dict) -> str:
    """Classify a page into a category based on URL path and content."""
    path = urlparse(url).path.lower().strip("/")
    h1 = (content.get("h1") or "").lower()
    title = (content.get("title") or "").lower()

    # Homepage detection
    if path in ("", "home", "index", "index.html"):
        return "home"

    # Localized homepages (e.g. /fr, /de, /en)
    if len(path) <= 3 and path.isalpha():
        return "home"

    # Rule-based matching on URL path, h1, and title
    searchable = f"{path} {h1} {title}"
    for category, keywords in RULES:
        for kw in keywords:
            if kw in searchable:
                return category

    return "other"
