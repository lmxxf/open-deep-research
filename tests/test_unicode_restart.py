"""Regression tests for broken UTF-8 terminal edits and retrying failed runs."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from deep_research.cli import require_empty, main
from deep_research.engine import Config, Research
from deep_research.llm import LLM
from deep_research.metrics import Metrics, save_json
from deep_research.text import clean_surrogates, write_text
from deep_research.web import Web


BROKEN = b"\xe4\xb8".decode("utf-8", "surrogateescape")  # 中 with last byte deleted


def test_recursive_cleaning_and_utf8_files(tmp_path):
    original = {"键"+BROKEN: ["中文😀"+BROKEN, ("前\ud800后", 3, None)]}
    cleaned = clean_surrogates(original)
    assert cleaned == {"键": ["中文😀", ("前后", 3, None)]}
    assert original != cleaned
    save_json(tmp_path/"data.json", original)
    assert json.loads((tmp_path/"data.json").read_text()) == {"键": ["中文😀", ["前后", 3, None]]}
    write_text(tmp_path/"report.md", "正文"+BROKEN)
    assert (tmp_path/"report.md").read_text() == "正文"
    metrics = Metrics(tmp_path)
    event = metrics.start("fetch", "网页"+BROKEN)
    metrics.end(event, note="条件"+BROKEN)
    metrics.finish()
    (tmp_path/"events.jsonl").read_text().encode("utf-8")


def test_cli_cleans_argv_before_parsing(tmp_path):
    captured = []
    async def execute(args):
        captured.append(args.question)
        return 0
    with patch("sys.argv", ["deep_research", "run", "--question", "中文"+BROKEN,
                            "--output", str(tmp_path/"run")]), patch("deep_research.cli.execute", execute):
        assert main() == 0
    assert captured == ["中文"]


def test_http_request_response_and_logs_are_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test")
    monkeypatch.setattr("deep_research.llm.load_dotenv", lambda *a, **k: None)
    async def run():
        client = LLM(Metrics(tmp_path))
        await client.http.aclose()
        def handler(request):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["messages"][0]["content"] == "系统"
            assert json.loads(payload["messages"][1]["content"]) == {"问题": "中文"}
            return httpx.Response(200, json={"model": "fake", "choices": [{
                "finish_reason": "stop", "message": {"content": '{"answer":"正常\\udc80"}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result, _ = await client.ask("stage"+BROKEN, "系统"+BROKEN, {"问题": "中文"+BROKEN})
            assert result == {"answer": "正常"}
        finally:
            await client.close()
        for p in tmp_path.glob("*.jsonl"):
            for line in p.read_text().splitlines():
                json.loads(line)
    asyncio.run(run())


def test_web_text_is_clean_before_hash_cache_and_return(tmp_path):
    async def run():
        web = Web(tmp_path)
        web._fetch_direct = AsyncMock(return_value=("标题"+BROKEN, "有效中文资料。"*30+BROKEN))
        try:
            result = await web.fetch("https://example.org/page")
            assert result["title"] == "标题"
            result["text"].encode("utf-8")
            assert (await web.fetch(result["url"]))["cache_hit"]
        finally:
            await web.close()
    asyncio.run(run())


def test_interactive_answer_is_clean_before_next_model_call(tmp_path):
    async def run():
        with patch("deep_research.engine.LLM"), patch("deep_research.engine.Web"):
            runner = Research(tmp_path, Config())
        runner.llm.model = "fake"
        runner.config.model = "fake"
        async def ask(name, system, payload, parents=()):
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if name == "align":
                return {"questions": ["给谁看？"], "plan": [], "assumptions": []}, "n1"
            assert "负责人" in payload["brief"]
            assert BROKEN not in payload["brief"]
            raise RuntimeError("stop after input validation")
        runner.llm.ask = ask
        with patch("builtins.input", return_value="负责人"+BROKEN):
            with pytest.raises(RuntimeError, match="stop after input validation"):
                await runner.run("问题"+BROKEN, interactive=True)
    asyncio.run(run())


@pytest.mark.parametrize("filename,status", [("metrics.json", "failed"), ("state.json", "needs_alignment"),
                                           ("failure.json", "failed")])
def test_restart_clears_only_incomplete_runs(tmp_path, filename, status):
    output = tmp_path/"try1"
    output.mkdir()
    save_json(output/filename, {"status": status})
    (output/"events.jsonl").write_text("old log")
    (output/"pages").mkdir()
    (output/"pages"/"S1.txt").write_text("old page")
    require_empty(output, restart_incomplete=True)
    assert not output.exists()
    Metrics(output)  # Same path is immediately reusable, no manual rm needed.


@pytest.mark.parametrize("status", ["complete", "running", "alignment_only", "unknown"])
def test_successful_or_unknown_runs_are_not_erased(tmp_path, status):
    save_json(tmp_path/"metrics.json", {"status": status})
    save_json(tmp_path/"failure.json", {"status": "failed"})
    with pytest.raises(ValueError, match="不覆盖"):
        require_empty(tmp_path, restart_incomplete=True)
    assert (tmp_path/"metrics.json").exists()


def test_export_default_and_unrelated_files_are_not_erased(tmp_path):
    save_json(tmp_path/"metrics.json", {"status": "failed"})
    with pytest.raises(ValueError):
        require_empty(tmp_path)  # Export never opts into cleanup.
    (tmp_path/"my-notes.md").write_text("user file")
    with pytest.raises(ValueError):
        require_empty(tmp_path, restart_incomplete=True)
    assert (tmp_path/"my-notes.md").exists()
