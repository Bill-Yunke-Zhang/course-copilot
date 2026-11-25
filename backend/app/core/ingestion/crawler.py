"""Schedule/syllabus crawling (spec §4.4).

The schedule and syllabus are more than blobs: the harness follows their
outbound links and registers each discovered link as its OWN new resource,
tagged from the fixed vocabulary (a "Read" link → reading, a project link →
project-spec, a supplemental link → reference, …). Discovered resources are
flagged in the Resource List ("discovered from …") so the student can prune
anything irrelevant.

Two phases, split deliberately:
  1. `extract_links` — deterministic HTML parsing (no LLM): find candidate
     outbound links with their anchor text and nearby label ("Read:",
     "Watch:", "Released:", …), skipping navigation/anchors/mailto.
  2. `classify_links` — ONE batched LLM call maps candidates to a tag from
     the vocabulary or "skip".
"""

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.llm import LlmClient

# Tags a crawled link can receive (a link off the schedule is never e.g.
# someone's personal notes).
LINK_TAGS = ["project-spec", "reading", "video", "reference"]

LinkTagEnum = Enum(
    "LinkTagEnum", {**{t.replace("-", "_"): t for t in LINK_TAGS}, "skip": "skip"}
)


@dataclass
class CandidateLink:
    url: str
    text: str
    label: str  # the "Read"/"Watch"/"Released" style marker, if any


def extract_links(html: str, base_url: str) -> list[CandidateLink]:
    """Deterministic pass: candidate outbound links with context."""
    soup = BeautifulSoup(html, "html.parser")
    for nav in soup.find_all("nav"):
        nav.decompose()

    seen: set[str] = set()
    candidates: list[CandidateLink] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).scheme not in ("http", "https"):
            continue
        if url.split("#")[0] == base_url.split("#")[0]:
            continue
        if url in seen:
            continue
        seen.add(url)

        # The schedule marks links like "<b>Read</b>: <a …>"; grab that
        # preceding bold/strong marker when present.
        label = ""
        marker = anchor.find_previous_sibling(["b", "strong"])
        if marker is not None:
            label = marker.get_text(strip=True).rstrip(":")
        candidates.append(
            CandidateLink(url=url, text=anchor.get_text(" ", strip=True), label=label)
        )
    return candidates


class _LinkDecision(BaseModel):
    url: str
    tag: LinkTagEnum


class _LinkDecisions(BaseModel):
    decisions: list[_LinkDecision]


_PROMPT = """A course study assistant crawled the page below and found these
outbound links. For each link decide what kind of course resource it points
to, or "skip" if it should not be registered (login portals, tool downloads,
social links, other course-site pages like the syllabus itself, …).

Tags:
- project-spec: a project overview/requirements page
- reading: an assigned pre-class reading
- video: an assigned pre-class video
- reference: an optional/supplemental curated resource
- skip: not worth registering as a resource

The "label" is the marker the page put before the link (e.g. "Read",
"Watch", "Released", "Install"). Labels are strong evidence: Read →
reading, Watch → video, Released → project-spec, Install → usually skip.

PAGE: {page_url}

LINKS (one per line, as label | anchor text | url):
{links}

Return a decision for every url.
"""


def classify_links(
    db: Session,
    llm: LlmClient,
    page_url: str,
    candidates: list[CandidateLink],
    resource_id: int | None = None,
) -> dict[str, str]:
    """Map url → tag (or 'skip') with one batched LLM call."""
    if not candidates:
        return {}
    lines = "\n".join(
        f"{c.label or '-'} | {c.text or '-'} | {c.url}" for c in candidates
    )
    result = llm.generate(
        db,
        purpose="classification",
        prompt=_PROMPT.format(page_url=page_url, links=lines),
        response_schema=_LinkDecisions,
        resource_id=resource_id,
    )
    return {d.url: d.tag.value for d in result.parsed.decisions}
