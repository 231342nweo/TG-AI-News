# AI News Telegram Radar

一个给个人 Telegram 频道用的轻量 AI 新闻信源提醒器。

它借鉴了 `LearnPrompt/ai-news-radar` 的思路：先抓取公开 RSS/Atom 信源，再做 AI 关键词评分、时间窗口过滤、去重和排序，最后生成一条适合发到 Telegram 频道的摘要。

## 现在能做什么

- 读取 `config/sources.json` 里的 RSS/Atom 信源
- 按最近 24 小时窗口过滤
- 用关键词和来源权重给新闻打分
- 自动去重、排序、限制条数
- 限制单个信源刷屏，避免 arXiv 这类高频源占满频道
- 生成中文 Telegram 摘要，展示时间使用北京时间
- 每条消息自动生成“中文看点”，减少英文摘要刷屏；原文标题和链接保留，方便核对来源
- 可选接入 OpenAI 自动翻译，把英文标题和摘要翻成中文
- 生成 `data/latest.json`、`data/telegram-message.txt`、`data/telegram-message.html`
- 填入 Telegram Bot Token 后发送到频道
- 用 GitHub Actions 每 5 分钟检查一次，有新内容才自动发送
- 已整理好云端部署说明：`docs/GITHUB_ACTIONS_DEPLOY.md`
- 用本地 demo 离线跑通，不依赖任何第三方 Python 包

## 1. 先跑通离线 demo

```bash
cd /Users/zhuzhiqin/Documents/小号AI信息源/ai-news-telegram-radar
python3 -m ai_news_radar run --config config/sources.demo.json --show
```

跑完会看到一条 Telegram 摘要预览，同时生成：

- `data/latest.json`
- `data/telegram-message.txt`
- `data/telegram-message.html`

## 2. 跑真实 RSS 信源

```bash
python3 -m ai_news_radar run --config config/sources.json --show
```

如果你这台机器当前网络能访问外部 RSS 和公开网页，就会抓取真实源。当前 `config/sources.json` 已替换为“中国 AI 信息源”目录：共 64 个信源，每条都包含 `entity`、`category`、`url`、`method`、`priority`、`push_rule`。当前 64 个源已启用，支持 RSS/GitHub Atom，以及 `html_diff`、`html_list`、`huggingface_api`、`github_repos_api`、`modelscope_html`、`policy_keyword_html`、`tikhub_wechat_account_articles`、`tikhub_wechat_search` 采集器。新增源会先建立状态基线，后续发现新链接、页面变化、模型更新或微信文章更新才推送。OpenXLab 已使用 GitHub 项目 API 作为备用采集；中国信通院和国家数据局已改为优先尝试更具体的栏目页，再回退到官网根域。

媒体源只保留三家：

- 机器之心
- 量子位
- Founder Park

Founder Park 使用 `html_diff` 监测页面变化。TikHub 微信补充源目前包含「第一财经」固定公众号文章列表，以及「21世纪经济报道」来源名过滤兼容采集。频道消息会用中文字段和中文看点呈现，时间统一显示为北京时间；原文标题会保留，方便追溯来源。

后面你只需要改 `config/sources.json` 里的 `sources` 列表：

```json
{
  "id": "my-source",
  "name": "My Source",
  "url": "https://example.com/feed.xml",
  "weight": 2
}
```

`weight` 越高，越容易排到摘要前面。官方博客、你很信任的源可以给 `2` 到 `3`；噪音较多的综合媒体可以给 `1`。

如果某个源更新太多，可以给它加 `max_items`，例如：

```json
{
  "id": "arxiv-cs-ai",
  "name": "arXiv cs.AI",
  "url": "https://rss.arxiv.org/rss/cs.AI",
  "weight": 1,
  "max_items": 2
}
```

## 3. 接入 Telegram 频道

