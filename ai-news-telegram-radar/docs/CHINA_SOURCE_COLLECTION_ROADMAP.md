# 中国 AI 信息源采集路线图

当前 `config/sources.json` 是信源目录，不等于全部立即采集。

规则很简单：只有采集器成熟、输出稳定、噪音可控的源才直接启用；网络不稳定的源会先接备用入口或降级采集器。

## 当前状态

- 总信源：62
- 当前启用：62
- 已支持方法：`rss`、`atom`、`github_atom`、`github_repos_api`、`html_diff`、`html_list`、`huggingface_api`、`modelscope_html`、`policy_keyword_html`
- S/A/B 级采集器已全部接入；原先 3 个网络不稳定源已改为尝试采集
- 新增源都会先建立状态基线，后续发现新链接、页面变化或模型更新才推送
- 当前启用方法数量：`html_diff` 20、`github_atom` 12、`huggingface_api` 11、`html_list` 9、`policy_keyword_html` 6、`modelscope_html` 2、`github_repos_api` 1、`rss` 1
- OpenXLab 使用 GitHub 项目 API 做 B 级备用信号；中国信通院和国家数据局优先尝试具体栏目页，再回退到官网根域
- 媒体源只保留：机器之心、量子位、Founder Park
- Founder Park 使用 `html_list`，已启用，但只做新链接监测

## 接入顺序

### 第一批：S 级官方更新页（已接入）

优先接 `html_diff` 和 `html_list`，因为这些最可能产生频道真正需要的信号：

- 新模型发布
- API 能力变化
- 上下文长度变化
- 价格变化
- 开源权重发布
- 官方公告

代表源：

- DeepSeek API 更新
- DeepSeek 官方新闻
- Kimi API 文档
- 智谱 BigModel 新品发布
- Z.ai Release Notes
- MiniMax Release Notes
- 阶跃星辰开放平台
- Qwen Blog
- 阿里云百炼模型列表
- 百度千帆文档
- 腾讯混元更新历史
- 火山方舟模型列表 / 产品更新

### 第二批：模型平台（已接入）

接 `huggingface_api` 和 `modelscope_html`。

目标不是抓所有动态，而是只推：

- 新模型仓库
- 权重更新
- README 或 model card 关键变化
- 下载量/热度异常上升

代表源：

- DeepSeek Hugging Face
- Qwen Hugging Face
- Z.ai Hugging Face
- MiniMax Hugging Face
- OpenBMB Hugging Face
- 魔搭社区 ModelScope

### 第三批：政策和标准（已接入）

接 `policy_keyword_html`。

这类源更新少，但重要性高。关键词应更严格：

- 生成式人工智能
- 大模型
- 算法备案
- 内容标识
- 数据安全
- 算力
- 标准

代表源：

- 国家网信办生成式 AI 备案公告
- 工信部 AI 政策
- 中国信通院 AI 报告
- 信安标委 TC260
- 国家数据局

### 第四批：B 级社区和访谈（已接入）

接 `html_list`，但默认只进日报，不做即时推送。

代表源：

- Kimi Forum
- OpenXLab
- Founder Park

## 每接一个采集器的验收标准

1. 能稳定返回结构化条目：标题、链接、摘要、发布时间、来源。
2. 单源不会刷屏，必须有 `max_items`。
3. 有噪音过滤，例如 GitHub 管理事件、论坛闲聊、重复列表页。
4. 至少跑 3 次不影响 Telegram 发送。
5. 先 `--show` 预览，再打开 `enabled: true`。

## 快速审计

```bash
python3 tools/audit_sources.py
```

## 不建议现在做的事

- 不要一次性把 48 个未启用源全部打开。
- 不要用通用网页抓取直接推送到频道。
- 不要把政策站、论坛、官网首页当成 RSS 源硬解析。
- 不要让翻译模型替代事实判断；翻译只负责中文化，不负责补事实。
