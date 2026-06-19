# MOOD

挂靠 Notion 的个人情绪日记 AI 工具。在 Notion 里记录心情，由你自己的 Claude / GPT API
自动识别情绪、按需生成回应、定期来信，并把 `@实体` 关联成你的关系网络。

没有公开服务、没有前端、不对外暴露任何接口——它只是一个定时运行的脚本，
跑在 GitHub Actions（免费，公开仓库）或你自己的电脑上。

## 它能做什么

- **记录**：在 Notion 的 Entries 数据库里写心情/心里话（正文写在页面里）。
- **情绪识别**：脚本轮询新记录，用便宜小模型打情绪标签（写回「情绪」属性）。
- **选择性回应**：勾选「求回应」，下次轮询时用你的好模型生成一段贴近事实、不灌鸡汤的回应，
  作为 callout 追加到记录页里。不勾就只记录、不回应。
- **定期来信**：每周综合近期记录，写一封诚实温暖的信，存到 Letters 数据库。
- **@实体网络**：正文里写 `@[名称]`，自动建实体并把记录关联过去；在实体页用反向关联看全部相关记录。
- **规则 agent**：在 Rules 数据库定义「触发条件 + 提醒话术」（如反事实大师），
  命中的记录会被追加一条提醒。
- **情绪泡泡图**：统计近 7 天识别出的情绪关键词，用不同颜色的气泡按频率展示
  （暖色=积极、冷色=消极），部署在 GitHub Pages，可 embed 进 Notion。

## 时效说明

四个工作流都只用 `workflow_dispatch` 触发，由外部定时器（cron-job.org，免费）
按精确时间调 GitHub API 唤起 —— **不用 GitHub 自带的 `schedule`**，因为它“尽力而为”，
高峰常延迟数小时甚至跳过（曾把“当晚 22:00”的日总结拖到次日凌晨）。
`workflow_dispatch` 不受此影响，runner 几秒内即起。

即便某次触发漏了，`run.py` 的自愈逻辑（只处理“没打勾”的记录）也会在下次补上，
不会重复、不会永久漏处理。外部定时器的配置见下方「外部定时器」一节。

## 关于 @实体的写法

中文没有空格，`@张三` 这种裸写法无法判断名字到哪里结束。
**请用方括号形式 `@[张三]`、`@[毕业论文]`**，解析稳定可靠。
裸写法仅在后面紧跟标点或空格时才准确。

## 部署（GitHub Actions，免费）

1. **建 Notion integration**：https://www.notion.so/my-integrations 创建 internal integration，
   拿到 `secret_xxx` token。
2. **建父页面**：Notion 新建空白页「MOOD」，右上 `...` → 连接 → 选你的 integration。
   复制页面链接末尾 32 位 ID。
3. **建数据库**：本地运行（需先 `pip install -r requirements.txt`）：
   ```bash
   NOTION_TOKEN=secret_xxx python setup_notion.py <父页面ID>
   ```
   记下打印的四个 DB ID。
4. **推到公开仓库**，在仓库 Settings → Secrets and variables → Actions 添加：
   - `NOTION_TOKEN`
   - `NOTION_ENTRIES_DB` / `NOTION_ENTITIES_DB` / `NOTION_LETTERS_DB` / `NOTION_RULES_DB`
   - `CLASSIFIER_API_KEY`（小模型用）
   - `RESPONDER_API_KEY`（好模型用，可与上面相同）
5. 完成。四个工作流都靠 `workflow_dispatch` 触发，由外部定时器精确唤起
   （见「外部定时器」一节）。也可随时在 Actions 页手动 `Run workflow` 立即触发。

> 公开仓库 = 代码公开，但你的**日记在 Notion（私有）、密钥在 Secrets（加密）**，都不公开。

## 外部定时器（治 GitHub schedule 的延迟）

用免费的 cron-job.org 按精确时间调 GitHub 的 `workflow_dispatch` API 来触发工作流，
绕开 `schedule` 动辄数小时的延迟。一次性配置，之后零维护。

1. **建细粒度 token**：https://github.com/settings/personal-access-tokens/new
   - Repository access → Only select repositories → 选本仓库
   - Permissions → Repository → **Actions: Read and write**（其余 No access）
   - 生成后复制 `github_pat_xxx`（只显示一次）
2. **注册 cron-job.org**，账户设置里把时区设为 `Asia/Shanghai`。
3. **为每个工作流建一个 cronjob**，URL（替换 `<文件名>`）：
   ```
   https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/<文件名>/dispatches
   ```
   - Method: `POST`
   - Headers：
     `Accept: application/vnd.github+json`、
     `Authorization: Bearer github_pat_xxx`、
     `X-GitHub-Api-Version: 2022-11-28`
   - Body：`{"ref":"main"}`
   - 时间：poll.yml 每 15 分钟、bubbles.yml 每小时、daily.yml 每天 22:00、
     letter.yml 每周日 22:30。
4. 每个建好后点 TEST RUN，到 Actions 页确认对应工作流被触发即成功。

> `workflow_dispatch` 要求工作流文件已在默认分支（main）上，所以先推送、再去 cron-job.org 配置。

## 情绪泡泡图（GitHub Pages）

近 7 天的情绪关键词，做成不同颜色的气泡（暖色=积极、冷色=消极），频率越高气泡越大。

- 数据：`mood/bubbles.py` 只读 Notion 已识别好的「情绪」属性，不调模型、几乎零成本。
- 渲染：`docs/index.html`（D3 力导向真气泡），浏览器端 `fetch ./data.json`。
- 调度：`.github/workflows/bubbles.yml` 定时统计 + 部署，**不往仓库提交任何文件**。

**一次性开启 Pages**（仓库 Settings → Pages）：把 **Source** 选成 **GitHub Actions**。
之后 `bubbles` 工作流每次跑完会自动发布，地址形如
`https://<用户名>.github.io/<仓库名>/`。

**嵌入 Notion**：在日记页输入 `/embed`，粘贴上面的 Pages 地址即可。

**以后微调**：配色改 `mood/bubbles.py` 的 `EMOTION_COLORS`；字体/字号/气泡大小改
`docs/index.html` 的 CSS 与半径数值。推上去后 Pages 自动重新部署。

本地预览：`python -m mood.run --task bubbles --out docs/data.json` 生成数据后，
`python -m http.server` 起服务，浏览器访问 `docs/index.html`（直接双击会被 CORS 拦住）。

## 本地运行

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 填入明文 token 和 key
python -m mood.run --task all        # 日常轮询
python -m mood.run --task letter     # 手动来信
```

## 切换模型

改 `config.yaml` 里 `classifier` / `responder` 的 `provider`（`anthropic`/`openai`）和 `model`。
情绪识别任务简单，可用最便宜的；回应和来信建议用最好的。

## 结构

```
mood/
  config.py     配置加载 + 环境变量替换
  llm.py        anthropic/openai 统一封装
  notion.py     Notion 读写
  classify.py   情绪识别
  entities.py   @实体解析
  generate.py   回应 / 来信 / 规则提醒
  run.py        主流程（classify / respond / rules / letter）
setup_notion.py 一次性建库脚本
```
