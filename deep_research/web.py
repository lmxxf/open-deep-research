"""Small, key-free web tools. Search failures are explicit, never seed URLs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .text import clean_surrogates, dumps
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
import trafilatura

_TRACKING = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"}
_BLOCKED = re.compile(
    r"captcha|verify (?:that )?you are human|checking your browser|"
    r"just a moment|access denied|unusual traffic|人机验证|安全验证|访问验证",
    re.I,
)


def canonical_url(url: str) -> str:
    """Normalize HTTP URLs for deduplication without destroying meaningful queries."""
    try:
        parts = urlsplit(clean_surrogates(url).strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username or parts.password:
            return ""
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port and not (parts.scheme.lower() == "http" and port == 80 or parts.scheme.lower() == "https" and port == 443):
            host += f":{port}"
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in _TRACKING]
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(sorted(query)), ""))
    except (ValueError, AttributeError):
        return ""


def _valid_text(title: str, text: str) -> bool:
    # Do not reject a real article merely because it discusses CAPTCHAs.
    if _BLOCKED.search(title):
        return False
    return len(text.strip()) >= 100 and not (
        len(text) < 2500 and _BLOCKED.search(text[:1200])
    )


class Web:
    def __init__(self, cache_dir: Path, timeout: float = 15, concurrency: int = 4):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = max(0.1, float(timeout))
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._client = httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LightweightResearch/0.1)"},
        )
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        async with self._semaphore:
            # One attempt per provider; fallback is the retry budget.
            response = await asyncio.wait_for(self._client.get(url, **kwargs), self.timeout)
            response.raise_for_status()
            if len(response.content) > 8_000_000:
                raise RuntimeError("Response exceeds the 8 MB document limit")
            return response

    @staticmethod
    def _clean_results(rows: list[dict], limit: int, provider: str) -> list[dict]:
        rows = clean_surrogates(rows)
        result, seen = [], set()
        for row in rows:
            url = canonical_url(row.get("url") or row.get("href") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            result.append({"url": url, "title": str(row.get("title", "")),
                           "snippet": str(row.get("snippet") or row.get("body") or ""),
                           "provider": provider})
            if len(result) >= limit:
                break
        return result

    async def _search_ddgs(self, query: str, limit: int) -> list[dict]:
        from ddgs import DDGS

        def run():
            return list(DDGS(timeout=min(self.timeout, 5)).text(query, max_results=limit, backend="yahoo"))

        async with self._semaphore:
            # DDGS also has its own HTTP timeout: cancelling to_thread cannot kill a thread.
            return await asyncio.wait_for(asyncio.to_thread(run), min(self.timeout, 6))

    async def _search_duckduckgo_lite(self, query: str, limit: int) -> list[dict]:
        response = await self._get("https://lite.duckduckgo.com/lite/", params={"q": query})
        soup = BeautifulSoup(clean_surrogates(response.text), "html.parser")
        rows = []
        for link in soup.select("a.result-link"):
            href = urljoin(str(response.url), link.get("href", ""))
            target = dict(parse_qsl(urlsplit(href).query)).get("uddg", href)
            row = link.find_parent("tr")
            snippet = None
            # A Lite result spans multiple table rows. Stop before the next result
            # so an absent snippet cannot accidentally borrow another source's text.
            if row:
                for sibling in row.find_next_siblings("tr"):
                    if sibling.select_one("a.result-link"):
                        break
                    snippet = sibling.select_one(".result-snippet")
                    if snippet:
                        break
            rows.append({"url": target, "title": link.get_text(" ", strip=True),
                         "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
        return rows

    async def _search_bing(self, query: str, limit: int) -> list[dict]:
        response = await self._get("https://www.bing.com/search", params={"q": query, "format": "rss"})
        root = ET.fromstring(clean_surrogates(response.text))
        return [{"url": item.findtext("link", ""), "title": item.findtext("title", ""),
                 "snippet": item.findtext("description", "")}
                for item in root.findall(".//item")]

    async def _search_duckduckgo(self, query: str, limit: int) -> list[dict]:
        response = await self._get("https://html.duckduckgo.com/html/", params={"q": query})
        soup = BeautifulSoup(clean_surrogates(response.text), "html.parser")
        rows = []
        for result in soup.select(".result"):
            link = result.select_one("a.result__a")
            if not link:
                continue
            href = urljoin(str(response.url), link.get("href", ""))
            redirect = dict(parse_qsl(urlsplit(href).query)).get("uddg")
            snippet = result.select_one(".result__snippet")
            rows.append({"url": redirect or href, "title": link.get_text(" ", strip=True),
                         "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
        return rows

    async def search(self, query: str, limit: int = 4) -> list[dict]:
        query = clean_surrogates(query)
        if not query.strip():
            raise ValueError("Search query must not be empty")
        limit = max(1, min(int(limit), 20))
        failures = []
        deadline = time.monotonic() + self.timeout
        for provider, search in (("duckduckgo-lite", self._search_duckduckgo_lite),
                                 ("ddgs-yahoo", self._search_ddgs),
                                 ("duckduckgo-html", self._search_duckduckgo),
                                 ("bing-rss", self._search_bing)):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failures.append("total search deadline exceeded")
                    break
                # Keep time for another provider when the first one stalls.
                allowance = min(remaining, max(0.01, self.timeout / 2), 6)
                raw = await asyncio.wait_for(search(query, limit), allowance)
                rows = self._clean_results(raw, limit, provider)
                if rows:
                    return rows
                failures.append(f"{provider}: no usable results (possibly blocked)")
            except Exception as exc:
                # Only the exception type: HTTP error strings may carry signed URLs.
                failures.append(f"{provider}: {type(exc).__name__}")
        raise RuntimeError("All free search providers failed; " + "; ".join(failures))

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _load_cache(self, url: str) -> dict | None:
        try:
            value = clean_surrogates(json.loads(self._cache_path(url).read_text(encoding="utf-8")))
            value["content_hash"] = hashlib.sha256(value["text"].encode("utf-8")).hexdigest()
            # A day is a practical prototype TTL; fetched_at remains the original timestamp.
            age = time.time() - datetime.fromisoformat(value["fetched_at"]).timestamp()
            if 0 <= age < 86400 and _valid_text(value["title"], value["text"]):
                value["cache_hit"] = True
                return value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    async def _fetch_direct(self, url: str) -> tuple[str, str]:
        response = await self._get(url)
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or "octet-stream" in content_type:
            raise RuntimeError("Binary document requires Reader conversion")
        if "text/plain" in content_type or "text/markdown" in content_type:
            return urlsplit(url).hostname or url, clean_surrogates(response.text)
        soup = BeautifulSoup(clean_surrogates(response.text), "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if _BLOCKED.search(title):
            raise RuntimeError("Website returned a challenge or access-denied page")
        text = await asyncio.to_thread(trafilatura.extract, clean_surrogates(response.text), url=url,
                                       include_comments=False, include_tables=True, favor_precision=True)
        if not text:
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            article = soup.find("article") or soup.find("main")
            text = article.get_text("\n", strip=True) if article else ""
        return title, text or ""

    async def _fetch_reader(self, url: str) -> tuple[str, str]:
        response = await self._get("https://r.jina.ai/" + url,
                                   headers={"Accept": "text/plain", "X-Retain-Images": "none"})
        raw = clean_surrogates(response.text)
        if re.search(r"^Warning:.*(?:error (?:403|404|429|5\d\d)|captcha)", raw, re.I | re.M):
            raise RuntimeError("Reader returned an upstream error or challenge")
        title_match = re.search(r"^Title:\s*(.*)$", raw, re.M)
        title = title_match.group(1).strip() if title_match else ""
        text = raw.split("Markdown Content:", 1)[-1].strip()
        return title, text

    async def fetch(self, url: str) -> dict:
        url = canonical_url(url)
        if not url:
            raise ValueError("A valid HTTP(S) URL without credentials is required")
        # Concurrent callers of one URL share the completed cache record.
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self._load_cache(url)
            if cached:
                return cached
            failures = []
            for name, fetcher in (("direct", self._fetch_direct), ("jina-reader", self._fetch_reader)):
                try:
                    title, text = await fetcher(url)
                    title, text = clean_surrogates((title, text))
                    if not _valid_text(title, text):
                        raise RuntimeError("Empty, short, or challenge page")
                    value = {"url": url, "title": title, "text": text,
                             "fetched_at": datetime.now(timezone.utc).isoformat(),
                             "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                             "cache_hit": False, "provider": name}
                    path = self._cache_path(url)
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(dumps(value, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(path)
                    return value
                except Exception as exc:
                    failures.append(f"{name}: {type(exc).__name__}")
            raise RuntimeError("Could not extract usable webpage text; " + "; ".join(failures))
