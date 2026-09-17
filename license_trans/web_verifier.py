"""Official-source web verification for high-risk translations.

Network failures never fail the pipeline. Evidence is stored when found.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

LOG = logging.getLogger("license_trans.web")

HIGH_PRIORITY_SUFFIXES = (
    ".gov.cn",
    ".gov.hk",
    ".gov.mo",
    ".edu.cn",
    ".ac.cn",
    ".org.cn",
)
HIGH_PRIORITY_HOSTS = {
    "gsxt.gov.cn",
    "www.gsxt.gov.cn",
    "www.gov.cn",
    "www.cac.gov.cn",
}
LOW_PRIORITY_HINTS = (
    "blog", "forum", "baike", "wikipedia", "zhihu", "csdn", "jianshu",
    "youdao", "fanyi", "translate.google", "deepl.com", "tieba",
    "weibo", "toutiao", "sohu.com", "163.com/dy",
)

_USER_AGENT = (
    "Mozilla/5.0 (compatible; LicenseTransVerifier/1.0; +https://local)"
)


@dataclass
class WebEvidence:
    source: str
    translation: str
    verified: bool
    source_url: str | None = None
    source_domain: str | None = None
    verified_at: str | None = None
    source_type: str = "unknown"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.texts: list[str] = []
        self._capture = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
            self._capture = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "a" and self._capture:
            self.texts.append("".join(self._buf))
            self._capture = False

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)
        self.texts.append(data)


def classify_domain(host: str) -> str:
    host = (host or "").lower()
    if any(host.endswith(sfx) for sfx in HIGH_PRIORITY_SUFFIXES) or host in HIGH_PRIORITY_HOSTS:
        if host.endswith(".edu.cn") or host.endswith(".ac.cn"):
            return "education"
        if "gov" in host:
            return "government"
        return "authoritative"
    if any(h in host for h in LOW_PRIORITY_HINTS):
        return "low_priority"
    if host.endswith(".com.cn") or host.endswith(".com"):
        return "company"
    return "unknown"


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        LOG.exception("url parse failed: %s", url)
        return ""


def _fetch(url: str, timeout: float = 6.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(400_000)
        charset = "utf-8"
        ctype = resp.headers.get("Content-Type", "")
        m = re.search(r"charset=([\\w-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
        return raw.decode(charset, errors="replace")


def _ddg_search(query: str, timeout: float = 6.0) -> list[str]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    html = _fetch(url, timeout=timeout)
    parser = _LinkParser()
    parser.feed(html)
    links: list[str] = []
    for href in parser.links:
        if "uddg=" in href:
            parsed = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed.query)
            real = qs.get("uddg", [None])[0]
            if real:
                links.append(urllib.parse.unquote(real))
        elif href.startswith("http") and "duckduckgo.com" not in href:
            links.append(href)
    return links


def _looks_like_english(text: str) -> bool:
    if not text:
        return False
    letters = sum(ch.isascii() and ch.isalpha() for ch in text)
    return letters >= 3 and not re.search(r"[\u4e00-\u9fff]", text)


class WebTranslationVerifier:
    """Best-effort official-source check. Never raises into the pipeline."""

    HIGH_RISK_FIELDS = {"name", "address", "authority"}

    def verify(
        self,
        source: str,
        translation: str,
        *,
        field: str = "",
        timeout: float = 6.0,
    ) -> WebEvidence:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not source or not translation:
            return WebEvidence(
                source=source, translation=translation, verified=False,
                verified_at=now, reason="empty",
            )
        try:
            query = f'"{source}" "{translation}"'
            links = _ddg_search(query, timeout=timeout)
        except Exception as exc:
            LOG.exception("web search failed")
            return WebEvidence(
                source=source, translation=translation, verified=False,
                verified_at=now, reason=f"search_failed:{exc}",
            )
        ranked: list[tuple[int, str, str]] = []
        for url in links[:8]:
            host = _host_of(url)
            kind = classify_domain(host)
            priority = {
                "government": 0, "education": 1, "authoritative": 2,
                "company": 3, "unknown": 4, "low_priority": 5,
            }.get(kind, 4)
            ranked.append((priority, url, kind))
        ranked.sort(key=lambda x: x[0])
        for priority, url, kind in ranked:
            if kind == "low_priority":
                continue
            try:
                html = _fetch(url, timeout=timeout)
            except Exception:
                LOG.exception("fetch failed: %s", url)
                continue
            if source in html and (
                translation in html or _looks_like_english(translation)
            ):
                # Require both Chinese source and Latin translation nearby-ish
                if translation.lower() in html.lower() or source in html:
                    if translation.lower() in html.lower() and source in html:
                        return WebEvidence(
                            source=source,
                            translation=translation,
                            verified=True,
                            source_url=url,
                            source_domain=_host_of(url),
                            verified_at=now,
                            source_type=kind,
                            reason="page_contains_both",
                        )
        return WebEvidence(
            source=source,
            translation=translation,
            verified=False,
            verified_at=now,
            reason="no_official_evidence",
        )
