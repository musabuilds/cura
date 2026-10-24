"""The refresh agent: re-reads facilities' own websites and proposes corrections.

Guard rails — the model is a reader, not an author:

1. It only sees text fetched from the facility's own site (robots.txt respected, 1 req/s per host).
2. Every extracted value must come with a verbatim `evidence` quote; values whose quote is not found
   in the fetched text are discarded. This is the hallucination filter.
3. Output goes to `sources/staging/agent_web.jsonl` with a low trust prior (0.6), so any official or
   human source outranks it, and every change still passes `cura merge` + `cura validate` + PR review.
4. When `CURA_API_URL` and `CURA_AGENT_TOKEN` are set it posts proposals to the API instead, where they
   wait for a moderator like any other contributor's.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from cura import normalize as nz
from cura.agent.llm import LLM
from cura.collectors._common import USER_AGENT, now_iso, write_staging
from cura.pipeline.merge import load_canonical

SOURCE = "agent_web"
CONTACT_HINTS = re.compile(r"contact|about|اتصل|تواصل|من-نحن|about-us|working-hours|insurance|تأمين", re.I)
MAX_CHARS = 12_000

SYSTEM = """You extract facts about a healthcare facility in Jordan from its own website text.
Return a JSON object with only the keys you can support from the text:
  phones (list), emails (list), opening_hours (string, OSM syntax if possible), emergency (bool),
  address (string), departments (list), services (list), insurance_accepted (list), equipment (list), beds (int)
For EVERY key also add "<key>_evidence": a short verbatim quote copied exactly from the text.
Never infer, never use outside knowledge, never translate the quote. If unsure, omit the key. Return {} if nothing."""


class Fetcher:
    def __init__(self) -> None:
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
        self.robots: dict[str, RobotFileParser] = {}
        self.last_hit: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        host = urlsplit(url).netloc
        if host not in self.robots:
            rp = RobotFileParser()
            try:
                r = self.client.get(f"{urlsplit(url).scheme}://{host}/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self.robots[host] = rp
        return self.robots[host].can_fetch(USER_AGENT, url)

    def get(self, url: str) -> str | None:
        if not self.allowed(url):
            return None
        host = urlsplit(url).netloc
        wait = 1.0 - (time.monotonic() - self.last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.last_hit[host] = time.monotonic()
        try:
            r = self.client.get(url)
        except httpx.HTTPError:
            return None
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return None
        return r.text


def page_text(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    links = [a["href"] for a in soup.find_all("a", href=True)]
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip(), links


def collect_site_text(fetcher: Fetcher, website: str) -> tuple[str, list[str]]:
    html = fetcher.get(website)
    if not html:
        return "", []
    text, links = page_text(html)
    pages, texts = [website], [text]
    for href in links:
        url = urljoin(website, href)
        if urlsplit(url).netloc == urlsplit(website).netloc and CONTACT_HINTS.search(url) and url not in pages:
            pages.append(url)
            if more := fetcher.get(url):
                texts.append(page_text(more)[0])
        if len(pages) >= 4:
            break
    return " \n ".join(texts)[:MAX_CHARS], pages


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", nz.normalize_arabic(str(s))).strip().lower()


def grounded(extracted: dict, text: str) -> dict:
    """Keep only values whose verbatim evidence quote is present in the source text."""
    haystack = _norm(text)
    out = {}
    for key, value in extracted.items():
        if key.endswith("_evidence") or value in (None, "", []):
            continue
        quote = extracted.get(f"{key}_evidence")
        if isinstance(quote, str) and len(quote) >= 4 and _norm(quote) in haystack:
            out[key] = value
    return out


def pick_targets(limit: int) -> list[dict]:
    """Stalest first: facilities with a website, ordered by last verification."""
    facilities = [f for f in load_canonical("facilities") if f["status"] != "merged" and f["contact"].get("website")]
    facilities.sort(key=lambda f: (f["quality"].get("last_verified_at") or "", -f["quality"]["completeness"]))
    return facilities[:limit]


def _post_proposal(api: str, token: str, facility: dict, data: dict, pages: list[str]) -> None:
    patch: dict = {}
    if data.get("phones"):
        patch.setdefault("contact", {})["phones"] = nz.phones(data["phones"]) or None
    if data.get("opening_hours"):
        patch["hours"] = nz.parse_opening_hours(data["opening_hours"])
    for key in ("departments", "services"):
        if data.get(key):
            patch[key] = data[key]
    if isinstance(data.get("emergency"), bool):
        patch["emergency"] = data["emergency"]
    if patch:
        httpx.patch(
            f"{api}/v1/facilities/{facility['id']}",
            headers={"Authorization": f"Bearer {token}"},
            json={"patch": patch, "reason": "agent_web re-read of the facility website", "evidence_url": pages[0]},
            timeout=30,
        )


def run(limit: int = 25, dry_run: bool = False) -> dict:
    llm, fetcher = LLM(), Fetcher()
    api, token = os.environ.get("CURA_API_URL"), os.environ.get("CURA_AGENT_TOKEN")
    records, stats = [], {"checked": 0, "blocked_or_down": 0, "extracted": 0, "dropped_ungrounded": 0}
    for f in pick_targets(limit):
        stats["checked"] += 1
        text, pages = collect_site_text(fetcher, f["contact"]["website"])
        if not text:
            stats["blocked_or_down"] += 1
            continue
        try:
            raw = llm.json(SYSTEM, f"Facility: {f['name'].get('en') or f['name'].get('ar')}\n\nWebsite text:\n{text}")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            print(f"{f['id']}: model error {exc!s:.120}")
            continue
        data = grounded(raw, text)
        stats["dropped_ungrounded"] += len([k for k in raw if not k.endswith("_evidence")]) - len(data)
        if not data:
            continue
        stats["extracted"] += 1
        if api and token and not dry_run:
            _post_proposal(api, token, f, data, pages)
            continue
        records.append(
            {
                "kind": "facility",
                "source": SOURCE,
                "source_ref": f["id"],
                "source_url": pages[0],
                "retrieved_at": now_iso(),
                "data": {
                    "name_en": f["name"].get("en"),
                    "name_ar": f["name"].get("ar"),
                    "type": f["type"],
                    "website": f["contact"]["website"],
                    **data,
                },
            }
        )
    if records and not dry_run:
        write_staging(
            SOURCE,
            records,
            {
                "title": "LLM-assisted re-read of facility websites (evidence-checked)",
                "publisher": "Cura agent",
                "urls": [],
                "retrieved_at": now_iso(),
                "method": "scrape",
                "license": "public-web (facts)",
                "robots_respected": True,
                "trust": 0.6,
                "notes": f"model={llm.config.model}; values kept only when a verbatim quote was found in the page text",
                "generated_at": datetime.now(UTC).isoformat(),
            },
        )
    return stats | {"written": 0 if dry_run else len(records)}
