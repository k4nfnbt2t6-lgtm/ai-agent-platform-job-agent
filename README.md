# AI Agent Platform + Job Agent v6

一个复用统一 Agent 底座的技术实习求职项目。Platform 提供工具调用、文件与 Terminal、会话、记忆、权限、任务管理和子 Agent；Job Agent 在同一底座上完成岗位搜索、分析、评分、岗位库、每日检查、简历优化与面试准备。

默认使用 **local**：Platform 使用本地模拟模型演示工具循环，Job Agent 使用本地规则分析。离线示例无需 API Key，不产生模型 API 费用；真实公开岗位搜索仍需要网络。模拟和规则测试不能代替真实模型质量验收。Job Agent v6 已完成真实人工材料验收与人工质量审查；原始请求、响应和个人求职资料仅保留本地。

## 项目解决什么问题

把分散的 Agent 实验整合成可运行的统一底座，再以求职工作流验证工具编排、数据持久化和真实性约束：

```text
公开岗位来源 → 提取JD → 本地过滤/去重 → 三个分析Skill
                                         ↓
                                证据匹配 + 分层评分
                                         ↓
                               SQLite岗位库与手动状态
                                         ↓
                         岗位定制简历 + 十道面试题/准备清单
```

重点工程实践：**有证据的技能匹配、组合要求评分、稳定岗位编号、幂等更新、受控LLM调用与持久调度**。Python基础不会升级成熟练，职业目标不会计作能力，项目描述不能借用其他项目的证据。

| 阶段 | 已实现的能力 |
| --- | --- |
| Platform v1.0 | 统一入口、Agent Loop、Tool Calling、会话/记忆、权限与Diff、受限子Agent、后台/定时任务 |
| Job Agent v1 | analyze-jd / match-resume / skill-gap-analysis、岗位去重、本地简历读取 |
| v2 | local / llm双模式、结构化输出、证据校验与分层评分 |
| v3 | 公开岗位搜索、岗位卡片/JSON-LD解析、城市分阶段筛选与可解释排序 |
| v4 | SQLite岗位库、稳定编号、状态/备注、求职漏斗 |
| v5 | 每日检查、只分析新增/实质变化、更新摘要与调度恢复 |
| v6 | resume-tailor / interview-prep、独立材料输出、项目证据与学习状态一致性 |

这是AI辅助开发项目。本人可展示的理解与验证、仍需学习的实现，以及没有实现的能力，见 [作品集与贡献边界](docs/PORTFOLIO.md)。

## 环境与安装

已验证 Python 3.14。依赖固定在 [requirements.txt](requirements.txt)。下载源码后，进入包含 `main.py` 的项目根目录执行：

```zsh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

安装完成后运行 `.venv/bin/python -m pip check`；应显示依赖一致。无需安装Skill、浏览器插件或MCP即可完成下面的离线演示。已有虚拟环境可以直接复用，安装前核对依赖文件。PDF 使用 pypdf 本地解析，DOCX/TXT/MD 同样不需要模型 API。

## 最简单的启动方式

```zsh
.venv/bin/python -B main.py
```

输入 `请用工具计算 37 + 58`，应得到 95；输入 `/help` 查看示例，`/quit` 退出。完整安全演示：

```zsh
.venv/bin/python -B main.py --self-check
```

Platform 的示例文件与状态在 `.agent-platform/` 下，不操作任意源码。文件修改展示 Diff 并等待用户确认。更多说明见 [演示流程](DEMO.md) 和 [Platform 设计记录](PLATFORM.md)。

## Job Agent：先用人工材料

```zsh
.venv/bin/python -B main.py --agent job --mode local \
  --resume job_agent/examples/resume.example.md \
  --jd job_agent/examples/jd.example.md \
  --state-dir .agent-platform/job-demo

.venv/bin/python -B main.py --agent job --mode local \
  --resume job_agent/examples/resume.example.md \
  --state-dir .agent-platform/job-demo
