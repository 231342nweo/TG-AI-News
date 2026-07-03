from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config/sources.json"


class SourcesCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        cls.sources = cls.catalog["sources"]

    def test_china_ai_catalog_shape(self) -> None:
        self.assertEqual(self.catalog.get("catalog_version"), "2026-07-02")
        self.assertEqual(len(self.sources), 63)

        required = {"entity", "category", "url", "method", "priority", "push_rule"}
        for source in self.sources:
            missing = required - source.keys()
            self.assertFalse(missing, f"{source.get('id')} missing {sorted(missing)}")

    def test_ab_collectors_are_enabled(self) -> None:
        enabled = [source for source in self.sources if source.get("enabled", True)]
        disabled = [source for source in self.sources if not source.get("enabled", True)]
        enabled_methods = {source["method"] for source in enabled}

        self.assertEqual(len(enabled), 63)
        self.assertEqual(disabled, [])
        self.assertEqual(
            enabled_methods,
            {
                "github_repos_api",
                "html_diff",
                "html_list",
                "huggingface_api",
                "modelscope_html",
                "policy_keyword_html",
                "rss",
                "tikhub_wechat_search",
            },
        )

    def test_media_sources_are_intentionally_limited(self) -> None:
        media = [source for source in self.sources if source.get("category") == "media"]

        self.assertEqual({source["name"] for source in media}, {"机器之心", "量子位", "Founder Park"})
        founder_park = next(source for source in media if source["name"] == "Founder Park")
        self.assertEqual(founder_park["method"], "html_diff")
        self.assertTrue(founder_park.get("enabled", True))

    def test_old_overseas_general_ai_sources_are_removed(self) -> None:
        serialized = json.dumps(self.sources, ensure_ascii=False).lower()
        old_general_sources = [
            "openai news",
            "google keyword",
            "latent.space ai 播客",
            "mit ai 新闻",
            "arxiv 人工智能论文",
        ]

        for source_name in old_general_sources:
            self.assertNotIn(source_name.lower(), serialized)


if __name__ == "__main__":
    unittest.main()
