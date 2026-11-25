"""Content extraction: turn a file path or URL into plain text."""

import hashlib
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# Text sent to the classifier / stored for agents is capped so one giant
# PDF can't blow up prompt sizes. Agents see the same capped text.
MAX_CONTENT_CHARS = 60_000

TEXT_SUFFIXES = {".md", ".txt", ".markdown", ".rst", ".csv", ".json", ".yaml", ".yml"}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def extract_file(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(p))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif p.suffix.lower() in TEXT_SUFFIXES or p.suffix == "":
        text = p.read_text(errors="replace")
    else:
        raise ValueError(f"unsupported file type: {p.suffix!r} ({path})")
    return text[:MAX_CONTENT_CHARS]


def fetch_url(url: str) -> str:
    """Fetch a web page and return its raw HTML."""
    response = httpx.get(url, follow_redirects=True, timeout=30.0)
    response.raise_for_status()
    return response.text


def html_to_text(html: str) -> str:
    """Strip HTML to readable text (links keep their anchor text)."""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav"]):
        element.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_CONTENT_CHARS]
