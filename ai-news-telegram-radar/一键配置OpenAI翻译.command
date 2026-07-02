#!/bin/zsh
cd "$(dirname "$0")"
echo "配置 OpenAI / LiteLLM 自动翻译。"
echo
python3 tools/setup_openai.py || {
  echo
  echo "配置没有完成。按回车关闭窗口。"
  read
  exit 1
}
echo
echo "现在运行一次新闻预览，确认是否出现中文译文。"
python3 -m ai_news_radar run --config config/sources.json --show
echo
echo "完成。按回车关闭窗口。"
read
