"""Deterministic tests; network checks are deliberately separate."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from deep_research.web import Web, canonical_url


class CanonicalTests(unittest.TestCase):
    def test_tracking_removed_but_query_preserved(self):
        self.assertEqual(canonical_url("https://EXAMPLE.org:443/a?utm_source=x&q=中文&b=2#foo"),
                         "https://example.org/a?b=2&q=%E4%B8%AD%E6%96%87")
        self.assertEqual(canonical_url("https://example.org/?page=1"), "https://example.org/?page=1")
        self.assertNotEqual(canonical_url("https://example.org/a/"), canonical_url("https://example.org/a"))

    def test_invalid_or_credential_urls(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.org", "http://host:bad"):
            self.assertEqual(canonical_url(url), "")


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.web = Web(Path(self.temp.name), timeout=0.1)
        self.web._search_duckduckgo_lite = AsyncMock(return_value=[])
        self.web._search_duckduckgo = AsyncMock(return_value=[])

    async def asyncTearDown(self):
        await self.web.close()
        self.temp.cleanup()

    async def test_lite_parses_redirect_and_matching_snippet(self):
        html = """<table>
        <tr><td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa">First</a></td></tr>
        <tr><td class="result-snippet">Evidence for first.</td></tr>
        <tr><td><a class="result-link" href="https://example.org/b">No snippet</a></td></tr>
        <tr><td><a class="result-link" href="https://example.org/c">Third</a></td></tr>
        <tr><td class="result-snippet">Evidence for third.</td></tr>
        </table>"""
        self.web._get = AsyncMock(return_value=httpx.Response(
            200, text=html, request=httpx.Request("GET", "https://lite.duckduckgo.com/lite/")))
        rows = await Web._search_duckduckgo_lite(self.web, "query", 4)
        self.assertEqual(rows[0]["url"], "https://example.org/a")
        self.assertEqual(rows[0]["snippet"], "Evidence for first.")
        self.assertEqual(rows[1]["snippet"], "")
        self.assertEqual(rows[2]["snippet"], "Evidence for third.")

    async def test_lite_challenge_returns_no_results_for_fallback(self):
        self.web._get = AsyncMock(return_value=httpx.Response(
            202, text="<html><title>Challenge</title><form>Verify you are human</form></html>",
            request=httpx.Request("GET", "https://lite.duckduckgo.com/lite/")))
        rows = await Web._search_duckduckgo_lite(self.web, "query", 4)
        self.assertEqual(rows, [])

    async def test_search_fallback_deduplicates(self):
        self.web._search_ddgs = AsyncMock(side_effect=RuntimeError("blocked"))
        self.web._search_bing = AsyncMock(return_value=[
            {"url": "https://example.org/a?utm_source=x", "title": "A"},
            {"url": "https://example.org/a#fragment", "title": "duplicate"},
            {"url": "https://example.org/b", "title": "B"}])
        rows = await self.web.search("a real query", 4)
        self.assertEqual([r["title"] for r in rows], ["A", "B"])
        self.assertEqual(rows[0]["provider"], "bing-rss")

    async def test_all_search_providers_fail_without_invented_results(self):
        self.web._search_ddgs = AsyncMock(side_effect=RuntimeError("secret-key-do-not-echo"))
        self.web._search_bing = AsyncMock(return_value=[])
        self.web._search_duckduckgo = AsyncMock(return_value=[])
        with self.assertRaises(RuntimeError) as ctx:
            await self.web.search("research")
        self.assertNotIn("secret-key", str(ctx.exception))
        self.assertIn("All free search providers failed", str(ctx.exception))

    async def test_search_deadline_keeps_time_for_fallback(self):
        async def stall(*args):
            await asyncio.sleep(5)
        self.web._search_ddgs = stall
        self.web._search_bing = AsyncMock(return_value=[{"url": "https://example.org", "title": "Fallback"}])
        rows = await asyncio.wait_for(self.web.search("query"), 0.5)
        self.assertEqual(rows[0]["provider"], "bing-rss")

    async def test_fetch_reader_fallback_and_disk_cache(self):
        self.web._fetch_direct = AsyncMock(return_value=("Just a moment", "captcha" * 100))
        self.web._fetch_reader = AsyncMock(return_value=("Research", "Useful evidence with dates and context. " * 20))
        first = await self.web.fetch("https://example.org/article")
        second = await self.web.fetch("https://example.org/article?utm_campaign=test")
        self.assertEqual(first["provider"], "jina-reader")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.web._fetch_reader.assert_awaited_once()
        self.assertEqual(len(list(Path(self.temp.name).glob("*.json"))), 1)

    async def test_challenge_is_not_saved_as_evidence(self):
        self.web._fetch_direct = AsyncMock(return_value=("安全验证", "人机验证" * 100))
        self.web._fetch_reader = AsyncMock(return_value=("Access denied", "verify you are human " * 50))
        with self.assertRaises(RuntimeError):
            await self.web.fetch("https://example.org/blocked")
        self.assertFalse(list(Path(self.temp.name).glob("*.json")))

    async def test_duplicate_fetches_share_result(self):
        self.web._fetch_direct = AsyncMock(return_value=("Title", "Evidence. " * 100))
        results = await asyncio.gather(*(self.web.fetch("https://example.org/a") for _ in range(3)))
        self.web._fetch_direct.assert_awaited_once()
        self.assertEqual(sum(not r["cache_hit"] for r in results), 1)

    async def test_direct_extraction_removes_navigation(self):
        body = "这是带日期和出处的正文，包含值得保留的研究证据。" * 30
        self.web._get = AsyncMock(return_value=httpx.Response(
            200, text=f"<html><head><title>正文测试</title></head><body><nav>导航垃圾</nav><article><p>{body}</p></article></body></html>",
            headers={"content-type": "text/html"}, request=httpx.Request("GET", "https://example.org")))
        result = await self.web.fetch("https://example.org")
        self.assertIn("研究证据", result["text"])
        self.assertNotIn("导航垃圾", result["text"])


if __name__ == "__main__":
    unittest.main()
