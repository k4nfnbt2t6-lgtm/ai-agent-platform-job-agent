# Job Agent v6

## v6：针对岗位整理简历与准备面试

新增 `resume-tailor`、`interview-prep` 两个业务 Skill，复用现有 Runtime、Skill 加载器、岗位库、三个分析函数及评分规则。原有 local / llm、搜索、状态管理和每日检查保持原流程。本版无新增依赖，使用现有 Python 虚拟环境。

**两个 Skill 支持 local / llm。** 默认 local 不联网；llm 逐次展示完整待发材料、接收 API、模型、预计费用与上限，只有本次明确确认才请求。不自动投递、不登录、不发邮件、不改变岗位状态和备注。本轮仅用人工资料和 mock 验证，未做付费调用。

先用人工资料离线体验。在项目目录运行第一条命令，建立测试岗位 #1；再运行第二条进入同一岗位库：

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B main.py --agent job --mode local \
  --resume job_agent/examples/resume.example.md --jd job_agent/examples/jd.example.md \
  --state-dir .agent-platform/job-v6-offline-check
.venv/bin/python -B main.py --agent job --mode local \
  --resume job_agent/examples/resume.example.md \
  --state-dir .agent-platform/job-v6-offline-check
```

在交互界面输入：

```text
/jobs
/job 1
/tailor 1
/interview-prep 1
/quit
```

用已保存真实岗位和真实本地简历时，继续使用原状态目录（无需重新搜索）：

```zsh
.venv/bin/python -B main.py --agent job --mode local \
  --resume "/absolute/path/to/resume.pdf" \
  --state-dir .agent-platform/job-v3-third-live-check
```

也可通过 `/resume 路径` 或 `/paste-resume` 替换当前简历。没有简历、岗位编号不存在、缺少 JD 或材料包含疑似凭据时明确报错。每次针对当前简历重新计算匹配，不把旧简历结论直接拿来使用。

`/tailor 编号`：读取岗位库 JD → 用三个现有本地分析函数核对当前简历 → 识别教育、技能、项目等事实 → 根据 JD 重排技能、项目和项目要点 → 生成完整中文技术实习简历 → 将修改说明另存。保留“基础、学习中”和 AI 辅助开发等限定；职业目标不算技能，项目名不证明未描述的技术，API 基础不当作 LLM API 实战。缺失能力只列为准备建议，不加入简历。

**正式简历与说明文件分离。** `tailored-resume.md` 包含已提供的个人信息、教育、技术技能、项目和其他真实经历，不附整份原文或分析提示；`report.md` 保存原文、修改后内容、岗位依据、排序理由和核对事项。自动清理项目符号及 PDF 项目断行，保留项目版本、测试数字、本人贡献及熟练程度。学习章节与其下的技能整体移动，不把 MCP 等关键词移出学习语境。仅输出 Markdown，不生成 PDF 排版，也不自由编造个人事实；投递前由本人复核。

`/interview-prep 编号`：固定生成 **10 道独立问题**，包含 Python、Agent、Tool Calling、LLM API、错误处理及 JD 相关技术；简历中有具体实现或测试描述的 AI Agent Platform、Job Agent、Jieqi Agent 各生成一题，并附两个具体追问。答案简洁具体，区分已有文本证据、学习中、未找到证据，每题附学习/复盘建议。职责用于选题，不重复粘贴；原文依据保留在 JSON 审计信息中。任选组合仍按一个要求评分和制定差距清单，面试扩展题不变成新的必须技能。参考答案是学习资料，不是个人经历声明；未知技术的专门答案明确提示核对。

面试材料区分 JD 通用技术题、项目经历追问和学习／待确认题。通用技术题可不引用简历；引用时须属于当前主题。项目追问必须引用正确项目的具体实现或测试，仅项目名称不够。技术主题可以关联项目中的实际实现（如工具校验和异常测试），不要求原文逐字写出抽象主题名。SQLite 存储经历不自动证明 SQL 熟练度；AI 项目名称或局部实现不证明完整 AI 应用开发能力。此限制用于面试准备状态，不改现有岗位评分。首次真实响应的逐题定位保存在本机 `JOB_V6_EVIDENCE_DIAGNOSIS.md`（不纳入 Git）。

准备清单与题目复用同一准备证据判断，区分已有项目或技能文本证据、学习中、待确认和未找到证据；明确未掌握的情况另保留差距标记。Python 基础保留学习状态，SQLite 经历不升级为 SQL 熟练；JSON 与 Markdown 清单都保留原文依据。组合要求仍只占一项，按准备证据选择可选分支。SQL 未证实的实现只列核对或练习建议；LLM API 使用独立的模拟 HTTP 练习，覆盖请求/响应、401、429、超时、错误分类、重试边界以及 Token/费用，不以 Tool Calling 练习替代，也不触发真实请求。历史验收文件保持原样，新生成报告采用修复后的清单。

### 两个 Skill 的 LLM 语义生成

先在 local 模式用人工样例建立测试岗位，再选同一状态目录启动 llm（真实调用须先获确认）：

```zsh
.venv/bin/python -B main.py --agent job --mode local \
  --resume job_agent/examples/preparation-resume.example.md \
  --jd job_agent/examples/preparation-jd.example.md \
  --state-dir .agent-platform/job-v6-semantic-check

.venv/bin/python -B main.py --agent job --mode llm --model gpt-6-sol \
  --resume job_agent/examples/preparation-resume.example.md \
  --state-dir .agent-platform/job-v6-semantic-check
