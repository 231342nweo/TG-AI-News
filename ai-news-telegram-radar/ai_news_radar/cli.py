from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from .core import NewsItem, collect_news, load_config, write_outputs
from .telegram import load_env, render_html, render_plain, send_telegram_message
from .translator import translate_items, translation_base_url, translation_enabled, translation_model


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch AI news feeds and optionally send a Telegram digest.")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="Fetch feeds and build/send a digest")
    run.add_argument("--config", default="config/sources.json", help="Path to source config JSON")
    run.add_argument("--output-dir", default="data", help="Directory for generated JSON/message files")
    run.add_argument("--env", default=".env", help="Path to .env file with Telegram settings")
    run.add_argument("--hours", type=int, default=None, help="Only keep items published within this window")
    run.add_argument("--limit", type=int, default=None, help="Maximum number of items in the digest")
    run.add_argument(
        "--translate",
        choices=["auto", "on", "off"],
        default="auto",
        help="Translate titles/summaries to Chinese with OpenAI when available",
    )
    run.add_argument("--send", action="store_true", help="Send the digest to Telegram")
    run.add_argument("--show", action="store_true", help="Print the plain-text digest preview")

    test = subparsers.add_parser("test-telegram", help="Send a short test message to Telegram")
    test.add_argument("--env", default=".env", help="Path to .env file with Telegram settings")
    test.add_argument("--message", default="中国 AI 新闻提醒测试消息：Telegram 频道已经接通。")

    translation = subparsers.add_parser("test-translation", help="Test OpenAI/LiteLLM Chinese translation")
    translation.add_argument("--env", default=".env", help="Path to .env file with translation settings")

    return parser


def run_digest(args: argparse.Namespace) -> int:
    load_env(args.env)
    generated_at = datetime.now(timezone.utc)
    config = load_config(args.config)
    output_dir = Path(args.output_dir)
    state_path = output_dir / "source-state.json" if args.send else None
    items, statuses = collect_news(
        config,
        now=generated_at,
        window_hours=args.hours,
        limit=args.limit,
        state_path=state_path,
    )

    if translation_enabled(args.translate):
        try:
            translated_count = translate_items(items)
            print(f"自动翻译完成：{translated_count} 条，模型：{translation_model()}，接口：{translation_base_url()}")
        except Exception as exc:  # noqa: BLE001 - translation should not break the news pipeline.
            if args.translate == "on":
                raise
            print(f"自动翻译失败，已使用原文标题和中文看点继续：{exc}")
    elif args.translate == "auto":
        print("未检测到 OPENAI_API_KEY，跳过自动翻译。")

    write_outputs(output_dir, items=items, statuses=statuses, generated_at=generated_at)

    html_message = render_html(items, generated_at=generated_at)
    plain_message = render_plain(items, generated_at=generated_at)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "telegram-message.html").write_text(html_message, encoding="utf-8")
    (output_dir / "telegram-message.txt").write_text(plain_message, encoding="utf-8")

    if args.show or not args.send:
        print(plain_message)

    failed = [status for status in statuses if not status.ok]
    if failed:
        print("部分信源抓取失败：")
        for status in failed:
            print(f"- {status.source_name}: {status.error}")

    if args.send:
        if not items:
            print("本轮没有新消息，已跳过 Telegram 发送。")
        else:
            send_telegram_message(html_message)
            print("Telegram 摘要已发送。")
    else:
        print("预览完成。配置好 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 后，加 --send 即可发送。")

    return 0


def test_telegram(args: argparse.Namespace) -> int:
    load_env(args.env)
    send_telegram_message(args.message)
    print("Telegram test message sent.")
    return 0


def test_translation(args: argparse.Namespace) -> int:
    load_env(args.env)
    item = NewsItem(
        title="OpenAI releases a new agent toolkit for developers",
        url="https://example.com/openai-agent-toolkit",
        summary="Developers can use the new AI agent toolkit to build safer workflows.",
        published_at="2026-07-02T01:00:00Z",
        source_id="demo",
        source_name="Demo",
        tags=["AI", "Agents", "Developer tools"],
        score=9.8,
    )
    try:
        count = translate_items([item])
    except Exception as exc:  # noqa: BLE001 - make config errors readable in double-click flows.
        print(f"翻译测试失败：{exc}")
        print("请检查 OPENAI_API_KEY、OPENAI_BASE_URL 和 OPENAI_TRANSLATION_MODEL。")
        return 1
    print(f"翻译测试完成：{count} 条")
    print(f"中文标题：{item.translated_title}")
    print(f"中文摘要：{item.translated_summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {None, "run"}:
        return run_digest(args)
    if args.command == "test-telegram":
        return test_telegram(args)
    if args.command == "test-translation":
        return test_translation(args)
    parser.error(f"unknown command: {args.command}")
    return 2
