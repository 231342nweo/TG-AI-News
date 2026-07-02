from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config/sources.json"
IMPLEMENTED_METHODS = {
    "rss",
    "atom",
    "github_atom",
    "github_repos_api",
    "html_diff",
    "html_list",
    "huggingface_api",
    "modelscope_html",
    "policy_keyword_html",
}


def main() -> int:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    sources = data["sources"]
    enabled = [source for source in sources if source.get("enabled", True)]
    disabled = [source for source in sources if not source.get("enabled", True)]
    pending = [source for source in disabled]

    print("中国 AI 信息源目录审计")
    print(f"目录版本：{data.get('catalog_version')}")
    print(f"总信源：{len(sources)}")
    print(f"当前启用：{len(enabled)}")
    print(f"待启用/待验收：{len(pending)}")
    print()

    print("当前启用方法：")
    for method, count in Counter(source["method"] for source in enabled).most_common():
        print(f"- {method}: {count}")
    print()

    print("待启用方法：")
    if pending:
        for method, count in Counter(source["method"] for source in pending).most_common():
            print(f"- {method}: {count}")
    else:
        print("- 无")
    print()

    if pending:
        print("暂缓源：")
        for source in pending:
            reason = source.get("disabled_reason", "未填写原因")
            print(f"- {source['priority']} / {source['method']} / {source['name']}: {reason}")
        print()

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for source in pending:
        grouped[(source["priority"], source["method"])].append(source)

    priority_order = {"S": 0, "A": 1, "B": 2}
    print("建议验收/启用顺序：")
    if grouped:
        for (priority, method), items in sorted(grouped.items(), key=lambda item: (priority_order.get(item[0][0], 9), item[0][1])):
            names = "、".join(source["name"] for source in items[:8])
            suffix = "" if len(items) <= 8 else f" 等 {len(items)} 个"
            print(f"- P{priority} / {method}: {names}{suffix}")
    else:
        print("- 暂无")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
