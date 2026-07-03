# GitHub Actions 云端部署

这份说明用于把 AI 新闻提醒搬到 GitHub Actions。搬上去之后，本机不用定时运行；GitHub 会按 `.github/workflows/telegram-news.yml` 每 5 分钟自动检查，有新内容才发到 Telegram。

## 当前状态

- 本地 Codex 自动任务已经停用。
- GitHub Actions 工作流已经设置为每 5 分钟运行一次。
- `data/source-state.json` 已经保留当前信源基线，云端第一次运行时不会把旧内容重新刷屏。
- `.env` 不会被上传，Telegram token 和 LiteLLM key 需要放到 GitHub Secrets。

## 1. 创建仓库

推荐创建一个新的 Private 仓库，例如：

```text
ai-news-telegram-radar
```

如果使用 GitHub Desktop：

1. 打开 GitHub Desktop。
2. 选择 `File` -> `Add Local Repository...`。
3. 选中这个文件夹：

   ```text
   /Users/zhuzhiqin/Documents/小号AI信息源/ai-news-telegram-radar
   ```

4. GitHub Desktop 会识别到当前文件夹已经是本地 Git 仓库。
5. 点击 `Publish repository`，建议保持 Private。

如果已经在 GitHub 网页上创建好空仓库，也可以把仓库地址发回来，我继续接手后面的推送检查。

## 2. 设置 GitHub Secrets

进入你的 GitHub 仓库：

```text
Settings -> Secrets and variables -> Actions -> New repository secret
```

添加这些 Secrets：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
OPENAI_API_KEY
OPENAI_BASE_URL
OPENAI_TRANSLATION_MODEL
TIKHUB_API_KEY
```

当前频道可这样填：

```text
TELEGRAM_CHAT_ID=@hq_ai_news
OPENAI_BASE_URL=https://aaii.xclaw.info
OPENAI_TRANSLATION_MODEL=gpt-5.5
```

`TELEGRAM_BOT_TOKEN`、`OPENAI_API_KEY` 和 `TIKHUB_API_KEY` 填你自己的密钥。不要把 `.env` 文件上传到 GitHub。

TikHub 当前只作为微信搜索补充源，默认每小时请求一次，用来控制 API 调用成本。

## 3. 确认 Actions 权限

进入：

```text
Settings -> Actions -> General -> Workflow permissions
```

确认允许 GitHub Actions 写入仓库内容。这个项目需要把 `data/source-state.json` 提交回仓库，避免重复推送。

如果页面里有选项，选择：

```text
Read and write permissions
```

## 4. 手动跑第一次

进入：

```text
Actions -> Send AI News To Telegram -> Run workflow
```

第一次运行完成后看两件事：

- 如果没有新内容，频道不会收到提醒，这是正常的。
- 如果有新内容，频道会收到一条中文 AI 新闻摘要。

## 5. 后续运行规则

- GitHub Actions 每 5 分钟运行一次。
- 时间展示统一是北京时间。
- 没有新内容时保持静默，有新内容才发提醒。
- 翻译接口失败时不会中断发送，会退回到“原文标题 + 中文看点”。
- 后续改信源只需要编辑 `config/sources.json`。
