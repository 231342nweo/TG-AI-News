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
AIHOT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
SUPPORTED_FEED_METHODS = {"rss", "atom", "github_atom"}
SUPPORTED_STATEFUL_METHODS = {
    "aihot_api",
    "html_diff",
    "html_list",
    "huggingface_api",
    "modelscope_html",
    "policy_keyword_html",
    "github_repos_api",
    "tikhub_wechat_account_articles",
    "tikhub_wechat_search",
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

AIHOT_CATEGORY_LABELS = {
    "ai-models": "模型发布/更新",
    "ai-products": "产品发布/更新",
    "industry": "行业动态",
    "paper": "论文研究",
    "tip": "技巧与观点",
}


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


def tikhub_api_key() -> str:
    return os.environ.get("TIKHUB_API_KEY", "").strip()


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


def source_state_signature(source: Source) -> str:
    payload = {
        "url": source.url,
        "method": source.method,
        "fallback_urls": list(source.fallback_urls),
        "push_rule": source.push_rule,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sync_source_signature(source: Source, source_state: dict[str, Any], now: datetime) -> bool:
    signature = source_state_signature(source)
    previous = str(source_state.get("source_signature") or "")
    if previous == signature:
        return False
    source_state["source_signature"] = signature
    source_state["baseline_initialized"] = True
    source_state["baseline_reinitialized_at"] = iso_or_none(now)
    return True


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

    now = datetime.now(timezone.utc)
    signature_changed = sync_source_signature(source, source_state, now)
    current_keys = []
    for item in items:
        key = state_key_for_item(item)
        if key:
            current_keys.append(key)
    previous_keys = list(source_state.get(key_name, []))
    seen = set(previous_keys)
    merged_keys = list(dict.fromkeys(current_keys + previous_keys))[:300]
    if signature_changed:
        source_state[key_name] = list(dict.fromkeys(current_keys))[:300]
        source_state["last_changed_at"] = iso_or_none(now)
        return []
    if merged_keys != previous_keys:
        source_state[key_name] = merged_keys
        source_state["last_changed_at"] = iso_or_none(now)

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


TIKHUB_INTERNAL_QUERY_KEYS = {"_poll_minutes", "_interval_minutes", "_limit", "_source_title"}
TIKHUB_TITLE_KEYS = {"title", "displaytitle", "articletitle", "msgtitle"}
TIKHUB_URL_KEYS = {"url", "link", "docurl", "articleurl", "contenturl", "jumpurl", "sourceurl"}
TIKHUB_SUMMARY_KEYS = {"digest", "summary", "description", "desc", "abstract", "snippet", "content"}
TIKHUB_TIME_KEYS = {
    "createtime",
    "createat",
    "createdat",
    "publishtime",
    "publishedat",
    "pubtime",
    "datetime",
    "timestamp",
    "time",
    "updatetime",
}
TIKHUB_ID_KEYS = {"id", "docid", "doc", "articleid", "mid", "sn"}


def normalize_json_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def json_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def parse_tikhub_value(value: str) -> Any:
    raw = value.strip()
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return raw
    if re.fullmatch(r"-?\d+\.\d+", raw):
        try:
            return float(raw)
        except ValueError:
            return raw
    if raw.startswith(("{", "[")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def tikhub_request_url_and_payload(source: Source) -> tuple[str, dict[str, Any], dict[str, str]]:
    parsed = urllib.parse.urlparse(source.url)
    payload: dict[str, Any] = {}
    internal: dict[str, str] = {}
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key in TIKHUB_INTERNAL_QUERY_KEYS:
            internal[key] = value
            continue
        payload[key] = parse_tikhub_value(value)
    api_url = urllib.parse.urlunparse(parsed._replace(query=""))
    return api_url, payload, internal


def tikhub_source_poll_minutes(source: Source) -> int:
    _, _, internal = tikhub_request_url_and_payload(source)
    raw = internal.get("_poll_minutes") or internal.get("_interval_minutes") or os.environ.get("TIKHUB_POLL_MINUTES", "60")
    try:
        return max(int(raw), 5)
    except ValueError:
        return 60


def tikhub_source_limit(source: Source) -> int:
    _, _, internal = tikhub_request_url_and_payload(source)
    raw = internal.get("_limit")
    if raw:
        try:
            return max(int(raw), 1)
        except ValueError:
            pass
    return source.max_items or 10


def tikhub_source_title_filter(source: Source) -> str:
    _, _, internal = tikhub_request_url_and_payload(source)
    return strip_html(internal.get("_source_title", "")).strip()


def tikhub_poll_due(source: Source, source_state: dict[str, Any] | None, now: datetime) -> bool:
    if source_state is None:
        return True
    last_polled = parse_datetime(str(source_state.get("last_polled_at") or ""))
    if last_polled is None:
        return True
    return now.astimezone(timezone.utc) - last_polled >= timedelta(minutes=tikhub_source_poll_minutes(source))


def fetch_tikhub_payload(source: Source) -> Any:
    api_url, payload, _ = tikhub_request_url_and_payload(source)
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme in {"http", "https"}:
        key = tikhub_api_key()
        if not key:
            return None
        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    else:
        path = urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else api_url
        raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def find_json_value(value: Any, key_names: set[str]) -> Any:
    if isinstance(value, dict):
        for key, candidate in value.items():
            if normalize_json_key(str(key)) in key_names and json_value_present(candidate):
                return candidate
        for candidate in value.values():
            found = find_json_value(candidate, key_names)
            if json_value_present(found):
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = find_json_value(candidate, key_names)
            if json_value_present(found):
                return found
    return None


def find_direct_json_value(value: dict[str, Any], key_names: set[str]) -> Any:
    for key, candidate in value.items():
        if normalize_json_key(str(key)) in key_names and json_value_present(candidate):
            return candidate
    return None


def iter_json_dicts(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        records.append(value)
        for candidate in value.values():
            records.extend(iter_json_dicts(candidate))
    elif isinstance(value, list):
        for candidate in value:
            records.extend(iter_json_dicts(candidate))
    return records


def iter_tikhub_wechat_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in iter_json_dicts(value):
        title = find_direct_json_value(record, TIKHUB_TITLE_KEYS)
        url = find_direct_json_value(record, TIKHUB_URL_KEYS)
        doc_type = str(record.get("docType") or record.get("doctype") or "")
        if json_value_present(title) and json_value_present(url) and doc_type in {"", "0"}:
            records.append(record)
    return records


def tikhub_record_account_name(record: dict[str, Any]) -> str:
    source_info = record.get("source")
    if isinstance(source_info, dict):
        for key in ("title", "nickName", "nickname", "name"):
            value = source_info.get(key)
            if json_value_present(value):
                return strip_html(str(value)).strip()
    for key in ("source_title", "sourceTitle", "account_name", "accountName", "nick_name", "nickname"):
        value = record.get(key)
        if json_value_present(value):
            return strip_html(str(value)).strip()
    return ""


def tikhub_source_title_matches(actual: str, expected: str) -> bool:
    if not expected:
        return True
    return re.sub(r"\s+", "", actual).lower() == re.sub(r"\s+", "", expected).lower()


def parse_tikhub_datetime(value: Any) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10,13}", value.strip())):
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value)
    return parse_datetime(text) or parse_date_from_text(text)


def collect_tikhub_wechat_items(
    source: Source,
    state: dict[str, Any] | None,
    now: datetime,
) -> list[NewsItem]:
    source_state = get_source_state(state, source)
    if not tikhub_poll_due(source, source_state, now):
        return []

    payload = fetch_tikhub_payload(source)
    if payload is None:
        if source_state is not None:
            source_state["last_skipped_reason"] = "missing TIKHUB_API_KEY"
        return []

    if source_state is not None:
        source_state["last_polled_at"] = iso_or_none(now)
        source_state.pop("last_skipped_reason", None)

    items: list[NewsItem] = []
    seen: set[str] = set()
    source_title_filter = tikhub_source_title_filter(source)
    for record in iter_tikhub_wechat_records(payload):
        account = tikhub_record_account_name(record)
        if not tikhub_source_title_matches(account, source_title_filter):
            continue
        title = strip_html(str(find_direct_json_value(record, TIKHUB_TITLE_KEYS) or "")).strip()
        if not title or len(title) < 4:
            continue
        url = str(find_direct_json_value(record, TIKHUB_URL_KEYS) or "").strip()
        if not url:
            record_id = str(find_direct_json_value(record, TIKHUB_ID_KEYS) or "").strip()
            url = f"{source.url}#{urllib.parse.quote(record_id or title)}"
        summary_text = strip_html(str(find_direct_json_value(record, TIKHUB_SUMMARY_KEYS) or "")).strip()
        published = parse_tikhub_datetime(find_direct_json_value(record, TIKHUB_TIME_KEYS)) or now
        key = canonical_url(url) if url else normalize_title(title)
        if key in seen:
            continue
        seen.add(key)
        summary = f"{source.name} 发现微信文章：{title}"
        if account:
            summary += f"（{account}）"
        if summary_text and summary_text != title:
            summary += f"。{summary_text[:240]}"
        items.append(
            NewsItem(
                title=title,
                url=url,
                summary=summary,
                published_at=iso_or_none(published),
                source_id=source.id,
                source_name=source.name,
                tags=[tag for tag in [source.entity, source.category, source.method, source.priority, "TikHub", "WeChat"] if tag],
                score=0.0,
            )
        )
        if len(items) >= tikhub_source_limit(source):
            break

    if source_state is not None:
        source_state["last_raw_count"] = len(items)
        placeholder_key = canonical_url(source.url)
        seen_keys = set(source_state.get("seen_keys", []))
        if placeholder_key in seen_keys:
            source_state["seen_keys"] = [state_key_for_item(item) for item in items if state_key_for_item(item)][:300]
            source_state["last_changed_at"] = iso_or_none(now)
            source_state["tikhub_url_migration_at"] = iso_or_none(now)
            return []
    return filter_new_items_by_state(source, items, state)


def fetch_aihot_payload(source: Source) -> Any:
    parsed = urllib.parse.urlparse(source.url)
    if parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(
            source.url,
            headers={
                "User-Agent": AIHOT_USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    elif parsed.scheme == "file":
        raw = Path(urllib.request.url2pathname(parsed.path)).read_text(encoding="utf-8")
    else:
        raw = Path(parsed.path or source.url.split("?", 1)[0]).read_text(encoding="utf-8")
    return json.loads(raw)


def iter_aihot_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        records = payload.get("items")
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
        return [payload]
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    return []


def aihot_field(record: dict[str, Any], *names: str) -> Any:
    return find_direct_json_value(record, {normalize_json_key(name) for name in names})


def aihot_generated_at(payload: Any) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    value = aihot_field(payload, "generatedAt", "generated_at")
    return parse_datetime(str(value or ""))


def aihot_record_tags(record: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    raw_tags = record.get("tags")
    if isinstance(raw_tags, list):
        tags.extend(str(tag).strip() for tag in raw_tags if str(tag).strip())
    elif isinstance(raw_tags, str) and raw_tags.strip():
        tags.extend(part.strip() for part in re.split(r"[,，/ ]+", raw_tags) if part.strip())

    category = str(aihot_field(record, "category") or "").strip()
    if category:
        tags.append(AIHOT_CATEGORY_LABELS.get(category, category))
    severity = str(aihot_field(record, "severity") or "").strip()
    if severity:
        tags.append(severity)
    return tags


def collect_aihot_items(
    source: Source,
    state: dict[str, Any] | None,
    now: datetime,
) -> list[NewsItem]:
    payload = fetch_aihot_payload(source)
    records = iter_aihot_records(payload)
    generated_at = aihot_generated_at(payload)
    items: list[NewsItem] = []
    seen: set[str] = set()

    for record in records:
        title = strip_html(str(aihot_field(record, "title", "title_cn", "title_zh") or "")).strip()
        if not title:
            continue

        url = str(aihot_field(record, "url", "link", "sourceUrl", "source_url") or "").strip()
        record_id = str(aihot_field(record, "id") or "").strip()
        if not url:
            url = f"{source.url}#{urllib.parse.quote(record_id or title)}"
        key = canonical_url(url) if url else normalize_title(title)
        if key in seen:
            continue
        seen.add(key)

        summary_text = strip_html(str(aihot_field(record, "summary", "description", "desc") or "")).strip()
        original_source = strip_html(str(aihot_field(record, "source", "sourceName", "source_name") or "")).strip()
        category = str(aihot_field(record, "category") or "").strip()
        category_label = AIHOT_CATEGORY_LABELS.get(category, category)
        importance = aihot_field(record, "importance_score", "importanceScore")
        published = (
            parse_datetime(str(aihot_field(record, "publishedAt", "published_at", "createdAt", "created_at") or ""))
            or generated_at
            or now
        )

        details = []
        if original_source:
            details.append(f"原始来源：{original_source}")
        if category_label:
            details.append(f"分类：{category_label}")
        if importance is not None and str(importance).strip():
            details.append(f"重要性：{importance}")
        if summary_text and summary_text != title:
            details.append(summary_text[:260])

        summary = f"{source.name} 精选热点：{title}"
        if details:
            summary += "。" + "；".join(details)

        items.append(
            NewsItem(
                title=title,
                url=url,
                summary=summary,
                published_at=iso_or_none(published),
                source_id=source.id,
                source_name=source.name,
                tags=[
                    tag
                    for tag in [
                        source.entity,
                        source.category,
                        source.method,
                        source.priority,
                        "AIHot",
                        "人工智能",
                        *aihot_record_tags(record),
                    ]
                    if tag
                ],
                score=0.0,
            )
        )

    source_state = get_source_state(state, source)
    if source_state is not None:
        source_state["last_raw_count"] = len(items)
        source_state["last_polled_at"] = iso_or_none(now)
    return filter_new_items_by_state(source, items, state)


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


HTML_DIFF_EXCERPT_LIMIT = 4000
HTML_DIFF_INVALID_MARKERS = (
    "_wafchallengeid",
    "wafchallenge",
    "cf-browser-verification",
    "window._cf_chl_opt",
    "please enable javascript",
    "enable javascript",
    "please wait",
    "just a moment",
    "access denied",
    "安全验证",
    "访问验证",
    "验证码",
    "人机验证",
)


def html_fingerprint(html_text: str) -> tuple[str, str]:
    text = strip_html(html_text)
    normalized = re.sub(r"\s+", " ", text).strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    excerpt = normalized[:HTML_DIFF_EXCERPT_LIMIT]
    return digest, excerpt


def html_diff_snapshot_invalid(html_text: str, excerpt: str) -> bool:
    compact_excerpt = excerpt.strip()
    if len(compact_excerpt) < 30:
        return True
    if "�" in compact_excerpt:
        return True
    lower_html = html_text[:12000].lower()
    lower_excerpt = compact_excerpt.lower()
    return any(marker in lower_html or marker in lower_excerpt for marker in HTML_DIFF_INVALID_MARKERS)


def clip_change_fragment(value: str, limit: int = 260) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 1].rstrip()}..."


def text_change_context(text: str, start: int, end: int) -> str:
    if not text:
        return ""
    context_start = max(start - 60, 0)
    context_end = min(max(end, start) + 180, len(text))
    fragment = text[context_start:context_end]
    if context_start > 0:
        fragment = f"...{fragment}"
    if context_end < len(text):
        fragment = f"{fragment}..."
    return clip_change_fragment(fragment)


def html_change_summary(previous_excerpt: str, current_excerpt: str) -> str:
    previous = re.sub(r"\s+", " ", previous_excerpt).strip()
    current = re.sub(r"\s+", " ", current_excerpt).strip()
    if not previous or not current or previous == current:
        return ""

    prefix_len = 0
    max_prefix = min(len(previous), len(current))
    while prefix_len < max_prefix and previous[prefix_len] == current[prefix_len]:
        prefix_len += 1

    suffix_len = 0
    max_suffix = min(len(previous), len(current)) - prefix_len
    while suffix_len < max_suffix and previous[-1 - suffix_len] == current[-1 - suffix_len]:
        suffix_len += 1

    previous_end = len(previous) - suffix_len if suffix_len else len(previous)
    current_end = len(current) - suffix_len if suffix_len else len(current)
    before = text_change_context(previous, prefix_len, previous_end)
    after = text_change_context(current, prefix_len, current_end)

    parts = []
    if after:
        parts.append(f"变化后片段：{after}")
    if before:
        parts.append(f"变化前片段：{before}")
    return "；".join(parts)


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
    signature_changed = sync_source_signature(source, source_state, now)
    invalid_current = html_diff_snapshot_invalid(html_text, excerpt)
    invalid_previous = bool(source_state.get("baseline_invalid")) or html_diff_snapshot_invalid("", previous_excerpt)
    if not signature_changed and previous == fingerprint and previous_excerpt == excerpt:
        return []
    source_state["fingerprint"] = fingerprint
    source_state["last_changed_at"] = iso_or_none(now)
    source_state["last_excerpt"] = excerpt
    if invalid_current:
        source_state["baseline_invalid"] = True
        return []
    source_state.pop("baseline_invalid", None)
    if signature_changed or not previous or invalid_previous:
        source_state["baseline_initialized"] = True
        return []
    if previous == fingerprint:
        return []
    change_summary = html_change_summary(previous_excerpt, excerpt)
    if not change_summary:
        source_state["last_suppressed_reason"] = "html_diff_without_readable_change"
        return []
    source_state.pop("last_suppressed_reason", None)

    return [
        NewsItem(
            title=f"{source.name} 页面发生更新",
            url=source.url,
            summary=(
                f"检测到 {source.entity or source.name} 的页面内容变化。"
                f"{change_summary}。推送规则：{source.push_rule or source.method}。"
            ),
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
    signature_changed = False
    if source_state is not None:
        signature_changed = sync_source_signature(source, source_state, now)
        previous = dict(source_state.get("models", {}))

    current: dict[str, str] = {}
    items: list[NewsItem] = []
    for model in models:
        model_id = str(model.get("modelId") or model.get("id") or "").strip()
        if not model_id:
            continue
        last_modified = str(model.get("lastModified") or "").strip()
        current[model_id] = last_modified
        if source_state is not None and (signature_changed or not previous):
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
        if signature_changed or current != previous:
            source_state["models"] = current
            source_state["last_changed_at"] = iso_or_none(now)
        if signature_changed or not previous:
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


def github_new_repository_alert_enabled(source: Source) -> bool:
    return "new_repository" in source.push_rule.lower()


def github_repo_created_recently(repo: dict[str, Any], now: datetime, days: int = 30) -> bool:
    created_at = parse_datetime(str(repo.get("created_at") or ""))
    if created_at is None:
        return False
    return created_at >= now.astimezone(timezone.utc) - timedelta(days=days)


def collect_github_repo_items(
    source: Source,
    state: dict[str, Any] | None,
    now: datetime,
) -> list[NewsItem]:
    repos = fetch_github_repos(source)
    source_state = get_source_state(state, source)
    previous: dict[str, str] = {}
    signature_changed = False
    if source_state is not None:
        signature_changed = sync_source_signature(source, source_state, now)
        previous = dict(source_state.get("repos", {}))

    current: dict[str, str] = {}
    items: list[NewsItem] = []
    for repo in repos[:30]:
        full_name = str(repo.get("full_name") or repo.get("name") or "").strip()
        if not full_name:
            continue
        updated_at = github_repo_sort_time(repo)
        current[full_name] = updated_at
        if source_state is not None and (signature_changed or not previous):
            continue
        if source_state is not None and full_name in previous:
            continue
        if not github_new_repository_alert_enabled(source):
            continue
        if not github_repo_created_recently(repo, now):
            continue

        url = str(repo.get("html_url") or "").strip() or f"https://github.com/{full_name}"
        description = str(repo.get("description") or "").strip()
        language = str(repo.get("language") or "").strip()
        topics = [str(topic) for topic in repo.get("topics", [])[:8] if topic]
        tags = [tag for tag in [source.entity, source.category, source.method, "GitHub", language, *topics] if tag]
        summary = f"{source.name} 检测到新增官方 GitHub 仓库：{full_name}"
        if description:
            summary += f"。{description}"
        items.append(
            NewsItem(
                title=f"{source.name} 新增仓库：{full_name}",
                url=url,
                summary=summary,
                published_at=iso_or_none(parse_datetime(str(repo.get("created_at") or "")) or parse_datetime(updated_at) or now),
                source_id=source.id,
                source_name=source.name,
                tags=tags,
                score=0.0,
            )
        )

    if source_state is not None:
        if signature_changed or current != previous:
            source_state["repos"] = current
            source_state["last_changed_at"] = iso_or_none(now)
        if signature_changed or not previous:
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

    if source.method in {"tikhub_wechat_account_articles", "tikhub_wechat_search"}:
        items = collect_tikhub_wechat_items(source, state, now)
        source_state = get_source_state(state, source)
        raw_count = int(source_state.get("last_raw_count", len(items))) if source_state is not None else len(items)
        return items, raw_count

    if source.method == "aihot_api":
        items = collect_aihot_items(source, state, now)
        source_state = get_source_state(state, source)
        raw_count = int(source_state.get("last_raw_count", len(items))) if source_state is not None else len(items)
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
