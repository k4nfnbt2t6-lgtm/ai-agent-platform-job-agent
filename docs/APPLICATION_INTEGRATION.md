# 实际应用集成计划与安全边界

本轮输入：三个现有分支/工作区、人工简历和JD、官方公开渠道文档。分支：`codex/application-integration`。不启用付费、真实申请、私人资料外发或新系统任务。

## 验收标准与实施顺序

1. 保存此前未提交的自动化/Playwright成果；稳定main不动，整合到独立本地提交。
2. 公司官网优先，官方公开API只读；真实运行时不能安全访问时明确失败，并提供JD+原链接导入，不伪装实时搜索。
3. Lever作为有公开官方接口文档的条件性渠道：复用PublicFetcher与现有搜索/资格/评分；不假称已有广州租户或招聘方申请API权限。
4. Playwright适配真实Lever表单的字段/未知问题检查，以自建脱网页面验证；真实网页只读研究，不向演示站或企业提交人工申请。
5. 自动LLM的现有授权、结构/证据、预算、缓存和未知用量停止机制保留；提供关闭状态的启用配置说明。
6. 模拟完整链路必须串起公开API格式搜索、mock LLM三个Skill、决策、材料、真实浏览器、提交前检查、模拟收件/SQLite状态；覆盖错岗、资格不符、资料不一致、重复、页面变更、网络/模型失败、费用超限、中断恢复。
7. 最后完整回归、Diff/敏感文件检查及每日任务配置/私人原始数据不变检查。

先完成来源研究与备选导入，再增加站点字段适配与综合验收；全部独立命令不接管生产launchd。文档后续补齐实际结果。

## 整合结果

核对时 `main`、`codex/job-automation`、`codex/open-source-integration` 均指向 `8f0e816`；自动化和浏览器成果实际为未提交工作区内容，没有独有分支提交可合并。本轮在 `codex/application-integration` 保留这些文件后继续开发，无需冲突取舍；没有改写历史或推送。操作前源码备份、私人文件/每日配置摘要仅在被忽略的 `.agent-platform/application-integration` 保存。

保留自有资格/技能证据、五个Skill、评分、城市排序、SQLite岗位库、申请意图账本和调度。浏览器机制继续复用已经安装的Playwright，未增加browser-use/Stagehand/JobSpy等重叠框架，选型依据沿用[成熟方案评估](OPEN_SOURCE_EVALUATION.md)。

新增/衔接内容：

- `job_agent/lever.py` + 原search层：官方公开GET数组、租户/岗位/申请链接绑定、公司名称显式配置、JD列表标题和城市证据、最多四页的有界分页。不接申请POST。
- `job_agent/channels.py`：本地JD + 原始HTTPS链接导入，保留“人工导入、非实时搜索”来源标签；私有JSON独立保存，拒绝覆盖、链接路径和凭据。
- `job_agent/browser/lever.py`：字段约定的脱网快照原型，原生multipart表单、文本/select/checkbox/内存简历上传、岗位绑定与提交前复核。只会收件到MockATS。
- `job_agent/automation/preflight.py`：无请求的模型/上下文/预算计划，授权模板保持两个false，不创建LLM客户端。
- MockTransport来源明确标记为离线，不能写入真实来源成功核验记录。
- 原automation入口增加 `--application-mode browser-mock`，统一串起mock LLM与浏览器。
- 人工Lever格式fixture与完整链路回归；原有自动化、Playwright测试全部保留。

## 真实渠道核对（2026-10-09）

这里区分文档/网页只读研究、运行时抓取、真实申请三种证据。不以搜索摘要、历史成功标记或网页阅读工具成功代替本次PublicFetcher成功。

