from __future__ import annotations

import hashlib
import gzip
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
import zlib
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36 ai-news-telegram-radar/0.1"
SUPPORTED_FEED_METHODS = {"rss", "atom", "github_atom"}
SUPPORTED_STATEFUL_METHODS = {
    "html_diff",
    "html_list",
    "huggingface_api",
    "modelscope_html",
    "policy_keyword_html",
    "github_repos_api",
}
SUPPORTED_METHODS = SUPPORTED_FEED_METHODS | SUPPORTED_STATEFUL_METHODS

POLICY_LIST_KEYWORDS = [
    "人工智能",
    "生成式",
    "大模型",
    "模型",
    "算法",
    "智能",
    "算力",
    "数据集",
    "数据要素",
    "公共数据",
    "可信数据空间",
    "标准",
    "评估",
    "测评",
    "报告",
    "白皮书",
    "蓝皮书",
    "备案",
    "治理",
    "监管",
    "ai",
    "aigc",
    "llm",
]


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    method: str = "rss"
    entity: str = ""
    category: str = ""
    priority: str = ""
    push_rule: str = ""
    weight: float = 1.0
    max_items: int | None = None
    enabled: bool = True
    fallback_urls: tuple[str, ...] = ()


@dataclass
class NewsItem:
    title: str
    url: str
    summary: str
    published_at: str | None
    source_id: str
    source_name: str
    tags: list[str]
    score: float
    translated_title: str | None = None
    translated_summary: str | None = None


@dataclass
class SourceStatus:
    source_id: str
    source_name: str
    ok: bool
    item_count: int
    error: str | None = None


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self.parts.append(data)

    def get_text(self) -> str:
        return " ".join(" ".join(self.parts).split())


class _HTMLLinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.title_parts: list[str] = []
        self._href: str | None = None
        self._link_fallback_text = ""
        self._link_parts: list[str] = []
        self._in_title = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            attr_map = {key.lower(): value for key, value in attrs if value is not None}
            href = attr_map.get("href", "").strip()
            if href:
                self._href = href
                self._link_fallback_text = (
                    attr_map.get("aria-label", "").strip() or attr_map.get("title", "").strip()
                )
                self._link_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a" and self._href:
            text = " ".join(" ".join(self._link_parts).split())
            if not text:
                text = self._link_fallback_text
            self.links.append((self._href, unescape(text).strip()))
            self._href = None
            self._link_fallback_text = ""
            self._link_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._href:
            self._link_parts.append(data)

    def get_title(self) -> str:
        return " ".join(" ".join(self.title_parts).split()).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(value)
    text = parser.get_text()
    return unescape(text).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def direct_children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    wanted = name.lower()
    return [child for child in list(element) if local_name(child.tag) == wanted]


def first_child(element: ElementTree.Element, *names: str) -> ElementTree.Element | None:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if local_name(child.tag) in wanted:
            return child
    return None


def first_text(element: ElementTree.Element, *names: str) -> str:
    child = first_child(element, *names)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def atom_link(entry: ElementTree.Element) -> str:
    fallback = ""
    for child in direct_children(entry, "link"):
        href = child.attrib.get("href", "").strip()
        if not href:
            continue
        rel = child.attrib.get("rel", "alternate")
        if rel == "alternate":
            return href
        fallback = fallback or href
    return fallback


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None

    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if "sources" not in config or not isinstance(config["sources"], list):
        raise ValueError("config must contain a sources list")
    return config


def load_sources(config: dict[str, Any]) -> list[Source]:
    sources = []
    for raw in config.get("sources", []):
        if raw.get("enabled", True) is False:
            continue
        sources.append(
            Source(
                id=str(raw["id"]),
                name=str(raw.get("name") or raw["id"]),
                url=str(raw["url"]),
                method=str(raw.get("method", "rss")),
                entity=str(raw.get("entity", "")),
                category=str(raw.get("category", "")),
                priority=str(raw.get("priority", "")),
                push_rule=str(raw.get("push_rule", "")),
                weight=float(raw.get("weight", 1.0)),
                max_items=int(raw["max_items"]) if raw.get("max_items") is not None else None,
                enabled=True,
                fallback_urls=tuple(str(url) for url in raw.get("fallback_urls", []) if str(url).strip()),
            )
        )
    return sources