```

进入后用 `/jobs` 确认编号，再运行 `/tailor 1` 或 `/interview-prep 1`。这两个命令随启动的 `--mode local|llm` 工作；求职准备默认最多两稿，仅质量检查发现问题时生成修正稿；确认屏幕会预估完整流程，实际无需修正时只请求一次。只有输入“确认 LLM 分析”才发送本次展示的具体材料；下一条命令、另一份简历或另一份 JD 都需要新确认。拒绝确认不请求、不生成新报告。管道输入不能开启收费模式。

环境变量沿用 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`；`--model` 可覆盖模型。密钥只在本机设置，不写进源码。成本沿用 v2：默认配置输入 $0.30/百万、输出 $1.50/百万 token，暂按 $1=10元；这些是已配置的中转价格，不是官方价格。每次按**实际待发 payload、专用 prompt 和 schema**估算，保守输入起点为30000 token，并非输入上限。生产输出按任务区分：JD提取/差距建议4096、匹配/简历6144、面试8192；准备材料超过24000字节时按任务扩大，最高16384。可显式覆盖，不能因预算不足自动压缩重要证据或降低参数。

默认推理强度：JD提取/差距为 low，匹配/简历/面试为 medium；保留 gpt-6-sol。非默认模型不擅自发送其可能不支持的 reasoning 参数，可由用户明确配置。中转站对新推理参数的实际兼容性尚待真实验收，不自动降为 none 重发。

默认每进程6次、1元，HTTP120秒、SDK `max_retries=0`。支持环境变量 `JOB_AGENT_MAX_OUTPUT_TOKENS`、`JOB_AGENT_REASONING_EFFORT`、`JOB_AGENT_MAX_REQUESTS`、`JOB_AGENT_BUDGET_RMB`、`JOB_AGENT_PREPARATION_PASSES`（1–3稿，默认2）、`JOB_AGENT_TIMEOUT_SECONDS`。配置在进程启动时读取，改变次数、预算或材料仍须重新确认；配置不授权付费。预算不足先停止，不能自动改用local迁就预算。示例见根目录 `.env.example`。

准备 Skill 先做结构和真实性检查，再检查可解释的质量信号（解释深度、例子/验证说明，简历是否仅改标点）。只有通过证据检查且有质量问题的草稿才可按已确认上限修正，修正只带原确认材料、已校验草稿和反馈。完整最大次数、草稿上下文及输出成本在确认前预留；无需修正不额外调用。JSON中的 `quality_review` 保留逐稿信号，未解决的问题明确待复核。这不是语义质量保证，仍须人工检查。

API账单可能与估算不同；接口、结构或证据失败停止后续收费请求。`--fallback local` 会标明“质量降级”，JSON保存 `quality_degraded`、`quality_warning` 和 `semantic_complete=false`，不能当成LLM验收成功；`--fallback none` 停止且不生成文件。失败无可核对用量时费用为未知。定时搜索始终local，不受这些参数影响。

发送范围：当前岗位的脱敏 JD、当前简历的项目/技能事实原文与章节边界；面试另含十个选题和本地准备状态。姓名/联系方式/教育/工作经历段整体留在本地，常见邮箱、手机号、链接和本地路径不发送；项目段仍可能有间接标识，确认屏幕会完整展示 payload 供本人检查。不发送 PDF/DOCX 原文件、源码、其他岗位、历史会话或简历文件路径。API使用 `store=False`，这不替代第三方服务的数据政策。