```

第二条命令进入交互界面，可以输入：

```text
/jobs
/job 1
/status 1 shortlisted
/note 1 先准备项目答辩
/interview-prep 1
/tailor 1
/last-check
/new-jobs
/quit
```

使用自己的简历时，将 `--resume` 替换为本机真实路径；支持 PDF、DOCX、TXT、MD，也可手动粘贴个人背景。生成的简历另存为新 Markdown，不覆盖原件。岗位状态由用户手动维护，不自动投递。

五个业务 Skill：

| Skill | 功能 |
| --- | --- |
| analyze-jd | 区分职责、必须项、加分项和资格要求，保留组合条件 |
| match-resume | 基于原文证据匹配，区分完整、部分与未匹配 |
| skill-gap-analysis | 给出缺口、优先补强项和验证方法 |
| resume-tailor | 针对 JD 整理真实经历，分别保存完整简历与修改说明 |
| interview-prep | 生成十道岗位/项目问题及准备清单，保留学习状态与证据 |

城市偏好为广州 >>> 上海 > 深圳：广州有效候选不足才扩展上海，再不足扩展深圳；质量与匹配度明显更优时允许越级，并解释原因。

公开岗位搜索需要明确启用可信来源与网络访问；不登录、不绕验证码。每日检查固定使用 local，不自动调用收费模型。`--watch` 需要进程持续运行；macOS 可准备 [launchd 每日部署](docs/MACOS_DAILY.md)，关闭 Terminal 后仍由系统触发，实际安装启用前需要本人确认。

详细命令、搜索与调度设置见 [Job Agent 说明](job_agent/README.md)。

## 离线搜索与每日摘要演示

以下只使用人工岗位，不访问招聘站，也不调用模型API：

```sh
.venv/bin/python -B main.py --agent job --search-demo --mode local \
  --state-dir .agent-platform/job-search-demo

.venv/bin/python -B main.py --agent job --check-now --search-demo --mode local \
  --state-dir .agent-platform/job-daily-demo
```

各自重复运行同一命令，未变化的岗位应复用分析、保持编号；每日摘要的新分析数应为0。`/last-check`、`/new-jobs`必须使用对应的同一状态目录查看。真实搜索需要另行启用公开来源和 `--allow-network`，命令与来源边界见 [来源核验](job_agent/SOURCE_VERIFICATION.md) 与 [搜索说明](job_agent/README.md#已核验公开来源工程化第二阶段)。

## 架构与工程能力

架构详见 [统一底座与业务流程](docs/ARCHITECTURE.md)。`main.py → PlatformRuntime / SDK Runner` 提供 Agent Loop、Tool Calling、SQLite Session、Memory、按需Skills、权限、后台任务和受限子Agent；`JobRuntime` 继承底座并配置求职工具，没有复制另一套框架。

搜索层先本地过滤和去重，再运行三个分析Skill，按1/0.5/0对完全/部分/未匹配评分（必须项权重2、加分项1；任选组合只算一次）。稳定岗位编号与JD版本分离，重新搜索及JD更新保留状态、备注与历史。两个准备Skill分别生成独立简历、修改说明与十道岗位/项目面试题，约束原文证据和学习状态。

`local` 适合离线演示、规则过滤和固定免费定时检查；`llm` 提供需授权的语义分析与生成。模拟模型不代表自主规划，匹配分不是录用概率，校园招聘也不自动等于2027届实习。公开页面存在不证明雇主仍有名额；未知资格需人工核对。GUI仅为本机平台演示，Job Agent没有Web UI。

## 每日运行与作品集

macOS 部署适配使用现有v5持久计划与跨进程锁，默认北京时间09:00；休眠后补查一次，失败记录后停止，不持续重试，也不启用付费分析。配置保存在被忽略的本地目录，准备配置不等于安装系统任务。操作、恢复边界、最近检查和新增岗位查询见 [每日运行说明](docs/MACOS_DAILY.md)。

向招聘方展示时使用人工材料，按 [作品集演示与真实开发边界](docs/PORTFOLIO.md) 展示底座、求职闭环、岗位状态、准备材料和异常回归。项目由AI辅助开发，个人理解、贡献和熟练度需要本人如实说明。

可选浏览器层采用 **Playwright Python**，用于隔离HTML快照渲染和真实Chromium模拟申请表单；复用原Skill、评分、SQLite、去重与回执恢复，没有真实网站自动投递出口，不改变每日任务。安装、离线演示和测试见 [浏览器集成](docs/BROWSER_INTEGRATION.md)；browser-use、Stagehand、JobSpy及中国招聘方案的取舍见 [成熟方案评估](docs/OPEN_SOURCE_EVALUATION.md)。

## LLM 配置与授权

沿用环境变量 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`，配置项见 [.env.example](.env.example)。示例 Key 为空；程序不会自动加载 `.env`。不要把真实 Key 写进源码、命令历史或聊天。

`--mode llm` 在发送前展示材料范围、目标 API、模型、次数与预计费用，等待当前用户确认。连接配置或历史验收不代表授权新的付费请求或真实简历上传。保留结构校验、原文证据核对、有界预算与失败停止；不自动重试可能收费的请求。定时检查仍为 local。

## 测试

新增可选的自动分析/模拟投递开发层：

```bash
.venv/bin/python -B -m job_agent.automation demo --mode mock-llm
```

