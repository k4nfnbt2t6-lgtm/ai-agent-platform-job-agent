# 自动 LLM 分析与 Auto Apply：开发版说明

本阶段在已发布 v6 上增加可选自动化层，不替换五个 Skill、不改变已有 launchd 配置，不自动启用付费或真实投递。本次验收只使用人工材料和本机模拟平台。真实模型语义质量、真实平台投递能力需另行验收。

## 验收标准与结构

1. 公开搜索沿用原解析器、来源健康记录、城市分阶段搜索与安全网络限制。
2. 本地先过滤招聘身份、届别、城市、截止日期、硬性资格、已有投递状态；只有新岗位/JD实质变化进入模型分析。
3. LLM复用三个Skill、严格JSON Schema、原文证据与学习状态检查、原有动态Token/推理配置。不用低质量截断迁就预算。
4. SQLite先持久化完整计划额度、逐次请求意图与用量；未变化岗位不重复分析；未发送的预算不足岗位留待后续，费用未知的请求不自动重试。
5. 资格检查先于评分；职业目标不作能力证据；学历/专业/年限还经过独立本地检查。未知届别、开放状态、个人回答、到岗周期等转人工。
6. 材料由已有resume-tailor生成独立文件，原简历不覆盖。申请字段要有本人确认和原文来源；开放问题/同意条款须逐岗位明确回答。
7. 模拟提交有数量限制、唯一申请意图、材料摘要校验、紧急停止、回执核对和中断恢复。模拟成功不写真实 `applied`。
8. 旧每日local调度回归通过，不安装新系统任务、不推送GitHub。

```text
原有公开来源搜索（不调用模型）
  → 本地筛选 / 广州不足再上海、深圳
  → DiscoveryStore / JobLibrary 稳定岗位与JD版本
  → AutomationRuntime → 原JobRuntime / Agent Loop / 三个Skill
      → MeteredAnalyzer → 原LLMAnalyzer / Schema / 证据检查
  → 硬资格 + 技能匹配决策：skip / review / eligible
  → 原resume-tailor（当前自动材料仅local；LLM材料保留原独立确认入口）
  → 本地人工材料 / MockATS表单提交
  → SQLite意图、回执、摘要（与真实岗位状态分开）
```

模块在 `job_agent/automation/`。`policy.py` 管授权；`store.py` 管持久化额度/请求/申请；`analysis.py` 适配原LLM；`engine.py` 管自动分析与v5调度适配；`decisions.py` 管资格；`applications.py` 管材料与恢复；`adapters.py` 管平台接口；`workflow.py` 串联流程。旧代码只增加默认行为不变的注入/展示/过滤接口，无新依赖。

## 最简单的离线完整演示

在项目目录执行：

```bash
.venv/bin/python -B -m job_agent.automation demo --mode mock-llm \
  --state-dir .agent-platform/job-automation-demo
```

这会使用内置人工简历、人工广州实习JD、内存Responses替身和SQLite模拟招聘平台，执行三个Skill、评分、材料生成、表单填写与模拟回执。`paid_api_calls=0`；预算字段是模拟用量对应的假设金额，不是真实费用。

同一命令再运行：未变岗位新分析为0，已有申请显示 `already_recorded`，不重复生成投递意图。`--mode local` 则完全使用原规则模式。

模拟报告标记 `mode=mock_llm`、`mock_checks_complete`；不标记真实语义验收通过。模拟与真实LLM的JD缓存和完整报告隔离，不能把离线结果误复用于真实分析。

每轮另存私有Markdown与JSON报告，CLI的 `report_files` 给出相对于状态目录的位置。`status`可查看最近摘要、预算账本、未解决请求和模拟申请状态。

```bash
.venv/bin/python -B -m job_agent.automation status --state-dir .agent-platform/job-automation-demo
.venv/bin/python -B -m job_agent.automation stop --state-dir .agent-platform/job-automation-demo
.venv/bin/python -B -m job_agent.automation resume --confirm-resume --state-dir .agent-platform/job-automation-demo
.venv/bin/python -B -m job_agent.automation reconcile --attempt <申请记录ID> --state-dir .agent-platform/job-automation-demo
```

停止是持久化开关：阻止新的模型请求/模拟提交，不能撤销已经发出的请求。恢复需要显式确认。`reconcile`仅查询本地模拟收件回执；超时且无回执继续unknown，不重发。

## 搜索与人工材料

以下是以后自主运行的方式，本次没有联网搜索，也没有调用真实API：

```bash
.venv/bin/python -B -m job_agent.automation check --mode local \
  --resume /path/to/resume.pdf --allow-network --guangzhou-only \
  --search-limit 5 --analyze-limit 2 \
  --application-mode manual --state-dir .agent-platform/job-automation-local
```

未指定 `--guangzhou-only` 时，继续广州 >>> 上海 > 深圳。已投递及硬资格不符的岗位不占城市扩展阈值。排序复用原高质量上海/深圳越级规则。其他城市不进入核心列表。

`--application-mode off`（默认）只分析；`manual`生成本地定制简历/申请材料与原始链接，交由本人检查并提交；`mock`只交给本机模拟平台，绝不向链接发送申请。缺少姓名/邮箱等答案时材料标记待确认，不代填。

从岗位库准备材料：

```bash
.venv/bin/python -B -m job_agent.automation prepare --job 1 \
  --resume /path/to/resume.pdf --adapter manual \
  --state-dir .agent-platform/job-automation-local
```

表单个人字段可通过本机 `--facts .agent-platform/confirmed-facts.json` 提供，例如人工样例：

