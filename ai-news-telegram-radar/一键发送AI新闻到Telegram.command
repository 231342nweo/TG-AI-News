#!/bin/zsh
cd "$(dirname "$0")"
if [ ! -f ".env" ]; then
  echo "还没有 Telegram 配置，先进入配置向导。"
  echo
  python3 tools/setup_telegram.py || {
    echo
    echo "配置没有完成。按回车关闭窗口。"
    read
    exit 1
  }
fi

echo "正在抓取 AI 新闻并发送到 Telegram..."
echo
python3 -m ai_news_radar run --config config/sources.json --send --show
echo
echo "完成。按回车关闭窗口。"
read