def fetch_text(url: str, timeout: int = 20, retries: int = 2) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(
            url,
            headers=request_headers(url),
        )
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                    content_encoding = response.headers.get("Content-Encoding", "").lower()
                    if "gzip" in content_encoding:
                        raw = gzip.decompress(raw)
                    elif "deflate" in content_encoding:
                        raw = zlib.decompress(raw)
                    charset = response.headers.get_content_charset() or "utf-8"
                    return raw.decode(charset, errors="replace")
            except Exception as exc:  # noqa: BLE001 - transient feed failures are common.
                last_error = exc
                if attempt < retries:
                    time.sleep(1 + attempt)
        curl_text = fetch_text_with_curl(url, timeout=timeout)
        if curl_text is not None:
            return curl_text
        raise last_error or RuntimeError(f"failed to fetch {url}")

    if parsed.scheme == "file":
        return Path(urllib.request.url2pathname(parsed.path)).read_text(encoding="utf-8")

    return Path(url).read_text(encoding="utf-8")


def fetch_text_with_curl(url: str, timeout: int = 20) -> str | None:
    args = [
        "curl",
        "-L",
        "--max-time",
        str(timeout),
        "-A",
        USER_AGENT,
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,application/atom+xml,text/xml,*/*;q=0.8",
        "-H",
        "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
    ]
    token = github_api_token(url)
    if token:
        args.extend(["-H", f"Authorization: Bearer {token}", "-H", "X-GitHub-Api-Version: 2022-11-28"])
    args.extend(["-sS", url])
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            timeout=timeout + 5,
        )
    except Exception:  # noqa: BLE001 - curl is only a best-effort fallback.
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def request_headers(url: str) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,application/atom+xml,text/xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }
    token = github_api_token(url)
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return headers


def github_api_token(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() != "api.github.com":
        return ""
    return os.environ.get("GITHUB_TOKEN", "").strip()


def source_fetch_candidates(source: Source) -> list[str]:
    return list(dict.fromkeys([source.url, *source.fallback_urls]))


def fetch_source_text(source: Source) -> tuple[str, Source]:
    errors: list[str] = []
    for url in source_fetch_candidates(source):
        try:
            return fetch_text(url), replace(source, url=url)
        except Exception as exc:  # noqa: BLE001 - try configured fallback URLs before failing the source.
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors))


def parse_feed(xml_text: str, source: Source) -> list[NewsItem]:
    root = ElementTree.fromstring(xml_text)
    root_name = local_name(root.tag)
    entries: list[ElementTree.Element]
    is_atom = False

    if root_name == "rss":
        channel = first_child(root, "channel")
        entries = direct_children(channel or root, "item")
    elif root_name == "feed":
        is_atom = True
        entries = direct_children(root, "entry")
    else:
        entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
        is_atom = any(local_name(node.tag) == "entry" for node in entries)

    items: list[NewsItem] = []
    for entry in entries:
        title = strip_html(first_text(entry, "title"))
        if not title:
            continue

        if is_atom or local_name(entry.tag) == "entry":
            url = atom_link(entry)
            summary = strip_html(first_text(entry, "summary", "content"))
            published = parse_datetime(first_text(entry, "published", "updated"))
            tags = [child.attrib.get("term", "").strip() for child in direct_children(entry, "category")]
        else:
            url = first_text(entry, "link", "guid")
            summary = strip_html(first_text(entry, "description", "content", "encoded"))
            published = parse_datetime(first_text(entry, "pubDate", "published", "updated", "date"))
            tags = [strip_html(child.text or "") for child in direct_children(entry, "category")]

        items.append(
            NewsItem(
                title=title,
                url=url.strip(),
                summary=summary,
                published_at=iso_or_none(published),
                source_id=source.id,
                source_name=source.name,
                tags=[tag for tag in tags if tag],
                score=0.0,
            )
        )
    return items


def load_runtime_state(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    state_path = Path(path)
    if not state_path.exists():
        return {"sources": {}}
    with state_path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if "sources" not in state or not isinstance(state["sources"], dict):
        state["sources"] = {}
    return state


def write_runtime_state(path: str | Path | None, state: dict[str, Any]) -> None:
    if path is None:
        return
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_source_state(state: dict[str, Any] | None, source: Source) -> dict[str, Any] | None:
    if state is None:
        return None
    sources = state.setdefault("sources", {})
    return sources.setdefault(source.id, {})


def state_key_for_item(item: NewsItem) -> str:
    return canonical_url(item.url) if item.url else normalize_title(item.title)


def filter_new_items_by_state(
    source: Source,
    items: list[NewsItem],
    state: dict[str, Any] | None,
    *,
    key_name: str = "seen_keys",
) -> list[NewsItem]:
    source_state = get_source_state(state, source)
    if source_state is None:
        return items

    current_keys = [state_key_for_item(item) for item in items if state_key_for_item(item)]
    previous_keys = list(source_state.get(key_name, []))
    seen = set(previous_keys)
    merged_keys = list(dict.fromkeys(current_keys + previous_keys))[:300]
    if merged_keys != previous_keys:
        source_state[key_name] = merged_keys
        source_state["last_changed_at"] = iso_or_none(datetime.now(timezone.utc))

    if not seen:
        source_state["baseline_initialized"] = True
        return []
    return [item for item in items if state_key_for_item(item) not in seen]


def parse_date_from_text(value: str) -> datetime | None:
    text = value.strip()
    patterns = [
        r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})",
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        year, month, day = [int(part) for part in match.groups()]
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def parse_html_links(html_text: str, source: Source) -> tuple[list[tuple[str, str]], str]:
    parser = _HTMLLinkExtractor()
    parser.feed(html_text)
    links: list[tuple[str, str]] = []
    source_host = urllib.parse.urlparse(source.url).netloc
    ignored_text = {
        "首页",
        "登录",
        "注册",
        "更多",
        "文档",
        "产品",
        "价格",
        "about",
        "login",
        "sign in",
        "sign up",
        "docs",
        "pricing",
        "blog",
        "publication",
        "about",
        "next",
        "next »",
        "previous",
        "qwen",
        "try qwen chat",
    }
    for href, text in parser.links:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute_url = urllib.parse.urljoin(source.url, href)
        parsed = urllib.parse.urlparse(absolute_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if source_host and parsed.netloc and parsed.netloc != source_host:
            continue
        clean_text = clean_link_title(text, absolute_url)
        if len(clean_text) < 4 or len(clean_text) > 180:
            continue
        if clean_text.lower() in ignored_text:
            continue
        if not should_keep_html_link(source, absolute_url, clean_text):
            continue
        links.append((absolute_url, clean_text))
    return links, parser.get_title()


def should_keep_html_link(source: Source, url: str, title: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    normalized_title = title.strip().lower()

    if source.id == "kimi-forum":
        if normalized_title == "announcement":
            return False
        return len(path_parts) >= 2 and path_parts[0] == "t" and path_parts[-1].isdigit()

    if source.id == "qwen-blog":
        return len(path_parts) == 2 and path_parts[0] == "blog"

    return True


def clean_link_title(text: str, url: str) -> str:
    clean_text = " ".join(unescape(text).split())
    lower = clean_text.lower()
    for prefix in ("post link to ", "topic link to "):
        if lower.startswith(prefix):
            clean_text = clean_text[len(prefix) :].strip()
            break
    if clean_text:
        return clean_text
    return title_from_url(url)


def title_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts:
        return ""
    slug = urllib.parse.unquote(parts[-1])
    if slug.isdigit() and len(parts) >= 2:
        slug = urllib.parse.unquote(parts[-2])
    slug = re.sub(r"[-_]+", " ", slug)
    return " ".join(word.capitalize() for word in slug.split()).strip()


def parse_embedded_html_items(html_text: str, source: Source) -> list[NewsItem]:
    items: list[NewsItem] = []
    seen: set[str] = set()

    embedded_patterns = [
        r'title:"(?P<title>(?:\\.|[^"\\])+)".{0,500}?create_time:"(?P<date>(?:\\.|[^"\\])+)".{0,500}?uuid:"(?P<id>(?:\\.|[^"\\])+)".{0,500}?content:"(?P<summary>(?:\\.|[^"\\])*)"',
        r'content:"(?P<summary>(?:\\.|[^"\\])*)".{0,500}?title:"(?P<title>(?:\\.|[^"\\])+)".{0,500}?create_time:"(?P<date>(?:\\.|[^"\\])+)".{0,500}?uuid:"(?P<id>(?:\\.|[^"\\])*)"',
    ]
    for pattern in embedded_patterns:
        for match in re.finditer(pattern, html_text, flags=re.DOTALL):
            raw_title = decode_js_string(match.group("title"))
            key = raw_title
            if key in seen:
                continue
            seen.add(key)
            item_id = decode_js_string(match.group("id"))
            date_text = decode_js_string(match.group("date"))
            summary = strip_html(decode_js_string(match.group("summary")))
            items.append(
                NewsItem(
                    title=raw_title,
                    url=f"{source.url.rstrip('/')}/?item={urllib.parse.quote(item_id or raw_title)}",
                    summary=summary or f"{source.name} 页面内嵌数据发现：{raw_title}",
                    published_at=iso_or_none(parse_datetime(date_text) or parse_date_from_text(date_text)),
                    source_id=source.id,
                    source_name=source.name,
                    tags=[tag for tag in [source.entity, source.category, source.method, source.priority] if tag],
                    score=0.0,
                )
            )

    for match in re.finditer(
        r'<article\b(?P<body>.*?)</article>',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        body = match.group("body")
        title_match = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", body, flags=re.DOTALL | re.IGNORECASE)
        href_match = re.search(r'href=(["\']?)(?P<href>https?://[^"\'\s>]+|/[^"\'\s>]+)', body, flags=re.IGNORECASE)
        if not title_match or not href_match:
            continue
        title = strip_html(title_match.group(1))
        if not title or title in seen:
            continue
        seen.add(title)
        href = href_match.group("href")
        url = urllib.parse.urljoin(source.url, href)
        summary_match = re.search(r'<div[^>]+class=["\'][^"\']*entry-content[^"\']*["\'][^>]*>(.*?)</div>', body, flags=re.DOTALL | re.IGNORECASE)
        date_match = re.search(r"<span[^>]+title=['\"]([^'\"]+)['\"]", body, flags=re.IGNORECASE)
        items.append(
            NewsItem(
                title=title,
                url=url,
                summary=strip_html(summary_match.group(1)) if summary_match else f"{source.name} 文章：{title}",
                published_at=iso_or_none(parse_datetime(date_match.group(1)) if date_match else None),
                source_id=source.id,
                source_name=source.name,
                tags=[tag for tag in [source.entity, source.category, source.method, source.priority] if tag],
                score=0.0,
            )
        )

    return items


def decode_js_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace(r"\/", "/").replace(r"\u002F", "/").replace(r"\"", '"')


def parse_html_list(html_text: str, source: Source) -> list[NewsItem]:
    links, page_title = parse_html_links(html_text, source)
    items: list[NewsItem] = []
    seen: set[str] = set()
    link_items = [
        NewsItem(
            title=title,
            url=url,
            summary=f"{source.name} 列表页发现：{title}",
            published_at=iso_or_none(parse_date_from_text(f"{title} {url}")),
            source_id=source.id,
            source_name=source.name,
            tags=[tag for tag in [source.entity, source.category, source.method, source.priority, page_title] if tag],
            score=0.0,
        )
        for url, title in links
    ]
    for item in [*link_items, *parse_embedded_html_items(html_text, source)]:
        url = item.url
        title = item.title
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= 30:
            break
    return items


def policy_keyword_match(item: NewsItem) -> bool:
    haystack = f"{item.title}\n{item.url}".lower()
    return any(keyword_present(haystack, keyword) for keyword in POLICY_LIST_KEYWORDS)


def parse_policy_keyword_html(html_text: str, source: Source) -> list[NewsItem]:
    return [item for item in parse_html_list(html_text, source) if policy_keyword_match(item)]


def html_fingerprint(html_text: str) -> tuple[str, str]:
    text = strip_html(html_text)
    normalized = re.sub(r"\s+", " ", text).strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    excerpt = normalized[:240]
    return digest, excerpt


def collect_html_diff_items(
    html_text: str,
    source: Source,
    state: dict[str, Any] | None,
    now: datetime,
) -> list[NewsItem]:
    source_state = get_source_state(state, source)
    if source_state is None:
        return []

    fingerprint, excerpt = html_fingerprint(html_text)
    previous = source_state.get("fingerprint")
    previous_excerpt = str(source_state.get("last_excerpt") or "")
    invalid_current = len(excerpt) < 40 or "�" in excerpt or excerpt.strip().lower() in {"please wait...", "please wait"}
    invalid_previous = "�" in previous_excerpt or previous_excerpt.strip().lower() in {"please wait...", "please wait"}
    if previous == fingerprint and previous_excerpt == excerpt:
        return []
    source_state["fingerprint"] = fingerprint
    source_state["last_changed_at"] = iso_or_none(now)
    source_state["last_excerpt"] = excerpt
    if invalid_current:
        source_state["baseline_invalid"] = True
        return []
    if not previous or invalid_previous:
        source_state["baseline_initialized"] = True
        return []
    if previous == fingerprint:
        return []

    return [
        NewsItem(
            title=f"{source.name} 页面发生更新",
            url=source.url,
            summary=f"检测到 {source.entity or source.name} 的页面内容变化。推送规则：{source.push_rule or source.method}。",
            published_at=iso_or_none(now),
            source_id=source.id,
            source_name=source.name,
            tags=[tag for tag in [source.entity, source.category, source.method, source.priority, "人工智能", "大模型"] if tag],
            score=0.0,
        )
    ]


def huggingface_namespace(source: Source) -> str:
    parsed = urllib.parse.urlparse(source.url)
    return parsed.path.strip("/").split("/", 1)[0]


def fetch_huggingface_models(source: Source) -> list[dict[str, Any]]:
    namespace = huggingface_namespace(source)
    query = urllib.parse.urlencode(
        {
            "author": namespace,
            "sort": "lastModified",
            "direction": "-1",
            "limit": "20",
        }
    )
    raw = fetch_text(f"https://huggingface.co/api/models?{query}", timeout=30)
    payload = json.loads(raw)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def collect_huggingface_items(
    source: Source,
    state: dict[str, Any] | None,
    now: datetime,
) -> list[NewsItem]:
    models = fetch_huggingface_models(source)
    source_state = get_source_state(state, source)
    previous: dict[str, str] = {}
    if source_state is not None:
        previous = dict(source_state.get("models", {}))

    current: dict[str, str] = {}
    items: list[NewsItem] = []
    for model in models:
        model_id = str(model.get("modelId") or model.get("id") or "").strip()
        if not model_id:
            continue
        last_modified = str(model.get("lastModified") or "").strip()
        current[model_id] = last_modified
        if source_state is not None and not previous:
            continue
        if source_state is not None and previous.get(model_id) == last_modified:
            continue
        published = parse_datetime(last_modified)
        tags = [str(tag) for tag in model.get("tags", [])[:8] if tag]
        pipeline_tag = str(model.get("pipeline_tag") or "").strip()
        if pipeline_tag:
            tags.append(pipeline_tag)
        downloads = model.get("downloads")
        likes = model.get("likes")
        metrics = []
        if downloads is not None:
            metrics.append(f"downloads={downloads}")
        if likes is not None:
            metrics.append(f"likes={likes}")
        summary = f"{source.name} 检测到模型更新：{model_id}"
        if metrics:
            summary += f"（{', '.join(metrics)}）"
        items.append(
            NewsItem(
                title=f"{source.name} 模型更新：{model_id}",
                url=f"https://huggingface.co/{model_id}",
                summary=summary,
                published_at=iso_or_none(published or now),
                source_id=source.id,
                source_name=source.name,
                tags=[tag for tag in [source.entity, source.category, source.method, "Hugging Face", *tags] if tag],
                score=0.0,
            )
        )

    if source_state is not None:
        if current != previous:
            source_state["models"] = current
            source_state["last_changed_at"] = iso_or_none(now)
        if not previous:
            source_state["baseline_initialized"] = True
            return []
    return items


def fetch_github_repos(source: Source) -> list[dict[str, Any]]:
    raw = fetch_text(source.url, timeout=30)
    payload = json.loads(raw)
    if isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            payload = payload["items"]
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def github_repo_sort_time(repo: dict[str, Any]) -> str:
    return str(repo.get("pushed_at") or repo.get("updated_at") or repo.get("created_at") or "").strip()


def collect_github_repo_items(
    source: Source,
    state: dict[str, Any] | None,
    now: datetime,
) -> list[NewsItem]:
    repos = fetch_github_repos(source)
    source_state = get_source_state(state, source)
    previous: dict[str, str] = {}
    if source_state is not None:
        previous = dict(source_state.get("repos", {}))

    current: dict[str, str] = {}
    items: list[NewsItem] = []
    for repo in repos[:30]:
        full_name = str(repo.get("full_name") or repo.get("name") or "").strip()
        if not full_name:
            continue
        updated_at = github_repo_sort_time(repo)
        current[full_name] = updated_at
        if source_state is not None and not previous:
            continue
        if source_state is not None and previous.get(full_name) == updated_at:
            continue

        url = str(repo.get("html_url") or "").strip() or f"https://github.com/{full_name}"
        description = str(repo.get("description") or "").strip()
        language = str(repo.get("language") or "").strip()
        topics = [str(topic) for topic in repo.get("topics", [])[:8] if topic]
        tags = [tag for tag in [source.entity, source.category, source.method, "GitHub", language, *topics] if tag]
        summary = f"{source.name} 检测到 GitHub 项目更新：{full_name}"
        if description:
            summary += f"。{description}"
        items.append(
            NewsItem(
                title=f"{source.name} 项目更新：{full_name}",
                url=url,
                summary=summary,
                published_at=iso_or_none(parse_datetime(updated_at) or now),
                source_id=source.id,
                source_name=source.name,
                tags=tags,
                score=0.0,
            )
        )

    if source_state is not None:
        if current != previous:
            source_state["repos"] = current
            source_state["last_changed_at"] = iso_or_none(now)
        if not previous:
            source_state["baseline_initialized"] = True
            return []
    return items


def collect_source_items(
    source: Source,
    state: dict[str, Any] | None,
    now: datetime,
) -> tuple[list[NewsItem], int]:
    if source.method in SUPPORTED_FEED_METHODS:
        text, active_source = fetch_source_text(source)
        items = parse_feed(text, active_source)
        raw_count = len(items)
        return filter_new_items_by_state(source, items, state), raw_count

    if source.method in {"html_list", "modelscope_html"}:
        text, active_source = fetch_source_text(source)
        items = parse_html_list(text, active_source)
        return filter_new_items_by_state(source, items, state), len(items)

    if source.method == "policy_keyword_html":
        text, active_source = fetch_source_text(source)
        items = parse_policy_keyword_html(text, active_source)
        return filter_new_items_by_state(source, items, state), len(items)

    if source.method == "html_diff":
        text, active_source = fetch_source_text(source)
        items = collect_html_diff_items(text, active_source, state, now)
        return items, 1

    if source.method == "huggingface_api":
        items = collect_huggingface_items(source, state, now)
        source_state = get_source_state(state, source)
        raw_count = len(source_state.get("models", {})) if source_state is not None else len(items)
        return items, raw_count

    if source.method == "github_repos_api":
        items = collect_github_repo_items(source, state, now)
        source_state = get_source_state(state, source)
        raw_count = len(source_state.get("repos", {})) if source_state is not None else len(items)
        return items, raw_count

    raise ValueError(f"unsupported method: {source.method}")


def canonical_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [
        (key, value)
        for key, value in query
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source"}
    ]
    normalized = parsed._replace(query=urllib.parse.urlencode(filtered), fragment="")
    return urllib.parse.urlunparse(normalized).rstrip("/")


def normalize_title(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()


def keyword_present(text: str, keyword: str) -> bool:
    key = keyword.lower().strip()
    if not key:
        return False
    if re.fullmatch(r"[a-z0-9.+#-]{1,4}", key):
        return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text) is not None
    return key in text


def item_matches_keywords(item: NewsItem, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{item.title}\n{item.summary}\n{' '.join(item.tags)}".lower()
    return any(keyword_present(haystack, keyword) for keyword in keywords)


def is_noise_item(item: NewsItem) -> bool:
    title = item.title.lower().strip()
    summary = item.summary.lower().strip()
    text = f"{title}\n{summary}"
    if "github" in item.source_id.lower() or "github" in item.source_name.lower():
        github_noise_patterns = [
            r"\badded .+ to .+/.+",
            r"\bremoved .+ from .+/.+",
            r"\bstarted following\b",
            r"\bstarred .+/.+",
            r"\bforked .+/.+",
            r"\bpushed to .+ in .+/.+",
            r"\bcreated branch\b",
            r"\bdeleted branch\b",
            r"\bopened issue\b",
            r"\bclosed issue\b",
        ]
        if any(re.search(pattern, text) for pattern in github_noise_patterns):
            return True
    return False


def score_item(item: NewsItem, keywords: list[str], source_weight: float, now: datetime) -> float:
    haystack = f"{item.title}\n{item.summary}\n{' '.join(item.tags)}".lower()
    title = item.title.lower()
    hits = 0
    title_hits = 0
    for keyword in keywords:
        if keyword_present(haystack, keyword):
            hits += 1
        if keyword_present(title, keyword):
            title_hits += 1

    published = parse_datetime(item.published_at)
    recency_bonus = 0.0
    if published:
        age_hours = max((now - published).total_seconds() / 3600, 0)
        recency_bonus = max(0.0, 1.0 - min(age_hours, 48) / 48)

    return round(source_weight + hits * 1.3 + title_hits * 1.7 + recency_bonus, 2)


def in_window(item: NewsItem, now: datetime, hours: int) -> bool:
    published = parse_datetime(item.published_at)
    if published is None:
        return True
    return published >= now - timedelta(hours=hours)


def collect_news(
    config: dict[str, Any],
    *,
    now: datetime | None = None,
    window_hours: int | None = None,
    limit: int | None = None,
    state_path: str | Path | None = None,
) -> tuple[list[NewsItem], list[SourceStatus]]:
    settings = config.get("settings", {})
    now = now or datetime.now(timezone.utc)
    hours = int(window_hours or settings.get("window_hours", 24))
    max_items = int(limit or settings.get("limit", 10))
    min_score = float(settings.get("min_score", 2.0))
    per_source_limit = int(settings.get("per_source_limit", max_items))
    require_keyword_match = bool(settings.get("require_keyword_match", True))
    keywords = [str(keyword) for keyword in config.get("keywords", [])]

    source_by_id = {source.id: source for source in load_sources(config)}
    runtime_state = load_runtime_state(state_path) if state_path is not None else None
    collected: list[NewsItem] = []
    statuses: list[SourceStatus] = []

    for source in source_by_id.values():
        try:
            if source.method not in SUPPORTED_METHODS:
                statuses.append(
                    SourceStatus(
                        source.id,
                        source.name,
                        False,
                        0,
                        f"unsupported method: {source.method}",
                    )
                )
                continue
            items, raw_count = collect_source_items(source, runtime_state, now)
            for item in items:
                if is_noise_item(item):
                    continue
                item.score = score_item(item, keywords, source.weight, now)
                keyword_ok = not require_keyword_match or item_matches_keywords(item, keywords)
                if keyword_ok and in_window(item, now, hours) and item.score >= min_score:
                    collected.append(item)
            statuses.append(SourceStatus(source.id, source.name, True, raw_count))
        except Exception as exc:  # noqa: BLE001 - keep one bad feed from breaking the digest.
            statuses.append(SourceStatus(source.id, source.name, False, 0, str(exc)))

    write_runtime_state(state_path, runtime_state or {})

    deduped: dict[str, NewsItem] = {}
    for item in collected:
        key = canonical_url(item.url) if item.url else normalize_title(item.title)
        existing = deduped.get(key)
        if existing is None or item.score > existing.score:
            deduped[key] = item

    sorted_items = sorted(
        deduped.values(),
        key=lambda item: (item.score, parse_datetime(item.published_at) or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )

    selected: list[NewsItem] = []
    source_counts: dict[str, int] = {}
    for item in sorted_items:
        source = source_by_id.get(item.source_id)
        source_limit = source.max_items if source and source.max_items is not None else per_source_limit
        if source_counts.get(item.source_id, 0) >= source_limit:
            continue
        selected.append(item)
        source_counts[item.source_id] = source_counts.get(item.source_id, 0) + 1
        if len(selected) >= max_items:
            break

    return selected, statuses


def item_to_dict(item: NewsItem) -> dict[str, Any]:
    return asdict(item)


def write_outputs(
    output_dir: str | Path,
    *,
    items: list[NewsItem],
    statuses: list[SourceStatus],
    generated_at: datetime | None = None,
) -> None:
    generated_at = generated_at or datetime.now(timezone.utc)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": iso_or_none(generated_at),
        "count": len(items),
        "items": [item_to_dict(item) for item in items],
        "sources": [asdict(status) for status in statuses],
    }
    (out / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
