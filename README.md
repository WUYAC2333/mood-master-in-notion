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

## 时效说明

GitHub Actions 定时任务最小间隔 5 分钟，且高峰可能延迟 ~15 分钟。
所以是「记完几分钟到十几分钟后收到回应」，不是即时。对反思日记足够。

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
5. 完成。`.github/workflows/poll.yml` 每 7 分钟跑一次，`letter.yml` 每周一来信。
   也可在 Actions 页手动 `Run workflow` 立即触发。

> 公开仓库 = 代码公开，但你的**日记在 Notion（私有）、密钥在 Secrets（加密）**，都不公开。
> 注意：超过 60 天无仓库活动，GitHub 会暂停定时任务——偶尔提交或手动触发一次即可。

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
