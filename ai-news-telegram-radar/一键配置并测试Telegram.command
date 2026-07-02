#!/bin/zsh
cd "$(dirname "$0")"
echo "先配置 Telegram，然后发送一条测试消息。"
echo
python3 tools/setup_telegram.py || {
  echo
  echo "配置没有完成。按回车关闭窗口。"
  read
  exit 1
}
echo
python3 -m ai_news_radar test-telegram
echo
echo "如果你的频道收到了测试消息，就说明 Telegram 已接通。按回车关闭窗口。"
read
