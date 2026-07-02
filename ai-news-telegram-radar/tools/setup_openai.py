from __future__ import annotations

import getpass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


def read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def parse_env(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def upsert(lines: list[str], key: str, value: str) -> list[str]:
    replaced = False
    updated: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            updated.append(f"{key}={value}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(f"{key}={value}")
    return updated


def main() -> int:
    lines = read_env_lines()
    values = parse_env(lines)
    existing_key = values.get("OPENAI_API_KEY", "")
    existing_model = values.get("OPENAI_TRANSLATION_MODEL", DEFAULT_MODEL)
    existing_base_url = values.get("OPENAI_BASE_URL") or values.get("LITELLM_BASE_URL") or DEFAULT_BASE_URL

    print("OpenAI / LiteLLM 自动翻译配置向导")
    print("需要一个 API Key。OpenAI key 或 LiteLLM Virtual Key 都可以。")
    print("如果使用 LiteLLM，还需要你的 LiteLLM Proxy base URL，通常形如：https://your-litellm.example.com/v1")
    print()

    if existing_key and "replace_me" not in existing_key:
        keep = input("已发现 OPENAI_API_KEY，继续使用它吗？[Y/n] ").strip().lower()
        api_key = existing_key if keep != "n" else getpass.getpass("请输入新的 OPENAI_API_KEY：").strip()
    else:
        api_key = getpass.getpass("请输入 OPENAI_API_KEY：").strip()

    base_url = input(f"接口 Base URL，可直接填 LiteLLM UI 地址 [{existing_base_url or DEFAULT_BASE_URL}]：").strip() or existing_base_url or DEFAULT_BASE_URL
    base_url = base_url.rstrip("/")
    if base_url.endswith("/ui"):
        base_url = base_url[:-3].rstrip("/")
    model = input(f"翻译模型/模型别名 [{existing_model or DEFAULT_MODEL}]：").strip() or existing_model or DEFAULT_MODEL

    if not api_key:
        print("配置未完成：API Key 不能为空。")
        return 1

    lines = upsert(lines, "OPENAI_API_KEY", api_key)
    lines = upsert(lines, "OPENAI_BASE_URL", base_url.rstrip("/"))
    lines = upsert(lines, "OPENAI_TRANSLATION_MODEL", model)
    lines = upsert(lines, "OPENAI_TRANSLATION_API", "chat")
    lines = upsert(lines, "OPENAI_TRANSLATION_ENABLED", "1")
    ENV_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(".env 已保存，自动翻译已开启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