复用现有 `LLMAnalyzer.ask`、Responses、模型配置、JSON schema 和计费机制，增加两个专用 schema 与专用 prompt。结构化输出按 [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 的方式实现；结构正确仍需要本地核对事实。

- `resume-tailor`：本地拆分事实并保护项目身份、基础/学习/否定等弱证据；模型按 JD 优先改写最多8条相关可编辑事实，其余由本地保留；每条必须返回一致的原文、事实ID、JD证据ID和理由。核对新增技术/英文名称/数字、学历经历、贡献成果及原有限定；不跨项目借证。个人信息、学历、工作经历和弱证据由本地保留，最后组装完整简历，另存修改说明。缺失技术不添加到简历。
- `interview-prep`：从当前 JD 和真实项目建立十个选题；模型生成针对性问题、教学参考答案、知识点、学习建议与项目深入追问。校验主题覆盖、问题/答案重复、原文ID、项目引用和个人能力声明；状态由宿主保留，Markdown基础/学习限定也不能被升级。未知实现只作为建议方案，不能写成本人已做过。

证据检查是保守的程序防线，不能证明每一句自由语言完全等价；未识别的语义新增或错误技术解释仍需本人核对。检出问题时拒绝整个模型结果，不把未经校验的改写混进正式材料；质量反馈不替代证据检查，不以额外模型自审代替原文验证；可在明确授权内修正已验证草稿。

历史首次双Skill人工资料验收记录已保留；当时两次/0.35元、4096/none配置只适用于该次范围，不作为现行业务默认参数。现在恢复验收仅运行 interview-prep，见文末“最终 interview-prep 单次真实验收”。生产两项Skill各自按当前完整计划重新预估与确认，不沿用历史额度。真实个人简历需要另一次数据范围与费用确认，不能沿用人工资料授权。

结果存放在当前状态目录的 `preparations/job-编号-随机标识/` 下，每次新建目录：

- `report.json`：带岗位编号、JD/简历指纹、匹配/差距与生成结果的结构化报告。
- `report.md`：可阅读的对比或面试准备报告，包含岗位依据、准备理由和资格核对。
- `tailored-resume.md`：仅 `/tailor` 生成的新简历草稿。

终端显示所有输出文件的绝对路径。文件只在本地创建，目录权限 700、文件权限 600，拒绝经过符号链接；固定输出名和独占创建，绝不覆盖原简历或上一次报告。结果可能包含个人资料，不要未经检查上传。没有新增岗位数据库表，也不修改投递状态；`jobs.sqlite` 继续提供岗位身份和 JD。

v6 回归与全项目测试（均不需要付费 API）：

```zsh
.venv/bin/python -B -m unittest -q test_job_agent test_job_agent_llm test_job_agent_search test_job_agent_v3_regressions test_job_agent_library test_job_agent_updates test_job_agent_preparation test_job_agent_preparation_llm test_job_agent_preparation_evidence test_job_agent_preparation_checklist
.venv/bin/python -B -m unittest -q
```

这是现有 Platform 的业务配置，保留三个 Skill、local / llm 分析、v3 公开岗位搜索和评分；v4 在原 SQLite 内增加岗位库与手动投递状态管理。不自动投递、不自动登录、不创建 Web UI。支持本地 PDF、DOCX、TXT、MD 简历；PDF 使用 `pypdf`，DOCX 使用 Python 标准库。v4 没有新增依赖或 Skill。

## v5：每天检查新岗位

v5 复用已有定时/后台管理器，增加每日检查与摘要，**自动分析固定为 local，模型费用 0 元**。设计与故障策略见 [SCHEDULE_DESIGN.md](SCHEDULE_DESIGN.md)。本轮只完成离线验收，没有开启真实长期任务。

先用人工岗位体验，完全不联网：

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B main.py --agent job --check-now --search-demo --mode local --state-dir .agent-platform/job-v5-offline-check
```

重复运行同一命令，未变化的已分析岗位新分析次数应为 0。离线人工岗位的每日计划示例：

```zsh
.venv/bin/python -B main.py --agent job --watch --daily-at 09:00 --search-demo --mode local --state-dir .agent-platform/job-v5-offline-check
```

在交互界面输入：

```text
/check-now
/last-check
/new-jobs
/jobs
/quit
```

`/check-now` 立即提交后台检查，不改变每日执行时间；正在检查时拒绝重复启动。完成后自动显示本次摘要；`/last-check` 查看最近检查的成功/部分失败/失败状态、摘要、最近一次发现新岗位时间及下次计划；`/new-jobs` 查看北京时间今日首次发现的岗位，重复检查不会重复计数。后两条命令在普通岗位管理入口也能用，无需加载简历。`/check-now` 需要在配置好来源和简历的 `--watch` 入口使用，或改用一次性 `--check-now` 参数。

确认真实公开来源后，用自己的文件启动（**下面是使用模板，本轮没有执行**）：

```zsh
.venv/bin/python -B main.py --agent job --watch --daily-at 09:00 --mode local \
  --resume "/绝对路径/我的简历.pdf" \
  --sources "/绝对路径/已核验公开来源.json" --allow-network \
  --state-dir .agent-platform/job-v3-third-live-check \
  --search-limit 8 --analyze-limit 4
```

每日时间按**北京时间**，默认沿用广州 >>> 上海 > 深圳；广州不足才扩展上海，再不足扩展深圳，原有高质量岗位越级条件不变。来源格式沿用 v3，模板默认禁用，不会自行启用来源。无需新增 Key 或安装工具。

下次在同一个状态目录重启 `--watch`，保留简历、来源和 `--allow-network` 参数，可以省略 `--daily-at` 恢复原时间；改变 `--daily-at` 才会重算计划。**进程保持运行才会检查；退出或关机后暂停，重启到期时最多补查一次。** 没有安装系统开机服务。输入 `/quit` 或 Ctrl+C 会取消当前任务，保存计划和已发现岗位，后续可恢复。

不想保持交互进程时，手动立即检查一次：把真实模板中的 `--watch --daily-at 09:00` 换成 `--check-now`。此操作不会改掉已保存的每日计划。

摘要区分本次新增、JD 更新、新分析、未变化跳过与待分析，并列出公司、岗位、城市、综合匹配度、推荐等级和原始链接。达到分析上限的岗位保留待分析，下一次补齐；没有技术要求但已分析的岗位显示“不适用”，不反复分析。投递状态和备注始终保留。

简历文件变化本身不会使定时检查重新分析全部旧 JD；需要重评某个旧岗位时用原 `/jd`、`/analyze` 流程。每次检查读取明确指定的文件，`/resume` 只替换当前手动会话背景，不改变每日任务的文件路径。

数据仍在同一个 `jobs.sqlite`：新增计划、检查历史与更新条目三张表。不保存简历全文、不自动调用 LLM、不自动投递、不发邮件。全部来源读取/解析失败会明确记录失败，不冒充“成功零新增”；不在失败后连续重试。

v5 回归测试与全项目检查：

```zsh
.venv/bin/python -B -m unittest -q test_job_agent test_job_agent_llm test_job_agent_search test_job_agent_v3_regressions test_job_agent_library test_job_agent_updates
.venv/bin/python -B -m unittest -q
```

## v4：管理真实求职进度

无需简历也能启动岗位管理，默认 local、无模型费用：

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B main.py --agent job --mode local
```

默认岗位库在 `.agent-platform/job-agent/jobs.sqlite`。**已有岗位在哪个状态目录，就继续使用哪个目录**；换目录是另一份独立岗位库。管理第三次广州-only验收保存的真实岗位：

```zsh
.venv/bin/python -B main.py --agent job --mode local --state-dir .agent-platform/job-v3-third-live-check
```

启动后直接输入命令，不必先粘贴简历：

| 命令 | 用途 |
| --- | --- |
| `/jobs` | 所有已发现岗位，显示稳定数字编号、公司/岗位/城市、状态、匹配度、推荐等级 |
| `/jobs shortlisted` | 只看准备投递 |
| `/jobs applied` | 只看已投递 |
| `/jobs interview` | 只看面试中的岗位 |
| `/jobs offer` | 只看 Offer |
| `/job 1` | #1 的 JD、来源/详情链接、评分、推荐原因、时间与备注 |
| `/status 1 applied` | **仅登记你已经完成的投递**，不会向招聘网站提交资料 |
| `/note 1 已收到一面邀请` | 追加备注，保留原备注 |
| `/funnel` | 岗位总数和八种当前状态的数量（每个岗位只计一次，非累计阶段人数） |

全部状态：`discovered` 已发现、`shortlisted` 准备投递、`applied` 已投递、`written_test` 笔试、`interview` 面试、`rejected` 被拒、`offer` Offer、`withdrawn` 主动放弃。无效状态、未知编号会报错，不改记录。可以跳过阶段或更正手动登记的状态，不强制逐步流转。

例如 `/jobs` 中显示 #1 后：

```text
/job 1
/status 1 shortlisted
/status 1 applied
/status 1 interview
/note 1 已参加一面，等待反馈
/jobs interview
/funnel
/quit
```

退出重启后进度仍在。首次登记 `applied` 时记录投递时间，后续进入笔试、面试、被拒或 Offer 不会抹掉；直接登记 `interview` 不会猜测投递时间。重复登记相同状态不刷新时间；备注追加不改变最近状态更新时间。

数据库新增 `job_library`（岗位与进度）、`job_library_links`（搜索/JD版本到稳定编号的映射）、`job_library_migrations`（一次性导入标记）。保留原岗位/报告/去重表，不保存整份简历。首次打开旧状态目录，会导入现有岗位。v3 保有 JD 原文与来源时直接恢复；早期手动分析未保存 JD 原文的条目明确显示“旧版本未保存”，重导同一材料后补齐，不伪造 JD。

同公司、岗位名称、实际工作城市、招聘类型用于识别职位；明确不同详情链接且不同 JD 的同名职位分开保存。已有 v3 确认的跨来源重复仍合并。发现相同岗位复用稳定编号，保留投递状态、首次发现时间、投递时间、最近状态更新时间和备注。JD 实质变化时更新该职位的 JD，清除旧版本匹配度；原 v3 按 JD 版本缓存，变化后的 JD 重新分析，避免拿旧要求的评分冒充新结果。推荐等级直接复用既有评分和城市策略：广州 >>> 上海 > 深圳，符合原条件时允许有解释的城市越级。

只查看岗位库也可一次运行后退出：

```zsh
.venv/bin/python -B main.py --agent job --mode local --list-jobs --state-dir .agent-platform/job-v3-third-live-check
```

测试全部使用离线数据和 mock，无真实 API：

```zsh
.venv/bin/python -B -m unittest -q test_job_agent test_job_agent_llm test_job_agent_search test_job_agent_v3_regressions test_job_agent_library
.venv/bin/python -B -m unittest -q
```

## 最简单的使用方式

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B main.py --agent job
```

已有真实简历时，推荐直接指定文件（含空格的路径用引号包住）：

```zsh
.venv/bin/python -B main.py --agent job --resume "/绝对路径/我的简历.pdf"
```

看到“简历已在本地载入”后，直接粘贴 JD，单独输入 `.end` 自动分析。之后继续粘贴下一份 JD，无需重复输入背景。可把路径换成 `.docx`、`.txt` 或 `.md` 文件。简历加载失败会明确说明原因并退出，不会带着空背景继续分析。

未指定 `--resume` 时仍可使用原来的粘贴流程：

1. 按提示粘贴自己的简历或个人背景：学历/年级、专业、已完成项目、能使用的技能、正在学习或尚未掌握的技能。单独输入 `.end` 结束，不必先整理成正式简历。
2. 直接粘贴一份岗位 JD，包含公司/职位、职责和要求，单独输入 `.end` 结束。
3. 程序自动执行三个 Skill，展示岗位提取、当前背景匹配、已有能力、学习差距、待确认能力和最先处理的三项。
4. 继续粘贴下一份 JD 即可复用本次背景；`/jobs` 看记录，`/quit` 退出。

不需要再输入 `/analyze`。旧的 `/paste-jd`、`/paste-resume`、`/analyze` 等命令仍可使用。多行输入中 `.cancel` 取消；未输入 `.end` 就结束输入的材料不会被导入或分析。

同一 JD 会复用提取结果；换背景后重新匹配。背景只在本次进程内复用，不自动保存完整简历。已经有背景文件时，可直接载入后连续粘贴 JD：

```zsh
.venv/bin/python -B main.py --agent job --resume /绝对路径/resume.md
```

## 可选：先检查人工样例

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B main.py --agent job --jd job_agent/examples/jd.example.md --resume job_agent/examples/resume.example.md
```

依次实际执行：加载三个 Skill → JD 提取 → 当前简历匹配 → 技能差距分析。默认本地规则处理实际文本，模型决策是离线替身，不收费。样例明确是人工材料，不冒充你的正式简历，也不是真实公司正在招聘。

## LLM 模式（确认后才收费）

本轮只完成离线测试，尚未发起真实付费调用。下面的模型启动方式会等待当前 Terminal 中的材料与费用确认；没有确认不会发送。

继续从环境变量读取 `OPENAI_API_KEY`、`OPENAI_BASE_URL`，不自动加载 `.env`。模型由 `--model` 优先指定，其次 `OPENAI_MODEL`，最后默认 `gpt-6-sol`。Key 由你在本地设置，不写进代码、文档或聊天。项目原中转站的非秘密配置示例（不会发起请求）：

```zsh
export OPENAI_BASE_URL=https://api.likeai520.cc/v1
export OPENAI_MODEL=gpt-6-sol
```

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B main.py --agent job --mode llm --model gpt-6-sol --resume "/绝对路径/我的简历.pdf"
```

粘贴 JD 并用 `.end` 结束后，程序说明目标 API、模型、材料范围与保守预算。只有输入“确认 LLM 分析”才开始。下一份 JD 或替换简历后同样确认，避免把旧确认扩大到新材料。管道输入不能静默开启收费模式。旧 `--live` 属于 Platform，本业务使用 `--mode llm`。

若已经有 JD 文件，也可以一次分析后退出：

```zsh
.venv/bin/python -B main.py --agent job --mode llm --jd "/绝对路径/jd.md" --resume "/绝对路径/我的简历.pdf"
```

先检查数据范围和费用估算，**完全不请求 API、不写入状态**：

```zsh
.venv/bin/python -B main.py --agent job --mode llm --llm-dry-run --jd job_agent/examples/jd.example.md --resume job_agent/examples/resume.example.md
```

预览不需要 Key，但需要配置有效的 `OPENAI_BASE_URL` 和对应模型/单价。默认 `gpt-6-sol` 沿用项目确认的中转价格：输入 $0.30、输出 $1.50/百万 token，暂按 $1=10元；不是 OpenAI 官方报价。其他模型需设置 `JOB_AGENT_INPUT_PRICE_USD_PER_MILLION` 和 `JOB_AGENT_OUTPUT_PRICE_USD_PER_MILLION`，值为该服务商的单价。`.env.example` 只说明配置，程序不会读取它。

新 JD 的完整闭环最多 3 次模型请求：JD → 匹配 → 准备建议。缓存命中则跳过 JD 请求，但换简历仍重新匹配。每次输出与推理强度按前文任务参数选择；默认超时120秒、本进程6次/1元，均可明确配置，无自动网络重试。启动前预估整个闭环，每次请求前继续检查预算。预览使用保守字节估算，不是实际 token 数或人为的低输入 token 上限；客户端无法保证服务商计费硬上限。

API 异常、非法 JSON、拒绝/不完整输出、证据或用量校验失败时明确提示。默认 `--fallback local` 用本地规则从头完成当前分析，标明实际模式与失败原因；本进程之后停止 LLM 请求，避免后续 JD 重复触发失败收费。`--fallback none` 则停止，返回未完成。没有返回可核对用量时费用标为未知，不能当作 0 元。

LLM 请求使用官方 [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 的 JSON Schema，并在本地校验。接口是否支持本项目模型与结构化输出尚待你确认后的真实测试；不自动切换模型、接口或降低为自由文本。

第一次真实测试建议先用已经准备的人工边界材料，不传你的私人简历：

```zsh
.venv/bin/python -B main.py --agent job --mode llm --jd job_agent/examples/llm-jd.example.md --resume job_agent/examples/llm-resume.example.md --state-dir .agent-platform/job-v2-first-check
```

当前不执行这条收费测试命令。你确认后，在 Terminal 再确认本次范围，最多进行三个请求；核对 MCP/Python基础/API基础不是已掌握、职业目标不是能力、实际项目名支持 AI 应用项目经验，以及结构、岗位记录和真实用量。可先在命令末尾加 `--llm-dry-run` 免费预览。人工验收通过后，再按你另外确认的范围处理真实简历。

简历按原顺序拆成带章节语境的证据段；模型必须引用存在的编号，由宿主还原原文。学习/基础语境不能变成已掌握，职业目标不参与能力证明；项目名不自动证明 MCP/RAG 等具体技术或实习年限。匹配覆盖率、已有能力与差距分类由已检查的结果计算，模型只为当前实际差距提供建议。引用存在不等于语义一定正确，仍需人工核对关系说明。

## 用自己的材料

JD 使用 UTF-8 的 `.md` 或 `.txt` 文件；简历/个人能力说明支持 `.pdf`、`.docx`、UTF-8 `.txt`、`.md`（包括 UTF-8 BOM）：

```zsh
.venv/bin/python -B main.py --agent job --jd /绝对路径/jd.md --resume /绝对路径/resume.md
```

只提取 JD，无需简历：

```zsh
.venv/bin/python -B main.py --agent job --jd job_agent/examples/jd.example.md --skill analyze-jd
```

单独匹配或分析差距：在完整命令后加 `--skill match-resume` 或 `--skill skill-gap-analysis`。工具会在本地取得必要前置结果。

交互时也可用 `/jd 文件路径`、`/resume 文件路径` 导入材料，然后 `/analyze`。用 `/paste-resume` 替换本次背景，再输入 JD 可重新匹配。`/help` 查看命令，`/quit` 退出。

`/resume` 同样支持四种简历格式；路径含空格可加引号。导入失败时保留原背景，继续使用或改用 `/paste-resume`。

简历文件上限 10 MB，提取文字沿用现有 64 KB 材料上限。PDF 必须有可提取文字；扫描图片版不做 OCR，会提示换文字版或手动粘贴。加密 PDF、损坏文件、空文件、格式不支持、UTF-8 解码失败均明确报错。DOCX 读取正文段落与表格，保留换行，不处理图片中的文字；复杂 PDF 排版可能影响文字顺序，建议核对报告中的原文证据。

查看记录：

```zsh
.venv/bin/python -B main.py --agent job --list-jobs
```

## 结果与去重

- analyze-jd：职责、必需/加分技术、学历与经验、关键词和原文要求；未说明字段不猜测。
- match-resume：已掌握/匹配（有文本证据）、学习中/部分匹配、明确差距、未找到证据或待确认；学历/专业和经验单独核对。
- “正在学习、尚未熟练、了解中、仅了解基础”等不会计入已掌握覆盖率；学习标题适用于对应逗号列表或后续行。如果项目中使用过，但当前仍明确写着不熟练，会标为部分匹配。
- 复合标题如“正在学习或尚未熟练”也会保留章节作用域，直到明确的新章节；技能与项目读取共用章节判断，证据中保留相关标题。职业目标、求职方向、正在寻找等内容不参与能力证明；想学习/准备学习仍属于弱证据，不算已掌握。修改代码后退出旧进程，再重新启动 Job Agent 才会加载新规则。
- 明确的项目经历或已完成的 AI Agent Platform、Job Agent 可支持 AI 应用项目经验，保留项目原文；具体技术依赖项目描述，不能从项目名推断 MCP、RAG 或 LLM API 实战。API 基础或普通接口练习只能为 LLM API 提供待确认证据，项目也不替代工作/实习年限。
- skill-gap-analysis：列出岗位相关的已有能力证据，区分学习差距与待确认能力，先必需后加分，同级先明确差距、再学习中、最后待确认；最多三项优先动作及完整准备/验收说明。
- 分析缓存使用统一大小写、全半角和空白后的 JD 内容哈希；相同内容复用提取结果。改 JD 是新分析版本，同一职位在 v4 岗位库中保持原编号和进度；换简历会重新匹配，不返回旧匹配。
- 提取规则升级后，旧记录会在原岗位编号上更新一次，随后继续复用，不增加重复记录；保留原来源与首次记录时间。

local 评分统计词典识别出的技术要求；llm 评分统计模型提取且通过结构/证据校验的技术要求。两个模式都由宿主计算分数，模型不负责报分。报告分别列出必须技能和加分技能的完全匹配、部分匹配、未匹配数量，每项附岗位原文、简历证据与判定原因。

评分规则：完全匹配 `matched = 1.0`，学习中/部分匹配 `learning = 0.5`，其余未找到证据、明确差距、待确认均为 `0`；待确认不等于一定不会，但补充证据前不加分。必须项每项权重 `2`，加分项每项权重 `1`。同一独立技能在必须/加分同时出现时，只按必须项计一次。

“至少一门”“以下任一”“A 或 B”保存为 `requirement_groups`，组合整体只计一个评分项：任选分支取最高证据分，同一分支的全部条件取最低证据分，括号保留作用范围。例如 Python/Java/C++ 任一即可，只写 Python 基础时该组合得 0.5 分，不额外扣 Java/C++；掌握 Python 时得 1 分。每个组合的 `expression_matches` 保留所有分支、选中分支、JD 和简历证据；独立提出的其他必须技能仍单独计分。未登记分支保留待核对，空分支不会视为满足。资格条件属于任选组合时也保留其分支，不把未选资格当作独立硬门槛。

兴趣/意愿保存为 `interests`，Git/GitHub/作品链接等申请材料保存为 `application_materials`，不进入技术评分。混合句中的本科门槛和相关专业优先拆开；资格与偏好各自保留原文和级别。升级后的规则与报告缓存版本会使旧评分失效，岗位记录不会被删除或重复创建。

- **硬技能完全覆盖率（必须项）** = 必须技术完全匹配数量 ÷ 必须技术总数 × 100%。
- **必须项/加分项匹配度** = 对应组的（完全匹配数量 + 0.5 × 部分匹配数量）÷ 该组技术总数 × 100%。
- **综合匹配度** = `[2 × 必须项所得分 + 加分项所得分] ÷ [2 × 必须技术总数 + 加分技术总数] × 100%`。使用原始分值计算，最终显示保留一位小数；没有某组要求时该组显示“不适用”，没有技术要求时综合也“不适用”。只有加分项时综合只按已识别加分项计算。

例如 6 个必须技能中完全匹配 1 项、部分匹配 3 项、未匹配 2 项且没有加分项：硬技能完全覆盖率为 **16.7%**，必须项匹配度和综合匹配度为 **41.7%**。若还有 2 个完全匹配的加分项，则综合为 `(2 × 2.5 + 2) ÷ (2 × 6 + 2) = 50.0%`，必须项指标不变。

这些指标只说明已识别技术的证据匹配，不是整体适合度或录用概率；学历、专业与经验条件仍单独核对。请复核保留的原文。原 `required_coverage` 字段继续保留；新结构化 `match_scores` 保存评分版本、权重、分组数量、分值与综合百分比，无需修改模型 JSON schema、数据库或三个 Skill。

## v3 公开岗位搜索与自动筛选

v2 单份分析用法保留。v3 增加独立搜索入口，继续调用原三个 Skill 和评分；没有安装新 Skill/Plugin/MCP，不登录、不投递。架构、过滤/排序规则、去重、费用和首次验收计划见 [SEARCH_DESIGN.md](SEARCH_DESIGN.md)。

最简单的离线验收（人工岗位，不是真实搜索；无需 Key）：

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B main.py --agent job --search-demo --mode local --analyze-limit 3
```

默认使用人工个人背景。可加 `--resume "/绝对路径/我的简历.pdf"`，继续支持PDF/DOCX/TXT/MD。重复运行会复用同岗位、同背景的分析；`--state-dir .agent-platform/job-v3-demo` 可与已有状态隔离。自备岗位结果JSON可用 `--search-fixture /绝对路径/jobs.json`，字段参考 [search-jobs.example.json](examples/search-jobs.example.json)，先按城市、有效性与去重筛选，再导入最多8项；跨来源别名在剩余额度内保留。

此前小范围真实验收已验证部分高校页面读取、保存和去重；本轮新版城市策略只完成离线/mock验证，重新真实搜索需先确认。必须先核验具体公开来源。复制 [search-sources.example.json](examples/search-sources.example.json) 为自己的JSON配置，填入公开列表页地址或已核验的Greenhouse企业board；只启用1–3个来源。模板全部禁用，不会误访问示例域名。HTML来源支持JobPosting JSON-LD或明确公司/岗位/地点/任职要求的静态详情；动态JS页面、登录、验证码、robots禁止和重定向会跳过，不能保证各招聘网站都能读取。

用户确认后第一次可只搜索，不分析、不发送简历：

```zsh
.venv/bin/python -B main.py --agent job --search --sources /绝对路径/sources.json --allow-network --search-only --search-limit 5 --guangzhou-only
```

核验来源和结果后，使用本地规则自动分析并输出优先列表：

```zsh
.venv/bin/python -B main.py --agent job --search --sources /绝对路径/sources.json --allow-network --resume "/绝对路径/我的简历.pdf" --mode local --analyze-limit 2
```

需要语义分析时将模式改为 `--mode llm`；仍逐份确认材料去向和费用，拒绝确认不会收费。搜索本身不调用付费LLM，模型只用于过滤、去重后选中的岗位；默认最多2个新分析，原LLM每进程6请求/约1元保守预算边界仍有效。搜索only忽略LLM分析，不需要Key。首次真实验收先local，不建议直接批量付费。

输出包括“高优先级/可以尝试/当前差距较大”，每条附综合匹配度、必须技能状态数量、城市、招聘类型、2027届判断、证据与原始链接。被筛除/尚未分析岗位单列，未知届别不会当作符合。结构化完整报告写入原状态目录的 `priority-list-时间.json`；原SQLite增加岗位来源/跨会话分析缓存。包含必要简历证据片段，请按个人求职资料保管，不上传或公开。

搜索只在配置来源内执行，范围与数量有上限，不代表全网岗位覆盖。模板JSON不是可执行脚本。搜索不从网页接受文件路径；简历不会发送给招聘来源。新版城市策略尚未重新进行真实网络验收，本轮只做离线/mock验证。

### 城市搜索与可解释排序

默认广州 >>> 上海 > 深圳，其他城市及地点待确认的岗位不进入核心候选。先读配置中 `search_city: "广州"` 的来源（缺省为广州阶段），有效候选不足才读上海阶段，再不足读深圳阶段；`search_city: "不限"` 是共享列表，同一页面只读一次。该字段表示预先核验的搜索范围，绝不作为岗位地点证据。广州阶段优先配置已核验的公司广州招聘页、广州高校就业网；来源本身位于深圳不代表岗位在深圳。

有效候选指方向、正文质量、招聘类型、届别、学历和截止条件通过、且去重后的岗位，不表示已经匹配简历。`--city-min 2` 为默认扩展阈值，可改1–20；实际不超过 `--search-limit`。前序城市达到阈值就不访问下一城市来源。`--guangzhou-only` 完全关闭上海/深圳扩展。未知/受限来源只报告问题，不扩大访问范围。

工作城市优先读取 JD 明确的工作地点/工作城市（包含正文多城市）；没有正文地点时使用岗位城市字段，不根据高校或公司地址推断。多城市岗位按最偏好的可选城市归组，并明确需要确认团队地点。报告保留实际城市、地点证据和来源，避免将页面元数据冒充正文。

技术匹配公式保持不变：完全=1、部分=0.5、未匹配=0；必须项权重2、加分项权重1。最终排序分为：综合匹配度30% + 必须技能完全覆盖10% + 城市偏好25% + JD岗位质量20% + AI方向相关度10% + 明确2027届适配5%。城市分：广州100、上海45、深圳20，因此同等岗位明显优先广州。

JD岗位质量按明写的AI工程职责35分、可交付实践25分、工程质量20分、带教成长20分计算；报告展示每项证据。公司质量没有独立核验资料时标“待核验”，不从名称、融资广告或来源城市猜测加分。这些分值表示 JD 证据，不保证实际团队质量。

默认按城市，再在同城按推荐等级/排序分排序。已发现并分析的上海/深圳岗位可越级：综合匹配≥80%、必须技能完全覆盖≥70%、必须技能无未匹配、质量≥75、方向相关≥90，明确2027届实习、原文JD且无明确资格差距；与被越过的岗位相比还须匹配度至少高20个百分点、含城市权重的排序分更高。报告列出越过哪个岗位及原因。不为寻找越级岗位无意义扩展网络搜索；未分析岗位不会猜分。

JD分类修复识别编号、Markdown和括号章节，区分职责、必须要求、加分、学历/专业、福利/企业简介；加分项不进入必须技能。旧 local 分析缓存按规则版本失效，历史记录保留，成功 LLM 报告仍遵守原去重规则。本地规则仍不能完整理解“至少满足一项”等组合约束：会保留原文和限制提示，不能把关键词分数当最终录用判断。

## 存储、权限与数据范围

状态隔离在 `.agent-platform/job-agent/`（已由 Platform 的忽略规则覆盖）：`jobs.sqlite` 保存岗位提取结论；`sessions.sqlite` 保存 Job 自己的会话、工具结果及必要简历证据片段。**不另存完整简历或自动建立个人能力档案**；关闭后再次分析需要重新提供简历。岗位记录留在本地，没有自动删除/云端同步功能。

v2 在原 SQLite 中新增两个表，保留原岗位与历史：`llm_jd_analyses` 隔离 model/schema 版本的 LLM 提取结果，不能被 local 当作规则结果；`job_reports` 保存通过校验的结构化最终报告，以 JD、简历指纹、实际模式、模型、Skill、结构版本区分。保存必要证据片段、建议、模式和用量元数据，不保存请求正文或简历原文件。相同 JD 在 local/llm 中仍只出现一个岗位编号。

`permissions.py` 独立限定三个 Skill、四个工具、材料格式与大小；宿主实际校验。`resume_loader.py` 只在宿主读取你直接指定的简历，不增加模型工具或业务 Skill。工具只有 Skill 加载和三个业务分析函数。文件只能由你直接指定或粘贴，模型不能传入路径、运行命令、改源码、读取 .env、联系招聘方或自动投递。沿用普通文件与链接检查，包含明显凭据的材料拒绝导入。JD/简历中的指令不能改变权限。

默认 `--mode local` 不需要 Key、不联网、不收费，即使配置了环境变量也不会自动使用。llm 确认后仅发送本次 JD、提取后的简历证据段和本次中间结果；不发送 PDF/DOCX 二进制、路径、项目源码、Key 或历史会话。证据段可能包含简历私人信息，可先使用 `/paste-resume` 提供脱敏背景。`store=False` 不代表第三方服务商保证不留存。Platform 原有真实模式入口不受影响。

## 复用关系与测试

`job_agent/runtime.py` 继承 PlatformRuntime，继续复用 Runner、会话、工具事件和停止逻辑。业务工具按模式选择原有规则函数或 `llm.py` 的有界模型适配，`schemas.py` 定义固定输出。SDK 工具调度仍由本地 JobModel 按现有顺序执行；llm 的真实请求负责业务理解，最多三次，不另花模型请求调度工具。三个 Skill 的文件与名称保留在 `job_agent/skills/`。

当前项目虚拟环境已安装 `pypdf==6.10.0`。新环境需安装项目 `requirements.txt` 中的依赖（使用官方 PyPI）；不安装 Skill，不需要账号或付费服务。若缺少 `pypdf`，PDF 加载会提示缺失依赖，TXT/MD/DOCX 仍可读取。

```zsh
.venv/bin/python -B -m unittest -q test_job_agent test_job_agent_llm test_job_agent_search
.venv/bin/python -B -m unittest -q
```

结构盘点与实施计划见 `job_agent/PLAN.md`。Agent 配置方式核对 [官方 Agent definitions 文档](https://developers.openai.com/api/docs/guides/agents/define-agents)。


### v3 第二次验收后的解析修复

公开HTML按独立岗位卡片读取（role-card/job-card/position-card等），识别卡片标题、实际工作地点、公开详情链接及完整JD；不会把投递侧栏、其他岗位或网站导航混入单份JD。JSON-LD支持JobPosting、列表、@graph/mainEntity，以及同页@id组织/地点引用；不访问@id外部链接、不执行JS。公司名必须有明确配置或页面证据，未知地点不会按来源网站所在地猜测。

岗位记录新增可选字段 `source_url`、`detail_url`、`parsing_evidence`；原 `url` 保留兼容。列表页可在现有同域、robots和请求上限内继续读取公开详情。可读但无完整JD的页面会明确记录“来源可读但解析失败”，并保存失败来源及当前页面URL；登录/验证码/HTTP错误仍按访问失败跳过，绝不绕过。产品/运营/商务岗位不会计入开发实习城市扩展阈值。

离线验证第二次保存的广州页面：

```zsh
cd ~/ai-agent-lab
env -u OPENAI_API_KEY -u OPENAI_BASE_URL .venv/bin/python -B -m unittest test_job_agent_v3_regressions -v
```

该测试使用 `tests/fixtures/job_agent/flypai-careers.html`（第二次验收取得的公开页面岗位区快照，保留原标题和五张卡片，去除CSS、脚本和导航）及Mock HTTP。无需新Skill、插件、Key或付费请求；保存页面自动提取5张卡片，最终筛出1个广州研发实习岗位。实时页面内容仍需第三次真实验收核对，本轮未重新联网。

### 最终 interview-prep 单次真实验收

仅人工简历/JD，不重复 resume-tailor。原先0.20元预算保留，新质量参数单稿8192输出、medium推理，按30000输入起点保守预估0.21288元，原预算不足会安全停止。建议先确认0.30元边界；不自动提升预算，不为旧预算下调质量。

以下仅预览，无需Key、不请求API：

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B accept_job_v6_interview_final.py --budget-rmb 0.30
```

用户确认该方案后，在已设置环境变量的Mac Terminal中运行同一命令加 `--run`；脚本先展示全部人工材料、端点、模型、推理/输出参数和费用，再要求输入“确认面试验收”。本轮仍最多1次、不重试、不修正；这是单次验收的明确范围，不是生产Skill默认限制。结果保存于 `.agent-platform/job-v6-interview-final-api-acceptance`；已有记录只显示，不再请求。历史首次验收脚本仅用于保留当时4096/none、两次/0.35元范围，不能代表当前业务默认质量。
