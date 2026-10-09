# 成熟方案评估与选择

检查日期：2026-10-09。工作分支 `codex/open-source-integration`；保留原稳定提交和上一阶段未提交自动化代码。未推送、未调整生产launchd、未调用模型或真实投递。

## 本轮目标与验收标准

输入：现有代码、官方仓库/包元数据、人工JD与人工简历。输出：选型记录、最小可选浏览器集成、离线端到端证据。

1. 先核对依赖、许可证、维护记录和适用场景，再选择一个浏览器组件。
2. 动态岗位节点经真实浏览器渲染后，继续由原JD解析器处理，保留来源/详情链接/地点证据。
3. 真实浏览器完成模拟表单文本、选择、确认和内存简历文件上传；无未知字段猜测。
4. 原申请账本继续负责去重、次数限制、紧急停止与未知结果查询恢复。
5. 外部HTTP、WebSocket及Service Worker不会成为浏览器传输资料的通道；不连接个人浏览器。
6. 原测试通过；新增真实Chromium测试必须实际运行，不能用跳过结果代替验收。

实施顺序：结构/风险审查 → 官方资料选型 → 隔离安装 → 可选适配层 → 模拟端到端 → 全量回归与Diff检查。

## 当前实现的三类划分

| 模块 | 分类与处理 | 原因 |
|---|---|---|
| JD职责/必须/加分、任选组合、学历/专业/届别/实习身份 | 自有业务逻辑，保留 | 与中国实习求职及证据边界直接相关；浏览器框架不能替代资格判断 |
| 五个Skill与LLM Schema/原文证据检查 | 保留 | 防项目串证据、学习中升级掌握、编造经历；外部Agent不自动提供这些保证 |
| 城市分阶段搜索、可解释评分/越级推荐 | 保留 | 广州远高于上海/深圳的明确用户偏好 |
| 稳定岗位ID、JD版本、投递状态/备注、分析缓存 | 保留SQLite业务模型 | 不能让外部工具重建库或重置真实求职进度 |
| 预算预留、未知费用停止、申请意图/回执、次数/去重/紧急停止 | 保留应用层防护 | 通用浏览器重试不能解决“已收件但超时”的业务歧义 |
| Agent Loop、Tool Calling、SQLiteSession、文件解析 | 已复用成熟SDK/库 | 现有Agents SDK、SQLite、Pydantic、pypdf已经承担通用机制，不应另换Agent框架 |
| 浏览器定位、等待、文本/选项/文件输入、请求拦截 | 交给Playwright | 原MockATS直接调用Python方法，尚没有真实DOM/浏览器；这是缺失能力，不是已有生产浏览器的重复实现 |
| 公开HTML树/JSON-LD与岗位卡片解析 | 暂保留边界清晰的适配 | 当前HTMLParser承担有限识别，不重做通用浏览器；若来源复杂度增长，再考虑成熟DOM解析库 |
| 小规模抓取队列/限流/robots与来源健康 | 暂保留 | Crawlee可替代部分通用调度，但当前最多数页，迁移会涉及现有网络安全边界与状态模型，收益不足 |
| 合法公开招聘接口、网站特定表单映射、开放状态复核 | 仍缺生产接入/核验 | 组件成熟不等于站点许可、完整JD、2027资格和真实收件回执已经具备 |

“之前重复造轮子”主要风险在继续扩展自写抓取调度和将来手写浏览器等待/输入机制。现有业务规则和防重复申请账本并非可随意删除的通用轮子；本轮没有发现需要重写整套Platform的依据。

## 官方项目核对

以下维护日期来自GitHub仓库元数据的 `pushed_at`，只证明有仓库活动，不证明版本稳定或网站适配仍有效。main分支版本与PyPI正式发布明确区分；所有未安装组件只做静态资料/依赖评估，没有声称完成兼容性实测。

