#!/bin/zsh
cd "$(dirname "$0")"
echo "正在运行离线 demo，不需要网络和 Telegram 配置..."
echo
python3 -m ai_news_radar run --config config/sources.demo.json --show
echo
echo "完成。按回车关闭窗口。"
read