```json
{"name":{"value":"人工候选人","quote":"姓名：人工候选人","confirmed":true}}
```

该引用必须在本次简历中，值必须出现在引用中。开放问题/同意项除 `confirmed:true` 外还需 `source:"user_answer"` 与本岗位 `job_id`；不能从JD、模型建议或其他岗位复制授权。正式投递还需要站点对应的全部必填字段、人工复核和独立许可。

## 自动LLM的授权与预算（当前未启用）

真实调用入口为 `check --mode llm`，但默认预算和次数都是0，没有 `--consent` 即拒绝。密钥只读 `OPENAI_API_KEY`；地址/模型/质量参数复用现有 `OPENAI_BASE_URL`、`OPENAI_MODEL`、`JOB_AGENT_*`。

启用前须由本人确认：发送整份选定简历（可能含个人信息）、岗位JD、三个Skill的结构化上下文给指定中转API；确认模型、输入/输出单价、每日预算、每日请求数、每轮岗位数及有效期。授权JSON包含 `resume_hash/base_url/model/expires_at/limits_hash/input_price/output_price/confirmed_data_transfer/confirmed_paid_calls`，用 `LLMConsent.for_materials` 生成后由本人核对，保存在 `.agent-platform/`；更换简历、地址、模型、单价或限额会失效。不能由业务Agent自行生成真实授权。

完整三Skill通常需要3次请求，JD结构缓存命中时2次。先按原客户端的保守输入估算和任务输出上限预留完整额度，再逐次结算。预算不足不缩短上下文或降低输出质量；应调整经确认的预算。

客户端金额是配置价格估算，不是服务商账单硬上限。请求返回用量异常或超时：保留预留额度、标记unknown、暂停新的付费计划（跨日有效），人工核对账单后再决定如何恢复。没有自动HTTP重试。已知费用的Schema/证据失败可local降级，但必须标记质量降级、转人工，不冒充语义验收。

每日北京时间归零的是新一天的额度，不是未知请求状态。SQL `BEGIN IMMEDIATE` 控制跨连接额度；状态目录锁防并发分析；提交意图唯一键防同一岗位因JD更新再次投递。`auto_plans/auto_requests/auto_applications/auto_packets/auto_runs` 是新增表，原岗位表及历史不替换。

## 调度与异常恢复

`AutomationUpdates`复用v5 `DailyUpdates`、ScheduledCallbacks、BackgroundCallbacks、到期认领、状态锁、last-check/new-jobs和重启恢复。仅显式注入的分析worker能使用获准LLM；原类仍只允许local，原launchd未修改。开发入口是单次检查，不提供偷偷安装或启用长期任务的选项。

定时模拟使用可控时间，不真实等待一天。以后启用自动LLM须先单独验收、确认预算与资料外发，再决定是否部署新系统任务。当前自动申请编排通过单次 `workflow` 演示，未接入生产每日launchd。

程序退出前已落盘的申请意图不重发。模拟平台收件后超时可只查回执恢复；无回执、错误回执、异常退出保留unknown。模拟拒绝/网络失败计入当日申请尝试额度，禁止无限重试。日志只记录岗位ID、状态、计数、估算费用和错误类别；简历和表单答案仅在私有材料/岗位数据库保存。

## 申请方式研究与实际支持范围

| 方式 | 当前实现 | 合法使用前提与边界 |
|---|---|---|
| 本机MockATS | 完整表单/意图/回执/恢复/限额测试 | 无网络，无真实申请；只证明工程流程 |
| 人工交接 | 独立简历、申请材料、原始链接 | 本人核对后自行提交；程序不把材料生成记为已投递 |
| Greenhouse Job Board API | 仅接口设计与研究，未接入真实POST | 官方文档公开GET；申请POST需招聘方Job Board API Key，不代表任意求职者可直接调用。须平台/雇主许可 |
| Lever Postings API / hosted applyUrl | 仅接口设计与研究，未接入真实POST | 官方推荐托管申请页；自建申请POST需key，简历multipart、自定义问题与限流需单独处理。须平台/雇主许可 |
| 通用网页表单自动化 | 未实现 | 先逐站点确认条款与自动化许可、字段、登录及同意要求。遇验证码/访问限制转人工，不绕过 |

来源：[Greenhouse官方文档](https://docs.greenhouse.io/job-board.html)、[Lever官方Postings API](https://github.com/lever/postings-api)。这些是可研究的技术接口，不能据此声称已有投递权限。本轮没有安装浏览器Skill、插件或MCP，没有索取招聘方密钥。

LLM结构化输出沿用[OpenAI官方说明](https://developers.openai.com/api/docs/guides/structured-outputs)。本轮官方文档MCP连接失败后使用官方网页核对；文档查询不属于模型付费调用。

## 测试与尚未验收范围

```bash
env -u OPENAI_API_KEY -u OPENAI_BASE_URL .venv/bin/python -B -m unittest test_job_agent_automation -q
env -u OPENAI_API_KEY -u OPENAI_BASE_URL .venv/bin/python -B -m unittest discover -p 'test_job_agent*.py' -q
env -u OPENAI_API_KEY -u OPENAI_BASE_URL .venv/bin/python -B -m unittest -q
```

覆盖完整离线链路、额度/重启、重复岗位/JD变化、资格优先、真实状态保留、未知字段、材料防篡改、超时回执恢复、紧急停止、调度与隐私日志。真实LLM自动批次和真实招聘平台申请尚未验收，不能称为“已支持真实全自动投递”。详细结果见[本阶段工程验收报告](JOB_AUTOMATION_ACCEPTANCE.md)。