1. 在 Telegram 找 `@BotFather`，创建 bot，拿到 `TELEGRAM_BOT_TOKEN`
2. 创建或打开你的频道
3. 把 bot 加进频道，并设为管理员
4. 如果频道有公开用户名，`TELEGRAM_CHAT_ID` 填 `@频道用户名`
5. 如果是私密频道，后续可以再用 Telegram API 查 `-100...` 形式的频道 ID

复制配置文件：

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
TELEGRAM_BOT_TOKEN=你的_bot_token
TELEGRAM_CHAT_ID=@你的频道用户名
```

先发一条测试消息：

```bash
python3 -m ai_news_radar test-telegram
```

确认频道能收到后，再发送新闻摘要：

```bash
python3 -m ai_news_radar run --config config/sources.json --send --show
```

## 4. 开启自动翻译

没有 OpenAI API Key 时，系统会继续使用“原文标题 + 中文看点”的安全模式。

如果要把标题和摘要也翻成中文，双击：

```text
一键配置OpenAI翻译.command
```

它支持两种方式：

- OpenAI 官方 API Key
- LiteLLM Virtual Key

如果你用 LiteLLM Virtual Key，配置时这样填：

- API Key：你的 LiteLLM Virtual Key
- Base URL：你的 LiteLLM Proxy 地址；如果你只知道后台 UI 地址，填 `https://your-litellm.example.com/ui` 也可以
- 模型/模型别名：LiteLLM 里允许这个 key 调用的模型名或 alias

也可以手动在 `.env` 里加入：

```bash
OPENAI_API_KEY=你的_openai_key_或_litellm_virtual_key
OPENAI_BASE_URL=https://your-litellm.example.com
OPENAI_TRANSLATION_MODEL=你的_litellm_模型名或别名
OPENAI_TRANSLATION_API=chat
OPENAI_TRANSLATION_ENABLED=1
```

如果你直接用 OpenAI 官方接口，`OPENAI_BASE_URL` 保持 `https://api.openai.com/v1` 即可。

然后预览：

```bash
python3 -m ai_news_radar test-translation
python3 -m ai_news_radar run --config config/sources.json --show
```

确认效果后发送：

```bash
python3 -m ai_news_radar run --config config/sources.json --send --show
```

翻译失败时不会中断频道发送，会自动退回到“原文标题 + 中文看点”。

## 5. 放到 GitHub Actions 自动跑

这个项目已经带了 `.github/workflows/telegram-news.yml`，默认每 5 分钟跑一次。

工作流会把 `data/source-state.json` 提交回仓库，用来记住已经发过的链接和页面指纹；没有新内容时保持静默，有新内容才向频道发送提醒。

当前本地 Codex 自动任务已经停用，建议后续只保留 GitHub Actions 云端运行，避免本机和云端同时检查同一频道。

完整步骤见：

```text
docs/GITHUB_ACTIONS_DEPLOY.md
```

你需要在自己的 GitHub 仓库里设置这些 Secrets：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OPENAI_API_KEY`（可选，用于自动翻译）
- `OPENAI_BASE_URL`（可选；使用 LiteLLM 时填写 Proxy 根地址或 `/v1` 地址，不要填后台 `/ui` 页面）
- `OPENAI_TRANSLATION_MODEL`（可选；使用 LiteLLM 时填写允许调用的模型名或 alias）
- `TIKHUB_API_KEY`（可选，用于微信补充采集）

然后手动触发一次 `Send AI News To Telegram` 工作流，确认频道收到消息。

## 6. 下一步怎么改成你的版本

建议先不要急着加很多源。第一轮可以这样做：

1. 保留 5 到 10 个高质量源
2. 每天看 Telegram 摘要是否太吵
3. 太吵就降低综合源的 `weight` 或提高 `settings.min_score`
4. 漏掉重要消息就补关键词，或提高官方源权重
5. 跑稳定后，再把你的自媒体、Newsletter、中文信源逐个加进去

`config/sources.json` 里有一个默认关闭的 Anthropic 社区 RSS。Anthropic 新闻页目前没有清晰公开的官方 RSS 入口，所以这里先不默认启用；如果你接受社区维护源，可以把它的 `enabled` 改成 `true`。
