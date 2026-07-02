#!/bin/zsh
cd "$(dirname "$0")"
echo "正在抓取真实 RSS/Atom 信源..."
echo
python3 -m ai_news_radar run --config config/sources.json --show
echo
echo "完成。按回车关闭窗口。"
read
