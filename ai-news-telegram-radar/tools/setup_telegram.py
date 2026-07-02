from __future__ import annotations

import getpass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return "replace_me" in lowered or "your_" in lowered or "你的" in value


def write_env(token: str, chat_id: str) -> None:
    ENV_PATH.write_text(
        "\n".join(
            [
                "# Telegram settings for AI News Telegram Radar",
                "# Keep this file private. It is ignored by git.",
                f"TELEGRAM_BOT_TOKEN={token}",
                f"TELEGRAM_CHAT_ID={chat_id}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> int:
    values = read_env()
    token = values.get("TELEGRAM_BOT_TOKEN")
    chat_id = values.get("TELEGRAM_CHAT_ID")

    print("Telegram 配置向导")
    print("需要两样东西：BotFather 给你的 bot token，以及频道用户名/频道 ID。")
    print()

    if is_placeholder(token):
        token = getpass.getpass("请输入 TELEGRAM_BOT_TOKEN（输入时不会显示）：").strip()
    else:
        keep = input("已发现 TELEGRAM_BOT_TOKEN，继续使用它吗？[Y/n] ").strip().lower()
        if keep == "n":
            token = getpass.getpass("请输入新的 TELEGRAM_BOT_TOKEN（输入时不会显示）：").strip()

    if is_placeholder(chat_id):
        chat_id = input("请输入 TELEGRAM_CHAT_ID，例如 @your_channel_username：").strip()
    else:
        keep = input(f"已发现 TELEGRAM_CHAT_ID={chat_id}，继续使用它吗？[Y/n] ").strip().lower()
        if keep == "n":
            chat_id = input("请输入新的 TELEGRAM_CHAT_ID，例如 @your_channel_username：").strip()

    if not token or not chat_id:
        print("配置未完成：token 和 chat id 都不能为空。")
        return 1

    write_env(token, chat_id)
    print(".env 已保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