使用人工材料、本地模型替身及模拟招聘平台；不联网、不付费、不改变真实投递状态或已启用的每日任务。支持持久化每日额度、资格优先决策、独立材料、申请去重/回执恢复与紧急停止。真实申请适配器未启用，自动付费需独立确认；详见[自动化范围、使用与授权](docs/JOB_AUTOMATION.md)。稳定v6的五个Skill及原入口保持可用，开发版尚未发布。

Job Agent 全部测试：

```zsh
env -u OPENAI_API_KEY -u OPENAI_BASE_URL .venv/bin/python -B -m unittest discover -p 'test_job_agent*.py' -q
```

全项目（包含 Platform、旧教学模块、验收脚本的 mock）：

```zsh
env -u OPENAI_API_KEY -u OPENAI_BASE_URL .venv/bin/python -B -m unittest -q
.venv/bin/python -m pip check
```

公开回归使用人工合成材料及 mock HTTP；真实 API 响应不纳入 Git。本机保留响应时额外离线重放，公开副本会明确跳过仅依赖私人档案的三项检查，不发起 API 请求，也不降低其余验证标准。

## 项目结构

```text
main.py                  统一入口
agent_platform/          统一运行底座及终端/本机 GUI
job_agent/               求职业务、搜索、评分、岗位库、调度和准备材料
job_agent/skills/        五个业务 Skill
job_agent/examples/      人工样例与默认禁用的来源模板
lab_skills/              Platform 示例 Skill
test_*.py                离线测试
tests/fixtures/          合成数据与公开网页解析样例
docs/                    架构、macOS部署、作品集及最终审查说明
*_agent.py / *_tools.py  保留原教学入口与底层能力
.agent-platform/         私人状态、数据库及真实验收记录（不纳入 Git）
.local-history/          初始化前文档备份（不纳入 Git）
```

## 版本管理与安全

稳定版本已发布；新增自动化能力仍在独立本地开发分支，未经授权不上传。可纳入 Git 的内容是源码、五个 Skill、人工样例、离线测试、依赖声明和经过清理的说明文档。

真实简历、个人信息、密钥、数据库、状态、运行日志、真实 API 请求/响应、个人授权指令与历史运行笔记仅留本地。`.gitignore` 是防误提交措施，不能加密文件，也不能阻止 `git add -f`；每次提交/上传前仍需审查暂存区。详见 [安全说明](SECURITY.md)。未经明确授权不推送、不发布、不上传材料。

公开副本的离线测试和演示已单独验收。2026-10-09本机全量测试684项通过；公开副本681项通过、3项私人响应重放明确跳过。真实来源曾完成成功搜索/去重验收，后续部署检查又遇到安全地址校验拒绝访问；联网结果随本机环境与站点变化，不能保证每次成功。macOS任务已在所有者本机经授权安装，真实到点/唤醒运行仍待观察；克隆仓库不会安装系统任务。最新工程审查和限制见 [最终工程审查](docs/ENGINEERING_REVIEW.md)。按所有者决定，当前暂不添加开源许可证；公开可见不等于授予任意复用许可。

## 公开岗位来源

已核验的官方来源配置、真实可用性及未启用原因见 [岗位来源核验](job_agent/SOURCE_VERIFICATION.md)。广州优先，不足才上海、再深圳；搜索保持公开只读，不向招聘来源发送简历。只搜索的完整命令见 [Job Agent 说明](job_agent/README.md#已核验公开来源工程化第二阶段)。

发布前身份处理、仓库名称/简介和确认清单见 [GitHub发布准备](docs/GITHUB_RELEASE.md)。
## 实际应用集成（独立开发分支）

自动LLM、申请账本和可选Playwright成果已整合到 `codex/application-integration`，未推送或替换GitHub稳定版。**真实自动投递未启用**。飞派/Coremail官方邮件入口保留人工交接；新增Lever官方公开GET格式适配与安全JD导入，未宣称新增已验证的广州接口租户。

从项目目录运行人工材料完整模拟（先按[浏览器说明](docs/BROWSER_INTEGRATION.md)准备独立环境并设置浏览器路径）：

```bash
.agent-platform/browser-eval-venv/bin/python -m job_agent.automation demo --mode mock-llm --application-mode browser-mock --state-dir .agent-platform/application-integration-demo
```

搜索/API格式替身、三个分析Skill、资格决策、独立材料、真实隔离Chromium填写/文件上传、模拟收件与SQLite去重均可离线验证。详细渠道结论、JD导入备选、关闭状态的LLM预算计划及真实启用要求见[实际应用集成报告](docs/APPLICATION_INTEGRATION.md)。现有每日任务仍保持local。
