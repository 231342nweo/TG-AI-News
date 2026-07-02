from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tempfile import TemporaryDirectory

from ai_news_radar.cli import run_digest
from ai_news_radar.core import NewsItem, Source, collect_news, is_noise_item, parse_feed, parse_html_list
from ai_news_radar.telegram import chinese_focus, chunk_message, format_time, render_html
from ai_news_radar.translator import apply_translation_payload, extract_response_text, parse_json_object


ROOT = Path(__file__).resolve().parents[1]


class CoreTest(unittest.TestCase):
    def test_parse_rss_feed(self) -> None:
        source = Source(id="demo", name="Demo", url=str(ROOT / "tests/fixtures/demo-feed.xml"))
        items = parse_feed((ROOT / "tests/fixtures/demo-feed.xml").read_text(encoding="utf-8"), source)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "OpenAI releases a new agent toolkit for developers")
        self.assertEqual(items[0].published_at, "2026-07-02T01:00:00Z")

    def test_collect_news_filters_and_ranks(self) -> None:
        config = {
            "settings": {"window_hours": 48, "limit": 5, "min_score": 2},
            "keywords": ["ai", "agent", "openai", "claude", "gemini", "model"],
            "sources": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "url": str(ROOT / "tests/fixtures/demo-feed.xml"),
                    "weight": 2,
                },
                {
                    "id": "atom",
                    "name": "Atom",
                    "url": str(ROOT / "tests/fixtures/demo-feed.atom"),
                    "weight": 2,
                },
            ],
        }

        items, statuses = collect_news(config, now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc))

        self.assertTrue(all(status.ok for status in statuses))
        self.assertGreaterEqual(len(items), 3)
        self.assertIn("agent", items[0].title.lower())
        self.assertFalse(any("coffee" in item.title.lower() for item in items))

    def test_telegram_message_chunks(self) -> None:
        config = {
            "settings": {"window_hours": 48, "limit": 5, "min_score": 2},
            "keywords": ["ai", "agent", "openai", "claude", "gemini", "model"],
            "sources": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "url": str(ROOT / "tests/fixtures/demo-feed.xml"),
                    "weight": 2,
                }
            ],
        }
        items, _ = collect_news(config, now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc))
        message = render_html(items, generated_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc))

        self.assertIn("<b>中国 AI 新闻提醒</b>", message)
        self.assertIn("北京时间", message)
        self.assertIn("中文看点", message)
        self.assertEqual(len(chunk_message(message, limit=4096)), 1)

    def test_time_is_rendered_in_beijing_timezone(self) -> None:
        self.assertEqual(format_time("2026-07-02T12:00:00Z"), "2026-07-02 20:00 北京时间")

    def test_chinese_focus_extracts_concepts(self) -> None:
        config = {
            "settings": {"window_hours": 48, "limit": 1, "min_score": 2},
            "keywords": ["agent", "benchmark"],
            "sources": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "url": str(ROOT / "tests/fixtures/demo-feed.xml"),
                    "weight": 2,
                }
            ],
        }
        items, _ = collect_news(config, now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc))

        self.assertIn("智能体", chinese_focus(items[0]))

    def test_render_prefers_translated_title_and_summary(self) -> None:
        item = NewsItem(
            title="OpenAI releases a new agent toolkit for developers",
            url="https://example.com/openai-agent-toolkit",
            summary="Developers can use the new AI agent toolkit to build safer workflows.",
            published_at="2026-07-02T01:00:00Z",
            source_id="demo",
            source_name="Demo",
            tags=["AI"],
            score=9.8,
            translated_title="OpenAI 发布新的开发者智能体工具包",
            translated_summary="OpenAI 推出面向开发者的智能体工具包，帮助构建更安全的自动化工作流。",
        )

        message = render_html([item], generated_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc))

        self.assertIn("OpenAI 发布新的开发者智能体工具包", message)
        self.assertIn("中文摘要", message)
        self.assertNotIn("OpenAI releases a new agent toolkit", message)

    def test_render_empty_digest_says_no_new_messages(self) -> None:
        html_message = render_html([], generated_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc))

        self.assertIn("本次筛出 <b>0</b> 条 AI 信号", html_message)
        self.assertIn("本轮没有新消息。", html_message)

    def test_apply_translation_payload(self) -> None:
        item = NewsItem(
            title="Gemini multimodal model adds faster inference",
            url="https://example.com/gemini",
            summary="The model update focuses on multimodal latency.",
            published_at="2026-07-02T01:00:00Z",
            source_id="demo",
            source_name="Demo",
            tags=["Model"],
            score=8.0,
        )
        raw_response = {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"items":[{"index":0,"title_zh":"Gemini 多模态模型提升推理速度","summary_zh":"Gemini 更新聚焦多模态延迟优化，适合关注模型部署效率。"}]}',
                        }
                    ]
                }
            ]
        }

        text = extract_response_text(raw_response)
        count = apply_translation_payload([item], parse_json_object(text))

        self.assertEqual(count, 1)
        self.assertEqual(item.translated_title, "Gemini 多模态模型提升推理速度")
        self.assertIn("多模态", item.translated_summary or "")

    def test_extract_chat_completion_response_text(self) -> None:
        raw_response = {
            "choices": [
                {
                    "message": {
                        "content": '{"items":[{"index":0,"title_zh":"中文标题","summary_zh":"中文摘要"}]}'
                    }
                }
            ]
        }

        self.assertIn("中文标题", extract_response_text(raw_response))

    def test_filters_low_value_github_activity(self) -> None:
        item = NewsItem(
            title="QwenLM added xionghuichen to QwenLM/Qwen-RobotNav",
            url="https://github.com/QwenLM/Qwen-RobotNav",
            summary="QwenLM added xionghuichen to QwenLM/Qwen-RobotNav",
            published_at=None,
            source_id="qwen-github",
            source_name="Qwen GitHub",
            tags=[],
            score=0,
        )

        self.assertTrue(is_noise_item(item))

    def test_unsupported_enabled_method_is_skipped(self) -> None:
        config = {
            "settings": {"window_hours": 48, "limit": 5, "min_score": 1},
            "keywords": ["ai"],
            "sources": [
                {
                    "id": "future-api",
                    "name": "Future API Source",
                    "url": "https://example.com",
                    "method": "future_api",
                    "weight": 2,
                }
            ],
        }

        items, statuses = collect_news(config, now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc))

        self.assertEqual(items, [])
        self.assertEqual(statuses[0].error, "unsupported method: future_api")

    def test_html_list_uses_state_baseline_before_emitting(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "list.html"
            state = root / "state.json"
            page.write_text(
                '<html><body><a href="https://example.com/ai-model">AI 模型发布</a></body></html>',
                encoding="utf-8",
            )
            config = {
                "settings": {"window_hours": 48, "limit": 5, "min_score": 1},
                "keywords": ["AI", "模型"],
                "sources": [
                    {
                        "id": "html-list",
                        "name": "HTML List",
                        "url": str(page),
                        "method": "html_list",
                        "weight": 2,
                    }
                ],
            }

            first_items, first_statuses = collect_news(
                config,
                now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
                state_path=state,
            )
            second_html = (
                '<html><body>'
                '<a href="https://example.com/ai-model">AI 模型发布</a>'
                '<a href="https://example.com/new-agent">新智能体模型发布</a>'
                "</body></html>"
            )
            page.write_text(second_html, encoding="utf-8")
            second_items, _ = collect_news(
                config,
                now=datetime(2026, 7, 2, 13, tzinfo=timezone.utc),
                state_path=state,
            )

            self.assertEqual(first_items, [])
            self.assertTrue(first_statuses[0].ok)
            self.assertEqual(len(second_items), 1)
            self.assertEqual(second_items[0].title, "新智能体模型发布")

    def test_html_list_uses_aria_label_for_empty_links(self) -> None:
        source = Source(id="qwen-blog", name="Qwen Blog", url="https://qwenlm.github.io/blog/", method="html_list")
        html = """
        <article class=post-entry>
          <header class=entry-header><h2>Qwen3Guard: Real-time Safety for Your Token Stream</h2></header>
          <a class=entry-link aria-label="post link to Qwen3Guard: Real-time Safety for Your Token Stream" href=https://qwenlm.github.io/blog/qwen3guard/></a>
        </article>
        """

        items = parse_html_list(html, source)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Qwen3Guard: Real-time Safety for Your Token Stream")

    def test_html_list_filters_qwen_navigation_links(self) -> None:
        source = Source(id="qwen-blog", name="Qwen Blog", url="https://qwenlm.github.io/blog/", method="html_list")
        html = """
        <a href="https://qwenlm.github.io/">Qwen (Alt + H)</a>
        <a href="https://qwenlm.github.io/zh/blog/">简体中文</a>
        <a aria-label="post link to Qwen-Image-Edit: Image Editing with Higher Quality and Efficiency" href="https://qwenlm.github.io/blog/qwen-image-edit/"></a>
        """

        items = parse_html_list(html, source)

        self.assertEqual([item.title for item in items], ["Qwen-Image-Edit: Image Editing with Higher Quality and Efficiency"])

    def test_html_list_filters_kimi_forum_navigation_links(self) -> None:
        source = Source(
            id="kimi-forum",
            name="Kimi 论坛公告",
            url="https://forum.moonshot.ai/c/announcement/5",
            method="html_list",
        )
        html = """
        <a href="/c/announcement/5">Announcement</a>
        <a href="/u/Cellen">Cellen</a>
        <a href="/tag/kimi-k27/25">Kimi K27</a>
        <a href="/t/here-comes-kimi-k2-7-code-better-coding-with-more-efficiency/441">Here Comes Kimi K2 7 Code Better Coding With More Efficiency</a>
        """

        items = parse_html_list(html, source)

        self.assertEqual(
            [item.url for item in items],
            ["https://forum.moonshot.ai/t/here-comes-kimi-k2-7-code-better-coding-with-more-efficiency/441"],
        )

    def test_policy_keyword_match_ignores_synthetic_source_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            page = Path(tmp) / "policy.html"
            page.write_text(
                '<a href="https://www.cac.gov.cn/gzzt/ztzl/yjzt/xjpsx/">学习习近平新时代中国特色社会主义思想</a>',
                encoding="utf-8",
            )

            items = collect_news(
                {
                    "settings": {"window_hours": 48, "limit": 5, "min_score": 1},
                    "keywords": ["人工智能", "AI", "政策"],
                    "sources": [
                        {
                            "id": "policy",
                            "name": "国家网信办生成式 AI 备案公告",
                            "url": str(page),
                            "method": "policy_keyword_html",
                        }
                    ],
                },
                now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
            )[0]

            self.assertEqual(items, [])

    def test_html_list_extracts_embedded_nuxt_blog_items(self) -> None:
        source = Source(id="openbmb-website", name="OpenBMB 官网", url="https://openbmb.cn/", method="html_list")
        html = (
            '<script>window.__NUXT__={data:[{content:"今天，我们发布新版本",'
            'title:"基座上新：MiniCPM 4.1 将「高效深思考」引入端侧",'
            'create_time:"2025年10月29 16:00",'
            'uuid:"40072c93d9e14054a871fb6e84aca8ad"}]}</script>'
        )

        items = parse_html_list(html, source)

        self.assertEqual(len(items), 1)
        self.assertIn("MiniCPM 4.1", items[0].title)

    def test_html_diff_uses_state_baseline_before_emitting(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "diff.html"
            state = root / "state.json"
            page.write_text(
                "<html><body>测试模型平台 API 文档，包含模型列表、上下文窗口、价格和发布说明 v1。</body></html>",
                encoding="utf-8",
            )
            config = {
                "settings": {"window_hours": 48, "limit": 5, "min_score": 1},
                "keywords": ["人工智能", "模型"],
                "sources": [
                    {
                        "id": "html-diff",
                        "name": "HTML Diff",
                        "entity": "测试模型平台",
                        "category": "model_company",
                        "url": str(page),
                        "method": "html_diff",
                        "push_rule": "immediate_if_model_change",
                        "weight": 2,
                    }
                ],
            }

            first_items, _ = collect_news(
                config,
                now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
                state_path=state,
            )
            page.write_text(
                "<html><body>测试模型平台 API 文档，包含模型列表、上下文窗口、价格和发布说明 v2，新增 AI 模型。</body></html>",
                encoding="utf-8",
            )
            second_items, _ = collect_news(
                config,
                now=datetime(2026, 7, 2, 13, tzinfo=timezone.utc),
                state_path=state,
            )

            self.assertEqual(first_items, [])
            self.assertEqual(len(second_items), 1)
            self.assertIn("页面发生更新", second_items[0].title)

    def test_feed_sources_use_state_to_avoid_reposting(self) -> None:
        with TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            config = {
                "settings": {"window_hours": 720, "limit": 5, "min_score": 1},
                "keywords": ["agent", "openai"],
                "sources": [
                    {
                        "id": "demo-rss",
                        "name": "Demo RSS",
                        "url": str(ROOT / "tests/fixtures/demo-feed.xml"),
                        "method": "rss",
                        "weight": 2,
                    }
                ],
            }

            first_items, _ = collect_news(
                config,
                now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
                state_path=state,
            )
            second_items, _ = collect_news(
                config,
                now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
                state_path=state,
            )

            self.assertEqual(first_items, [])
            self.assertEqual(second_items, [])

    def test_github_repos_api_uses_state_baseline_before_emitting(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repos = root / "repos.json"
            state = root / "state.json"
            repos.write_text(
                """
                [
                  {
                    "full_name": "OpenXLab-APP/MindSearch",
                    "html_url": "https://github.com/OpenXLab-APP/MindSearch",
                    "description": "AI search agent demo",
                    "pushed_at": "2026-07-02T01:00:00Z",
                    "language": "Python",
                    "topics": ["ai", "agent"]
                  }
                ]
                """,
                encoding="utf-8",
            )
            config = {
                "settings": {"window_hours": 720, "limit": 5, "min_score": 1},
                "keywords": ["ai", "agent", "openxlab"],
                "sources": [
                    {
                        "id": "openxlab",
                        "name": "OpenXLab",
                        "entity": "上海 AI Lab / OpenXLab",
                        "category": "open_source",
                        "url": str(repos),
                        "method": "github_repos_api",
                        "weight": 2,
                    }
                ],
            }

            first_items, _ = collect_news(
                config,
                now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
                state_path=state,
            )
            repos.write_text(
                """
                [
                  {
                    "full_name": "OpenXLab-APP/MindSearch",
                    "html_url": "https://github.com/OpenXLab-APP/MindSearch",
                    "description": "AI search agent demo",
                    "pushed_at": "2026-07-02T05:00:00Z",
                    "language": "Python",
                    "topics": ["ai", "agent"]
                  }
                ]
                """,
                encoding="utf-8",
            )
            second_items, _ = collect_news(
                config,
                now=datetime(2026, 7, 2, 13, tzinfo=timezone.utc),
                state_path=state,
            )

            self.assertEqual(first_items, [])
            self.assertEqual(len(second_items), 1)
            self.assertIn("OpenXLab-APP/MindSearch", second_items[0].title)

    def test_send_mode_skips_empty_digest(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "sources.json"
            output_dir = root / "data"
            config.write_text(
                '{"settings":{"window_hours":48,"limit":5},"keywords":[],"sources":[]}',
                encoding="utf-8",
            )
            args = SimpleNamespace(
                config=str(config),
                output_dir=str(output_dir),
                env=str(root / ".env"),
                hours=None,
                limit=None,
                translate="off",
                send=True,
                show=False,
            )

            with patch("ai_news_radar.cli.send_telegram_message") as send:
                result = run_digest(args)

            self.assertEqual(result, 0)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