| 项目 | 维护/兼容与许可证 | 功能、成本和风险 | 本轮决定 |
|---|---|---|---|
| [Playwright Python](https://github.com/microsoft/playwright-python) | 仓库活动2026-10-08；PyPI正式版1.63.0，Python≥3.10；Apache-2.0；已实测本机Python3.14 | 确定性定位/自动等待/文件输入/网络模拟；本地运行无需LLM或云费用。浏览器及第三方页面仍有攻击面，需隔离和及时更新 | **唯一接入**，可选依赖、离线页面、独立环境 |
| [browser-use](https://github.com/browser-use/browser-use) | 活动2026-10-07；main 0.13.11，Python≥3.11；MIT | 适合自然语言多步浏览器任务，但重复现有Agent编排；当前main固定openai2.26.0、pypdf6.16.2，与本项目3.23.0/6.19.0冲突。依赖含遥测/多模型客户端，云/模型可能收费；profile/cookie上传需额外审查 | 暂不接入；不能为引入框架降级安全更新依赖 |
| [Stagehand](https://github.com/browserbase/stagehand) | 活动2026-10-09；当前Python SDK main4.2.0，Python≥3.11；MIT | act/observe/extract及自修复有价值；当前SDK支持local_browser，并非只能云端。语义动作需要模型，云浏览器另需Browserbase；增加运行链、页面内容外发和模型决策风险 | 暂不接入；确定性表单已可由Playwright解决。旧[stagehand-python](https://github.com/browserbase/stagehand-python)已归档，不能依旧仓库判断新SDK不存在 |
| [JobSpy](https://github.com/speedyapply/JobSpy) | 活动2026-10-08；main1.3.0，Python≥3.10；MIT | 支持多家海外/区域招聘板，输出DataFrame；无内置BOSS/智联/前程无忧适配。代理、指纹/反封锁配置不构成采集许可；没有申请流程。包本身免费，代理可能收费 | 不接入广州核心流程，未证明有足够广州AI实习增益 |
| [DrissionPage](https://github.com/g1879/DrissionPage) | 活动2026-09-14；Python浏览器/请求工具；当前[许可证](https://github.com/g1879/DrissionPage/blob/master/LICENSE)为自定义非商业限制，不是MIT/Apache | 中文资料友好；能做浏览器与HTTP操作，但与Playwright能力重叠，个人profile/调试端口同样有隐私风险；未在Python3.14实测 | 不接入；后续商业用途需另核授权，不能把“公开源码”说成宽松开源 |
| [Crawlee Python](https://github.com/apify/crawlee-python) | 活动2026-10-08；main1.10.4，Python≥3.10；Apache-2.0 | 成熟抓取队列/并发/持久存储，可本地运行；Apify云为可选收费服务。指纹/代理与重试策略需单独限制 | 留作来源规模增大后的候选，当前会重复小队列与状态机制 |
| [CareerSail](https://github.com/genius916/CareerSail) | 活动2026-09-16；package2.0.0，Node实现；MIT | 中国校招工作流/招聘系统链接模式可作人工研究参考，依赖宿主Agent、浏览器或飞书工作流；会重复岗位看板、简历与面试管理。源码公开不证明接口许可 | 参考思路，不安装Skill/启动脚本/飞书能力，不复制业务模块 |
| [boss-jobseeker](https://github.com/aliu-ronin/boss-jobseeker) | 活动2026-03-23；Chrome扩展1.3.2，非Python；GPL-3.0 | 操作登录后聊天页，自动消息/简历、LLM与可选通知；浏览器注入/站点权限、凭据存储、自动重试均需专项审查。规模与活动不足以当作成熟生产保证 | 不接入；当前不授权登录、发消息、个人资料外发，许可证也需单独评估 |

来源：各仓库README、LICENSE、pyproject/package/manifest，及 [Playwright PyPI](https://pypi.org/project/playwright/1.63.0/)。未执行外部README中的curl脚本或安装指令；没有安装额外Skill/Plugin/MCP。

同日追加核对PyPI正式发行元数据：browser-use0.13.11（Python≥3.11,<4）、stagehand4.2.0（≥3.11）、python-jobspy1.3.0（≥3.10）、DrissionPage4.1.1.4（≥3.6）、crawlee1.10.4（≥3.10）。声明允许某Python版本并不等于本项目依赖组合实测通过；本轮仅Playwright组合安装/执行过。

## 招聘接口与广州覆盖的现实边界

- 公司官网公开JD/JSON-LD/静态卡片继续优先，飞派/Coremail来源配置不动。
- [Greenhouse Job Board官方文档](https://docs.greenhouse.io/job-board.html)：公开GET与提交POST权限不同，后者需要招聘方Job Board API认证；现有只读解析可保留，不据此新增真实申请。
- [Lever官方Postings API](https://github.com/lever/postings-api)：有公开岗位与申请文档；目标租户、限制、字段、资料同意与收件确认仍须核验，不表示个人求职者获得通用批量投递许可。
- 国内Moka/北森/飞书招聘等公开页面是后续候选。企业HR管理API与求职者公开职位接口不同；浏览器能看到请求，不等于允许复用未公开接口。未核验企业/岗位不写入已验证来源。
- 本次提升的是“原静态解析器看不见的内联JS岗位节点”以及浏览器表单可靠性测试。依赖外部JS/API的SPA仍不自动加载；不会为了覆盖率开放浏览器任意联网或解除DNS/robots限制。
- 没有凭mock新增真实广州岗位，也不承诺来源数量或岗位数量已增加。可靠增量应以后续公司来源小规模只读验收衡量。
- 本轮实测飞派/Coremail现有公开URL时，原PublicFetcher因DNS指向非公开地址而拒绝，两站HTTP请求数均为0，没有使用历史缓存、修改解析器放宽防护或更改已验证来源配置。故本轮不报告实时岗位数量/覆盖率提升；这属于真实来源验收未通过，不影响离线浏览器测试。

## 接入设计与后续边界

`PublicFetcher（robots/DNS/数量限制） → HTML快照 → 离线Chromium → 原parse_public_page → 原资格/Skill/评分/SQLite流程`。

模拟申请：`原ApplicationService材料/资格复核 → OfflineBrowserATS语义定位/填写 → 内存/apply拦截 → 原MockATS持久回执 → 原申请状态机`。没有向JD链接导航、任意提交选择器、个人Chrome/CDP、登录资料或真实HTTP POST出口。

真实使用还缺：明确允许自动操作的具体站点/接口、逐站字段映射及同意项、实时开放/2027资格核对、个人资料发送授权、单次试投批准、收件/重复请求核对方案。自动LLM还需模型/中转、数据范围、单价、每日预算/次数/期限授权；不会借本轮研究自动启用。
