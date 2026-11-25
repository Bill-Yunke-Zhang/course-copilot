"""Crawler tests against the real course schedule page (fixture snapshot)."""

from app.core.ingestion.crawler import classify_links, extract_links
from tests.conftest import FIXTURES, FakeLlm

BASE_URL = "https://aicoding.cs.wisc.edu/fa26/schedule.html"


def _schedule_links():
    html = (FIXTURES / "schedule.html").read_text()
    return extract_links(html, BASE_URL)


def test_extracts_read_links_with_labels():
    links = _schedule_links()
    git_book = next(link for link in links if "git-scm.com" in link.url)
    assert git_book.label == "Read"
    assert "Pro Git" in git_book.text


def test_extracts_watch_and_install_labels():
    links = _schedule_links()
    labels = {link.label for link in links}
    assert "Install" in labels
    assert "Read" in labels


def test_syllabus_link_resolved_relative_to_page():
    links = _schedule_links()
    assert any(
        link.url == "https://aicoding.cs.wisc.edu/fa26/syllabus.html" for link in links
    )


def test_anchors_mailto_and_self_links_skipped():
    links = _schedule_links()
    assert all(not link.url.endswith("#") for link in links)
    assert all("mailto:" not in link.url for link in links)
    assert all(link.url.split("#")[0] != BASE_URL for link in links)


def test_urls_deduplicated():
    links = _schedule_links()
    urls = [link.url for link in links]
    assert len(urls) == len(set(urls))


def test_classify_links_batches_one_llm_call(db):
    links = _schedule_links()[:5]
    llm = FakeLlm(
        {
            "_LinkDecisions": {
                "decisions": [
                    {"url": links[0].url, "tag": "reading"},
                    {"url": links[1].url, "tag": "skip"},
                ]
            }
        }
    )
    decisions = classify_links(db, llm, BASE_URL, links)
    assert decisions[links[0].url] == "reading"
    assert decisions[links[1].url] == "skip"
    assert len(llm.calls) == 1  # one batched call, not one per link
    assert llm.calls[0]["schema"] == "_LinkDecisions"
