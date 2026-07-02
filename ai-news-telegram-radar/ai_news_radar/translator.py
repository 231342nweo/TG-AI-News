from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from .core import NewsItem


DEFAULT_TRANSLATION_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def translation_api_key() -> str | None:
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    return value or None


def translation_model() -> str:
    return os.environ.get("OPENAI_TRANSLATION_MODEL", DEFAULT_TRANSLATION_MODEL).strip() or DEFAULT_TRANSLATION_MODEL


def translation_base_url() -> str:
    raw = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or DEFAULT_OPENAI_BASE_URL
    )
    base_url = raw.strip().rstrip("/")
    if base_url.endswith("/ui"):
        base_url = base_url[:-3].rstrip("/")
    return base_url


def translation_api_mode() -> str:
    mode = os.environ.get("OPENAI_TRANSLATION_API", "chat").strip().lower()
    if mode not in {"chat", "responses"}:
        return "chat"
    return mode


def translation_enabled(mode: str = "auto") -> bool:
    normalized = mode.lower()
    if normalized == "off":
        return False
    if normalized == "on":
        return True
    env_value = os.environ.get("OPENAI_TRANSLATION_ENABLED", "auto").strip().lower()
    if env_value in {"0", "false", "no", "off"}:
        return False
    return translation_api_key() is not None


def compact_item(item: NewsItem, index: int) -> dict[str, Any]:
    summary = item.summary
    if len(summary) > 700:
        summary = summary[:700].rstrip() + "..."
    return {
        "index": index,
        "title": item.title,
        "summary": summary,
        "source": item.source_name,
        "tags": item.tags[:8],
    }


def build_translation_prompt(items: list[NewsItem]) -> str:
    payload = [compact_item(item, index) for index, item in enumerate(items)]
    return (
        "你是给中文 Telegram AI 新闻频道使用的专业科技新闻翻译编辑。\n"
        "任务：把输入新闻的 title 和 summary 翻译/改写成简体中文。\n"
        "要求：\n"
        "1. 保留公司名、产品名、模型名、论文名中的专有名词，不要乱译。\n"
        "2. 标题要像中文科技媒体标题，准确、短、不要夸张。\n"
        "3. 摘要用 1 句话，60 到 90 个中文字以内；没有摘要时，根据标题、来源和标签写一句谨慎概括。\n"
        "4. 不添加输入里没有的事实，不给投资建议，不写营销话术。\n"
        "5. 只输出 JSON，不要 Markdown，不要解释。\n"
        'JSON 格式必须是：{"items":[{"index":0,"title_zh":"...","summary_zh":"..."}]}\n\n'
        "输入：\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def build_translation_system_prompt() -> str:
    return (
        "你是给中文 Telegram AI 新闻频道使用的专业科技新闻翻译编辑。"
        "你只输出 JSON，不输出 Markdown，不解释。"
    )


def extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    parts: list[str] = []
    for output in payload.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                parts.append(content["text"])
    if parts:
        return "\n".join(parts).strip()

    choices = payload.get("choices", [])
    if choices and isinstance(choices, list):
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message", {})
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"].strip()
    return "\n".join(parts).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def request_translation(
    items: list[NewsItem],
    *,
    api_key: str,
    model: str,
    timeout: int = 60,
) -> dict[str, Any]:
    if translation_api_mode() == "responses":
        return request_translation_responses(items, api_key=api_key, model=model, timeout=timeout)
    return request_translation_chat(items, api_key=api_key, model=model, timeout=timeout)


def request_translation_chat(
    items: list[NewsItem],
    *,
    api_key: str,
    model: str,
    timeout: int = 60,
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_translation_system_prompt()},
            {"role": "user", "content": build_translation_prompt(items)},
        ],
    }
    return post_with_fallbacks(
        body,
        api_key=api_key,
        timeout=timeout,
        paths=("chat/completions", "v1/chat/completions"),
    )


def request_translation_responses(
    items: list[NewsItem],
    *,
    api_key: str,
    model: str,
    timeout: int = 60,
) -> dict[str, Any]:
    body = {
        "model": model,
        "input": build_translation_prompt(items),
    }
    return post_with_fallbacks(
        body,
        api_key=api_key,
        timeout=timeout,
        paths=("responses", "v1/responses"),
    )


def post_with_fallbacks(
    body: dict[str, Any],
    *,
    api_key: str,
    timeout: int,
    paths: tuple[str, str],
) -> dict[str, Any]:
    base_url = translation_base_url()
    candidates = [f"{base_url}/{paths[0]}"]
    if not base_url.endswith("/v1"):
        candidates.append(f"{base_url}/{paths[1]}")

    last_detail = ""
    for index, url in enumerate(candidates):
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_detail = f"HTTP {exc.code} {detail}"
            if exc.code == 404 and index + 1 < len(candidates):
                continue
            raise RuntimeError(f"翻译接口返回错误：{last_detail}") from exc
    raise RuntimeError(f"翻译接口返回错误：{last_detail or 'unknown error'}")


def apply_translation_payload(items: list[NewsItem], payload: dict[str, Any]) -> int:
    translated = payload.get("items", [])
    if not isinstance(translated, list):
        raise ValueError("翻译结果 JSON 缺少 items 数组")

    count = 0
    for raw in translated:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(items):
            continue
        title = str(raw.get("title_zh", "")).strip()
        summary = str(raw.get("summary_zh", "")).strip()
        if title and has_cjk(title):
            items[index].translated_title = title
        if summary and has_cjk(summary):
            items[index].translated_summary = summary
        if title or summary:
            count += 1
    return count


def translate_items(
    items: list[NewsItem],
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout: int = 60,
) -> int:
    if not items:
        return 0

    key = api_key or translation_api_key()
    if not key:
        raise RuntimeError("缺少 OPENAI_API_KEY，无法自动翻译")

    response = request_translation(
        items,
        api_key=key,
        model=model or translation_model(),
        timeout=timeout,
    )
    text = extract_response_text(response)
    if not text:
        raise RuntimeError("OpenAI 翻译接口没有返回文本")
    payload = parse_json_object(text)
    return apply_translation_payload(items, payload)
