"""Templating, tools and LLM providers used by the workflow engine."""
from __future__ import annotations

import ast
import ipaddress
import json
import operator
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

# ---------------------------------------------------------------- templating
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)(\?)?\s*\}\}")


class TemplateError(ValueError):
    pass


def lookup(state: dict[str, Any], path: str) -> Any:
    """Resolve dotted paths such as input.topic or nodes.planner.output."""
    current: Any = state
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise TemplateError(f"unknown reference '{path}'")
    return current


def render(template: str, state: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        try:
            value = lookup(state, match.group(1))
        except TemplateError:
            if match.group(2):  # {{path?}} renders empty when the reference does not exist yet
                return ""
            raise
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return _PLACEHOLDER.sub(replace, template)


def references(template: str) -> list[str]:
    return [m.group(1) for m in _PLACEHOLDER.finditer(template)]


# --------------------------------------------------------------------- tools
class ToolError(RuntimeError):
    pass


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _calc(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _calc(node.left), _calc(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ToolError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_calc(node.operand))
    raise ToolError("calculator accepts numbers and + - * / // % ** only")


def tool_calculator(expression: str) -> dict[str, Any]:
    try:
        value = _calc(ast.parse(str(expression), mode="eval"))
    except ZeroDivisionError as exc:
        raise ToolError("division by zero") from exc
    except SyntaxError as exc:
        raise ToolError(f"invalid expression: {expression!r}") from exc
    return {"expression": expression, "result": value}


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ToolError(f"cannot resolve host {host!r}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
            return False
    return True


def tool_http_get(url: str, max_chars: str | int = 4000) -> dict[str, Any]:
    parts = urlsplit(str(url))
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ToolError("http_get needs an absolute http(s) URL")
    if os.environ.get("STUDIO_ALLOW_PRIVATE_HTTP") != "1" and not _public_host(parts.hostname):
        raise ToolError("http_get refuses private/loopback addresses (set STUDIO_ALLOW_PRIVATE_HTTP=1 to allow)")
    try:
        response = httpx.get(str(url), timeout=15, follow_redirects=False, headers={"User-Agent": "m1-agent-studio/1"})
    except httpx.HTTPError as exc:
        raise ToolError(f"request failed: {type(exc).__name__}: {exc}") from exc
    text = response.text
    limit = max(200, min(int(max_chars), 20000))
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return {"url": str(url), "status_code": response.status_code, "content": text[:limit], "truncated": len(text) > limit}


def tool_word_count(text: str) -> dict[str, Any]:
    words = re.findall(r"\w+", str(text))
    return {"words": len(words), "characters": len(str(text))}


TOOLS: dict[str, dict[str, Any]] = {
    "calculator": {"fn": tool_calculator, "description": "Evaluate an arithmetic expression.",
                   "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},
    "http_get": {"fn": tool_http_get, "description": "Fetch a public web page and return its visible text.",
                 "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["url"]}},
    "word_count": {"fn": tool_word_count, "description": "Count words and characters in a text.",
                   "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
}


def run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError(f"unknown tool {name!r}")
    try:
        return spec["fn"](**arguments)
    except TypeError as exc:
        raise ToolError(f"invalid arguments for {name}: {exc}") from exc


def openai_tool_schema(name: str) -> dict[str, Any]:
    spec = TOOLS[name]
    return {"type": "function", "function": {"name": name, "description": spec["description"], "parameters": spec["parameters"]}}


# ----------------------------------------------------------------- providers
class ProviderError(RuntimeError):
    pass


class MissingCredentials(ProviderError):
    pass


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    raw_finish_reason: str | None = None


class OpenAICompatibleProvider:
    """Real model calls through any OpenAI-compatible Chat Completions endpoint."""
    name = "openai"

    def __init__(self, *, api_key: str | None, base_url: str, default_model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = default_model

    async def chat(self, messages: list[dict[str, Any]], *, model: str, temperature: float, max_tokens: int,
                   tools: list[dict[str, Any]] | None = None) -> ChatResult:
        if not self.api_key:
            raise MissingCredentials("OPENAI_API_KEY is not set; configure it in the environment or switch the node to the deterministic provider")
        from openai import AsyncOpenAI, OpenAIError

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=90, max_retries=1)
        started = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {"model": model or self.default_model, "messages": messages,
                                      "temperature": temperature, "max_tokens": max_tokens}
            if tools:
                kwargs["tools"] = tools
            response = await client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            raise ProviderError(f"{type(exc).__name__}: {str(exc)[:500]}") from exc
        finally:
            await client.close()
        choice = response.choices[0]
        calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments} for c in (choice.message.tool_calls or [])]
        usage = response.usage.model_dump() if response.usage else {}
        return ChatResult(content=choice.message.content or "", tool_calls=calls, usage=usage, model=response.model,
                          provider=self.name, latency_ms=int((time.perf_counter() - started) * 1000),
                          raw_finish_reason=choice.finish_reason)


class DeterministicProvider:
    """Scripted, offline provider. Output = node.mock_response rendered against state.

    Labeled 'deterministic' everywhere; never evidence of model-backed behaviour.
    """
    name = "deterministic"

    async def chat(self, messages: list[dict[str, Any]], *, script: str, state: dict[str, Any],
                   tool_calls: list[dict[str, Any]], round_index: int) -> ChatResult:
        started = time.perf_counter()
        if round_index < len(tool_calls):
            call = tool_calls[round_index]
            args = {k: render(str(v), state) for k, v in (call.get("arguments") or {}).items()}
            return ChatResult(content="", tool_calls=[{"id": f"det_{round_index}", "name": call["name"], "arguments": json.dumps(args)}],
                              model="deterministic-script", provider=self.name)
        tool_results = [m["content"] for m in messages if m.get("role") == "tool"]
        scope = {**state, "tool_results": tool_results}
        content = render(script, scope) if script else "Deterministic placeholder: " + str(messages[-1].get("content", ""))[:300]
        return ChatResult(content=content, model="deterministic-script", provider=self.name,
                          latency_ms=int((time.perf_counter() - started) * 1000), raw_finish_reason="stop")
