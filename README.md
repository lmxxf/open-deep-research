# 轻量 Deep Research：把搜索、写作和修改拆开

这是一个用 DeepSeek 做研究报告的小原型。我们想检查的是：同一个模型，换一种任务拆分、资料处理和人机交互方式，能不能更快交出可用的中文稿。

A 模式模拟常见的工程误用：先铺开问题，再逐项研究、摘要，最后拼成长报告。它不是 GPT-Researcher、LangChain 或任何开源项目的原版复现，也不代表这些项目的最佳配置。B 模式先对齐需求，按问题寻找证据，用中文写作者统一成稿，之后允许局部修改和按需补搜。

这个项目不能凭一次运行判定哪个模型更强。A/B 的流程、提示词、推理开关与并发设置都会影响结果，需要分别控制和比较。

## 环境

需要 Python 3.10 或更新版本。模型通过 DeepSeek 兼容接口调用；搜索不要求付费搜索 API Key，但公网搜索和网页读取仍可能受到限流、网络或站点限制。

模型配置使用环境变量：

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 模型 API Key |
| `DEEPSEEK_BASE_URL` | 模型接口地址，使用代码默认值时可省略 |
| `DEEPSEEK_MODEL` | 模型名称，使用代码默认值时可省略 |

默认模型为 `deepseek-flash`，默认接口为 `https://api.deepseek.com`。项目根目录的 `.env` 优先于 shell 中已有的同名环境变量，避免其他会话遗留的配置覆盖本项目；`--model` 可显式覆盖模型名。

