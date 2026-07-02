from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from .core import NewsItem, parse_datetime


TELEGRAM_LIMIT = 4096
BEIJING_TZ = timezone(timedelta(hours=8), name="Beijing")

CONCEPTS = [
    (("agent", "agents", "agentic", "智能体"), "智能体"),
    (("benchmark", "eval", "evaluation", "评测"), "评测基准"),
    (("llm", "large language model", "language model", "大模型"), "大模型"),
    (("multimodal", "vision-language", "多模态"), "多模态"),
    (("reasoning", "推理"), "推理能力"),
    (("inference", "latency", "deploy", "deployment", "部署"), "推理和部署效率"),
    (("developer", "toolkit", "tools", "coding", "code", "开发者"), "开发者工具"),
    (("enterprise", "business", "productivity", "企业"), "企业应用"),
    (("health", "healthcare", "medical", "drug", "molecular", "医疗"), "医疗 AI"),
    (("education", "classroom", "learning", "教育"), "教育 AI"),
    (("robot", "robotic", "embodied", "机器人"), "机器人"),
    (("safety", "security", "安全"), "安全治理"),
    (("diffusion", "扩散"), "生成模型"),
    (("openai", "gpt"), "OpenAI"),
    (("anthropic", "claude"), "Anthropic/Claude"),
    (("google", "gemini", "deepmind"), "Google/Gemini"),
    (("cursor",), "Cursor"),
    (("hugging face",), "Hugging Face"),
]


def load_env(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def format_time(value: str | None) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "时间未知"
    return parsed.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M 北京时间")


def format_generated_time(value: datetime) -> str:
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M 北京时间")


def chinese_focus(item: NewsItem) -> str:
    haystack = f"{item.title}\n{item.summary}\n{' '.join(item.tags)}".lower()
    matched: list[str] = []
    for keywords, label in CONCEPTS:
        if any(keyword in haystack for keyword in keywords) and label not in matched:
            matched.append(label)
        if len(matched) >= 4:
            break

    if not matched:
        matched.append("AI 行业动态")

    if "arxiv" in item.source_id.lower():
        prefix = "论文信号"
    elif any(word in haystack for word in ("release", "launch", "announced", "update", "发布", "上线", "更新")):
        prefix = "产品/发布信号"
    elif any(word in haystack for word in ("benchmark", "eval", "research", "study", "研究", "评测")):
        prefix = "研究信号"
    else:
        prefix = "资讯信号"

    return f"{prefix}：重点关注{'、'.join(matched)}。"


def display_title(item: NewsItem) -> str:
    return item.translated_title or item.title


def display_summary(item: NewsItem) -> str:
    return item.translated_summary or chinese_focus(item)


def render_plain(items: list[NewsItem], generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        f"中国 AI 新闻提醒 | {format_generated_time(generated_at)}",
        f"本次筛出 {len(items)} 条 AI 信号",
        "",
    ]
    if not items:
        lines.append("本轮没有新消息。")
        return "\n".join(lines).strip() + "\n"

    for index, item in enumerate(items, 1):
        lines.append(f"{index}. {display_title(item)}")
        lines.append(f"   来源：{item.source_name} | 评分：{item.score} | 时间：{format_time(item.published_at)}")
        label = "中文摘要" if item.translated_summary else "中文看点"
        lines.append(f"   {label}：{display_summary(item)}")
        if item.url:
            lines.append(f"   原文：{item.url}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_html(items: list[NewsItem], generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    parts = [
        f"<b>中国 AI 新闻提醒</b> | {escape(format_generated_time(generated_at))}",
        f"本次筛出 <b>{len(items)}</b> 条 AI 信号",
        "",
    ]
    if not items:
        parts.append("本轮没有新消息。")
        return "\n".join(parts).strip() + "\n"

    for index, item in enumerate(items, 1):
        title = escape(display_title(item))
        if item.url:
            headline = f'<b>{index}. <a href="{escape(item.url, quote=True)}">{title}</a></b>'
        else:
            headline = f"<b>{index}. {title}</b>"
        parts.append(headline)
        parts.append(
            f"来源：<code>{escape(item.source_name)}</code> · 评分：{item.score} · 时间：{escape(format_time(item.published_at))}"
        )
        label = "中文摘要" if item.translated_summary else "中文看点"
        parts.append(f"{label}：{escape(display_summary(item))}")
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def chunk_message(message: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in message.split("\n\n"):
        block_len = len(block) + 2
        if current and current_len + block_len > limit:
            chunks.append("\n\n".join(current).strip() + "\n")
            current = [block]
            current_len = block_len
        else:
            current.append(block)
            current_len += block_len
    if current:
        chunks.append("\n\n".join(current).strip() + "\n")
    return chunks


def send_telegram_message(
    message: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
    disable_preview: bool = True,
) -> list[dict]:
    bot_token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    target_chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not target_chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required to send messages")

    results = []
    for chunk in chunk_message(message):
        data = urllib.parse.urlencode(
            {
                "chat_id": target_chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true" if disable_preview else "false",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not payload.get("ok"):
                raise RuntimeError(f"telegram send failed: {payload}")
            results.append(payload)
    return results
