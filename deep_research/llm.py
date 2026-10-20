"""Minimal DeepSeek Chat Completions client. Never log headers or API keys."""

import asyncio
import json
import os
from pathlib import Path

from .text import clean_surrogates, dumps

import httpx
from dotenv import load_dotenv

from .metrics import Metrics


class ModelError(RuntimeError):
    pass


class LLM:
    def __init__(self, metrics: Metrics, model=None, thinking=False, concurrency=3,
                 max_calls=32, timeout=120, max_tokens=8192):
        # Resolve from the project, never from an unrelated shell cwd.
        # The project's explicit .env wins over unrelated inherited shell credentials.
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
        self.key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.key:
            raise ModelError("请在项目 .env 中设置 DEEPSEEK_API_KEY")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
        self.base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.metrics, self.thinking = metrics, thinking
        self.max_calls, self.calls, self.max_tokens = max_calls, 0, max_tokens
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(concurrency)
        self.http = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self.http.aclose()

    async def ask(self, name, system, payload, parents=()):
        name, system, payload = clean_surrogates((name, system, payload))
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": dumps(payload, ensure_ascii=False)}]
        request = dict(model=self.model, messages=messages,
                       thinking={"type": "enabled" if self.thinking else "disabled"},
                       max_tokens=self.max_tokens, response_format={"type": "json_object"})
        if self.thinking:
            request["reasoning_effort"] = "high"
        request = clean_surrogates(request)
        # Actual prompts are available locally for audit, without authorization data.
        with (self.metrics.directory / "prompts.jsonl").open("a", encoding="utf-8") as f:
            f.write(dumps(dict(stage=name, request=request), ensure_ascii=False)+"\n")
        previous = list(parents)
        for attempt in range(2):
            async with self.semaphore:
                if self.calls >= self.max_calls:
                    raise ModelError(f"模型调用预算已用完（{self.max_calls}）")
                self.calls += 1
                event = self.metrics.start("model", name, previous, attempt=attempt+1,
                                           requested_model=self.model, thinking=self.thinking)
                try:
                    response = await asyncio.wait_for(self.http.post(
                        self.base + "/chat/completions", json=request,
                        headers={"Authorization": "Bearer " + self.key}), self.timeout)
                    if response.status_code != 200:
                        # Response bodies may echo request data; do not save them in logs.
                        raise ModelError(f"DeepSeek HTTP {response.status_code}")
                    result = clean_surrogates(response.json())
                    choice = result["choices"][0]
                    usage = result.get("usage", {})
                    if usage and not self.thinking:
                        usage.setdefault("completion_tokens_details", {}).setdefault("reasoning_tokens", 0)
                    if choice.get("finish_reason") == "length":
                        self.metrics.end(event, "truncated", usage=usage, response_model=result.get("model"))
                        raise ModelError("模型输出达到上限；请增加 --max-tokens 或缩小任务（本次不会把截断稿当成功）")
                    content = choice["message"].get("content") or ""
                    with (self.metrics.directory / "model_outputs.jsonl").open("a", encoding="utf-8") as f:
                        f.write(dumps(dict(event_id=event, stage=name, response_id=result.get("id"),
                                                content=content.replace(self.key, "[REDACTED]")), ensure_ascii=False)+"\n")
                    try:
                        data = clean_surrogates(json.loads(content))
                        if not isinstance(data, dict):
                            raise ValueError("not an object")
                    except (ValueError, TypeError):
                        self.metrics.end(event, "invalid_json", usage=usage, response_model=result.get("model"))
                        raise ModelError("模型未返回有效 JSON 对象")
                    self.metrics.end(event, usage=usage, response_model=result.get("model"),
                                     response_id=result.get("id"))
                    return data, event
                except asyncio.CancelledError:
                    self.metrics.end(event, "cancelled")
                    raise
                except (httpx.HTTPError, asyncio.TimeoutError, ModelError, KeyError, ValueError) as exc:
                    current = next(e for e in self.metrics.events if e["id"] == event)
                    if current["status"] == "running":
                        self.metrics.end(event, "error", error_type=type(exc).__name__)
                    # Retry transient transport/server failures once, not authentication or truncation.
                    transient = isinstance(exc, (httpx.TransportError, asyncio.TimeoutError)) or (
                        isinstance(exc, ModelError) and any(str(code) in str(exc) for code in (429, 500, 502, 503, 504)))
                    if attempt or not transient:
                        if isinstance(exc, ModelError):
                            raise
                        raise ModelError(f"模型请求失败：{type(exc).__name__}") from None
                    previous = [event]
            await asyncio.sleep(1)
        raise ModelError("模型调用失败")