先创建虚拟环境，再安装项目及测试依赖：

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
python -m pip install -e '.[test]'
```

搜索依次尝试 DuckDuckGo Lite、DDGS Yahoo、DuckDuckGo HTML、Bing RSS；网页正文先用 trafilatura 提取，失败时尝试 Jina Reader。当前网络实测 Lite 能返回具体中英文业务资料；Bing RSS 对长查询可能只返回品牌首页，因此放在最后。无需另买搜索 Key，不代表这些服务在国内各地区都稳定或永远可用。没有抓到有效正文时程序报错并保留失败记录，不拿模型记忆补成“调研”。

## 生成报告

在项目根目录运行。下面各条命令是相互独立的例子。

```bash
python -m deep_research run --mode A --question '小团队怎样评估是否需要自建 Deep Research？' --profile serial-thinking --output runs/a
```

```bash
python -m deep_research run --mode B --question '小团队怎样评估是否需要自建 Deep Research？' --profile parallel-fast --output runs/b --interactive
```

`serial-thinking` 和 `parallel-fast` 是执行配置，不是产品名。默认 A 走串行推理配置，B 走快速配置；比较默认体验时允许这些差异，同时必须在报告中说明。若想判断单个设计的收益，需要另做同配置对照。

研究范围通过 `--max-pages`（默认 6，范围 1–20）、`--query-count`（默认 3，范围 1–5）、`--max-rounds`（默认 1，范围 0–2）控制。`query-count` 限制初始查询，B 的补查可以另加查询；`max-rounds` 是 B 的缺口检查与补查轮次上限。允许补查时，B 首轮尽量预留两页额度；`max-pages` 是整次研究的有效来源上限，不会每轮翻倍。这些上限不保证取得对应数量的有效资料。

默认还有 600 秒整体期限、32 次模型请求上限、120 秒模型超时、15 秒网络超时及每次模型输出 8192 Token 上限。可用 `python -m deep_research run --help` 查看对应参数。

思考内容也占输出预算。实测 A 的长文节点曾达到 8192 上限，程序将其记录为失败，不提交截断稿。`experiments/run_demo.sh` 因而对三组统一使用 `--max-tokens 16384`；这是上限，不代表每次都会用满。

## 先对齐，再研究

```bash
python -m deep_research align --question '小团队怎样评估是否需要自建 Deep Research？' --output runs/alignment
```

`align` 仅生成对齐建议，结果在 `runs/alignment/alignment.json`；`--output` 是新目录，不是文件名，也不会直接启动研究。所有运行入口都拒绝覆盖已有非空输出目录。

B 模式可以传入 `--brief-file`、`--answers-file` 和 `--style-sample`，分别提供 UTF-8 研究简报、澄清答案和文风样本；简报与答案也可通过 `--brief`、`--answers` 直接传字符串。简报应说清楚读者、用途、研究范围、时间口径、篇幅与需要回答的问题。

如果模型仍提出澄清问题，`--interactive` 会在终端询问。非交互运行会保留 `state.json`，状态为 `needs_alignment`，并以退出码 2 结束；把回答写入文件后，使用新的输出目录重跑。只有显式添加 `--accept-assumptions` 才允许带着未确认假设继续，这些假设会记录在产物里，不冒充用户偏好。

```bash
python -m deep_research run --mode B --question-file experiments/question.txt --brief-file experiments/brief.txt --answers-file experiments/answers.txt --style-sample experiments/style_sample.txt --output runs/b-fixture
```

这四个 `experiments/*.txt` 是固定实验输入；其中答案和文风样本是为实验编写的设定，不是用户口述或真实客户原稿。英文来源可以保留原始技术信息，中文成稿应按中文读者的表达习惯重组。文风样本帮助确定声音，不覆盖事实与出处。

## 改已有稿，不必从头重跑

```bash
python -m deep_research revise --run-dir runs/b --section P2 --instruction '这一节改成给程序员看的三段话，先讲操作过程，再讲术语。'
```

只有需要补充新事实时再加 `--research`。纯粹改语气、调整结构或缩短篇幅应优先复用现有证据。

```bash
python -m deep_research chat --run-dir runs/b
```

局部修改使用报告中的段落/章节标识，例如 `P2`。`revise` 默认在指定运行目录下创建 `revision-N` 子目录，不覆盖原稿；继续修订时把 `--run-dir` 指向上一份修订目录。`chat` 从指定目录开始，每次成功修改后自动接着最新结果继续；输入 `P2 修改要求`，补搜用 `/research P2 问题`，退出用 `/quit`。

普通修订不访问网络；`--research` 为这次修订额外分配最多 `--max-pages` 个新来源，旧证据保留。交互入口延续同一个任务的简报、证据和成品。

## 如何读实验

公开材料放在 [experiments](experiments/README.md)。本地运行目录 `runs/`、缓存 `.cache/` 和 `.env` 不进入版本库。运行目录包含 `report.md`、`state.json`、`metrics.json`、`events.jsonl` 等产物，网页正文保存在本地 `pages/`。B 的证据条目会检查引文能否在正文中定位；**匹配原文只证明引文存在，不证明它支持报告的解释**，关键结论仍需人工核查。

用允许清单导出已完成的运行：

```bash
python -m deep_research export --run-dir runs/b-fixture --output experiments/b-fixture
```

导出包括报告、统计、事件、证据及必要的修订信息，不复制原网页、完整 Prompt 或 `.env`。简报、答案和样稿可能随研究状态进入导出，因此公开前仍应检查自己的输入内容。程序也会检查疑似 Key，命中时拒绝导出。

至少区分首稿耗时与到可用稿的总耗时，同时看人工改动量、引用是否支持结论、中文是否自然。`metrics.json` 的主要口径如下：

| 字段 | 含义 |
| --- | --- |
| `wall_seconds` / `human_wait_seconds` / `automated_wall_seconds` | 本次运行墙钟时间、终端澄清等待、两者之差；不包括运行之外的人工阅读与改稿时间 |
| `model_calls` / `successful_model_calls` | 模型请求尝试数及成功数，重试也计入尝试数 |
| `critical_path_model_rounds` | 已记录依赖 DAG 中最长路径上的模型尝试数，包含重试；不是所有调用之和 |
| `dependency_path_seconds` | 已记录依赖路径的最长累计耗时，不包括未埋点调度和人工等待，不能替代墙钟时间 |
| `input_tokens` / `output_tokens` | 服务端返回的输入、输出用量汇总；输出已经包含推理 Token |
| `reasoning_tokens` / `visible_output_tokens` | 接口返回细分时才统计，否则为 `null`；不能再把推理 Token 加到输出总数上 |
| `calls_without_usage` / `token_totals_complete` | 缺少用量的调用数及总数是否完整；缺失意味着未知，不意味着免费 |
| `valid_evidence_count` / `rejected_quote_count` | B 中通过原文定位检查的证据条目数及被拒条目数；不是语义正确率 |

模型调用数不等于串行等待步数：多个并行调用增加请求总数，却未必同等增加等待时间。每次修订单独统计；比较完整交付成本时要合并首稿和后续修订，并补记人工阅读与反馈时间。

本原型没有验证商业产品的内部架构，也没有复现厂商训练过程。它提供一个可观察、可修改的工程样本，用来检验流程假说。

## 源码怎么读

| 文件 | 负责什么 |
|---|---|
| [engine.py](deep_research/engine.py) | A/B显式执行顺序、证据逐字校验、缺口补查与单段替换 |
| [prompts.py](deep_research/prompts.py) | 全部系统提示词；没有藏在框架里的角色模板 |
| [web.py](deep_research/web.py) | 免费搜索降级、正文提取、缓存与URL去重 |
| [llm.py](deep_research/llm.py) | DeepSeek请求、思考开关、有限重试、截断检测 |
| [metrics.py](deep_research/metrics.py) | 调用依赖图、墙钟、Token与缓存命中 |
| [cli.py](deep_research/cli.py) | 命令行、澄清退出、修订会话、公开结果导出 |

A的长文偏好参考了[GPT-Researcher报告模板](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/prompts.py#L293-L315)和[LangChain的报告提示](https://github.com/langchain-ai/open_deep_research/blob/1b7d2e80db9faa586165c60e09096dbbfd483a64/src/open_deep_research/prompts.py#L280-L300)，固定四节目录是本项目人为加入的误用条件。上游当前版具有各自的语言控制、并发和澄清机制，不能拿这个A替它们排名。

离线验证：`python -m pytest -q`。测试替换模型和网络，不消耗API额度。依赖版本快照在 `requirements-lock.txt`；需要复现实测环境时先 `pip install -r requirements-lock.txt`，再 `pip install -e . --no-deps`。