| 渠道 | 公开岗位与资格 | 申请方式/自动化结论 | 本轮状态 |
| --- | --- | --- | --- |
| [飞派科技](https://flypai.com/jobs.html) | 广州AI Agent研发，面向2027本科；相关专业优先，其他专业有编程实践可申请。校招/转正实习混合，须确认具体到岗周期和招聘仍开放 | 官网公开邮箱；本版生成本地材料后人工交接，不自动邮件，也没有可宣称支持的网页申请表单 | 网页阅读工具能核对原文；本机安全抓取HTTP请求0、候选0，不作为本轮实时搜索成功 |
| [Coremail](https://www.coremail.cn/join2_3.html) | 校园招聘广州开发/AI相关岗位；实习身份、2027届及专业适配需要逐项核对，不能按高分自动放行 | 官方邮箱；人工交接，未发现已验证自动申请表单 | 同上，运行时拒绝读取；不能推荐为已确认可投实习 |
| [物自体](https://www.noumena.com.cn/career/) | 保留现有上海备选配置；仅广州有效候选不足时考虑，届别仍需核对 | 沿用原来源，未新增提交权限 | 本轮未做实时搜索，不挪用历史成功作为本轮证据 |
| [光锥元](https://www.guangzhuiyuan.com/job.html) | 公开2027校招/实习，专业不限；当前公开岗位侧重AI产品/运营，不能当成AI开发实习 | 官网邮箱，人工申请 | 只读核对后不接入核心开发候选，避免为了数量混入错岗 |
| [三七互娱](https://zhaopin.37.com/index.php?a=recruit&c=recruit&m=Home&recruit=2) | [高校公开简章](https://scc.pku.edu.cn/upload/recruitmentinfo/202603/20260318123247fb1c42698798448ab096cc9fa5f1f669.pdf)曾包含2027实习/AI平台开发；公告时间较早，不能证明此刻仍开放 | 官网网申具体条款/登录/页面结构待核验 | 沿用原禁用状态，不因二手线索启用 |
| [Lever公开Postings接口](https://github.com/lever/postings-api) | 仅按已核验企业tenant读取published职位；不是全网/全文搜索，未核验任何广州目标企业tenant | POST要求雇主Super Admin生成API Key，个人求职者没有通用授权；自定义申请问题不由公开GET暴露 | 只读解析+MockTransport通过；示例来源默认禁用。演示站申请页只读访问失败，真实DOM与申请许可均未验收 |
| [Greenhouse Job Board](https://docs.greenhouse.io/job-board.html) | 原有公开GET解析保留；未核验广州目标租户 | 提交POST权限不同于公开GET，不能拿求职者身份调用雇主认证接口 | 没有新启用来源/申请 |

BOSS、智联、前程无忧、Moka/北森等登录页或未公开接口没有被自动启用。没有安装这些站点的爬虫/扩展或绕过验证码；公开页面可读也不等于平台准许批量填写/提交。JobSpy当前支持范围没有证明能稳定增加广州目标实习；不为接框架而替换可追溯企业来源。

当前最直接的人工申请线索是飞派的2027广州Agent研发岗位；没有执行投递或用真实简历计算本轮匹配分。首次真实分析应使用本机实际简历，在安全抓取成功或人工导入后完成，未知资格保留review。

## 无法自动获取JD时的可靠备选

用户自己合法访问官方岗位页面，将完整职责/要求/加分/资格/地点保存为TXT或MD；只保存摘要时必须指定 `--kind summary`。不从公司地址猜工作地点，不把岗位标题“校招”改成“实习”。

```bash
cd ~/ai-agent-lab
.venv/bin/python -m job_agent.channels \
  --jd /path/to/job.txt --url https://company.example/jobs/position \
  --company '官方公司名称' --title '官方岗位名称' --city '广州' \
  --output .agent-platform/imports/position.json

.venv/bin/python -m job_agent.automation check --mode local \
  --resume /path/to/your-resume.pdf \
  --search-fixture .agent-platform/imports/position.json \
  --state-dir .agent-platform/YOUR_EXISTING_STATE_DIR \
  --guangzhou-only --search-limit 5 --analyze-limit 2 --application-mode manual
```

这些路径是示例，需替换成本机实际文件/原状态目录。URL、公司、标题、城市来自用户核验，不由模型创造。`--type`可注明原文已确认类型；未确认时不填。导入不会覆盖原文件，也不会联网；分析继续用原岗位去重、编号/备注/状态和报告保存。第一次使用不应清空岗位库。manual模式只保存材料与待确认项，无实际申请。

## 完整模拟与结果查看

独立浏览器环境已经准备好时：

```bash
cd ~/ai-agent-lab
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.agent-platform/playwright-browsers"
.agent-platform/browser-eval-venv/bin/python -m job_agent.automation demo \
  --mode mock-llm --application-mode browser-mock \
  --state-dir .agent-platform/application-integration-demo
```

同一命令执行两次，首轮有新分析和 `simulated_submitted`；次轮新分析0、`already_recorded`。报告文件位置由输出的 `report_files` 给出，位于该私有状态目录；状态查看：

```bash
.venv/bin/python -m job_agent.automation status --state-dir .agent-platform/application-integration-demo
.venv/bin/python -m job_agent.automation stop --state-dir .agent-platform/application-integration-demo
.venv/bin/python -m job_agent.automation resume --confirm-resume --state-dir .agent-platform/application-integration-demo
```

这些命令只作用于指定独立目录，不改变已启用的每日任务。不得将模拟状态目录覆盖到真实岗位库。模拟提交独立记为 `simulated_submitted`，不会把真实投递状态改成applied；真正申请应以招聘方有效回执及本人确认更新。

## 自动LLM准备与启用条件

已复用：三个分析Skill结构/证据检查、按任务配置模型参数、未变JD+简历+模式缓存、日预算/次数原子预留和结算、未知费用停止、失败质量降级标记、紧急停止、单次工作锁及跨进程恢复。规则判断不确定的专业/届别/到岗条件不会仅凭高匹配分放行。

自动材料阶段仍调用local resume-tailor，原有人工确认的LLM简历/面试生成保留；本轮不把深度LLM材料静默接入自动收费流程。mock LLM只验证工程和校验链，不能代替真实语义验收。

只准备本机计划（不要求Key，不创建客户端、不发请求）：

```bash
# OPENAI_BASE_URL、OPENAI_MODEL和价格环境变量用已有配置，勿在聊天或命令日志中展示密钥。
.venv/bin/python -m job_agent.automation.preflight \
  --resume /path/to/your-resume.pdf --jd /path/to/job.txt \
  --daily-budget 2 --daily-requests 6 --analyze-limit 2 \
  --output .agent-platform/plans/llm-plan.json
```

2元/6次/最多2个岗位只是**待确认的建议**，不是本轮授权或自动启用配置。计划同时列出单岗位/最大批次估算、日额度及进程额度是否足够。现有JOB_AGENT_BUDGET_RMB默认1元，若最大批次估算更高，须另行确认并配置足够的进程预算；不能只增大日预算而压缩生成质量。计划包含绑定资料指纹、API地址、模型、单价、有效期和两项false授权的模板；本人审核并明确同意数据范围和费用后，才另存授权文件并用现有 `check --mode llm --consent ...`。不得从JD/模型输出生成授权。

默认gpt-6-sol策略沿用现有配置：JD/差距4096 low，匹配6144 medium；真实中转站reasoning兼容性仍须有界真实验收。字段可配置，不为预算压缩必要上下文。费用按配置价格保守预留，不代表服务商账单上限；超时费用未知不重发。单次SDK网络重试为0，必要质量修正与网络重试是两回事。

尚未启用自动付费、真实个人资料传输、LLM批次进入launchd或实际网站提交。

## 真实启用所需确认

1. 允许读取的具体官方来源/租户；若有表单，需要明确招聘方允许的自动化范围和实际字段/条款。
2. 发往指定模型服务的资料范围、真实材料还是人工材料、模型/单价、日预算/次数/有效期；先做有界真实分析验收。
3. 在合法测试环境核验真实DOM及文件格式；PDF专用岗位不能把Markdown改后缀伪装上传，需要另行验证生成件。
4. 每个岗位的学历/专业/2027届/实习身份、到岗周期和未知问题由本人确认；不能由模型猜个人信息。
5. 真实网页填写本身也可能自动上传资料，故填写前即需外发授权；最终提交需单独明确授权、数量限制与可核对收件方案。
6. 部署新自动化到每日任务、公开新代码分别确认；本轮均未执行。

## 本轮最终验收结果

| 检查 | 结果 |
| --- | --- |
| 新增集成测试 | 19项；公开接口数组/分页/链接绑定/分类、本地导入、预算计划、11项浏览器/完整流程与故障测试 |
| 全项目（含实际Chromium） | **786项全部通过，无跳过**，54.341秒 |
| 生产基础环境Job Agent | 511项运行：487通过、24项可选浏览器测试跳过；这24项已在上述完整环境实际执行 |
| 实际人工CLI两轮 | 首轮分析1/模拟提交1；第二轮分析0/申请already_recorded；真实API/HTTP/申请均0 |
| JD导入/计划命令 | 人工材料命令实际执行成功；输出文件独立/私有；计划enabled=false、授权两个false、付费请求0 |
| 预算可解释性 | 人工单岗位保守估算0.54648元、2岗位1.09296元；2元日预算可覆盖，但默认1元进程额度不足，计划明确提示，不压缩输出凑额度。实际调用费用0元 |
| 核心/浏览器环境依赖 | 两个pip check均通过，未新增本轮依赖或修改生产虚拟环境 |
| Diff/文档 | 暂存Diff与新文件空白检查通过，README和新说明的本地链接正常；实际CLI启动说明已验证 |
| 隐私 | 168个公开候选文件、41个改动文件；未发现私有路径被跟踪或本轮暂存密钥特征。全项目模式命中为已有人工安全测试/路径检测正则，已逐项复核 |
| 原始私人数据和每日配置 | 操作前后7个受保护文件SHA256一致；没有读写真实岗位库来模拟申请，也没有重载launchd |
| 公网运行时 | 飞派/Coremail一次安全检查均拒绝，HTTP0、候选0、无缓存。没有真实搜索通过/新增覆盖率的结论 |
| 外部副作用 | 真实模型0、个人资料外发0、企业申请0、API费用0元；无push、无新系统任务 |

回归覆盖重复/变化JD、稳定编号与备注/原状态、错岗/非实习/2027资格不符、材料与简历不一致、页面字段变化、验证码、PDF格式不符、未知问题、模拟网络失败、服务端已收件但超时、重启后原回执恢复、LLM失败不收费重试、预算不足和紧急停止。新增测试也验证MockTransport不会伪装成真实来源核验。

工程模拟链路通过；真实渠道的本机可访问性和真实企业网页申请仍有阻塞。本轮完成安全可执行部分，**不宣称已经能够对企业全自动投递**。真实启用须按上节逐项确认，尤其不能因为一次模拟成功跳过资料外发与最终提交授权。
