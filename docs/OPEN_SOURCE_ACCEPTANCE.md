# 开源选型与浏览器集成验收

日期：2026-10-09；分支：`codex/open-source-integration`。

## 验证结果

| 检查 | 结果 |
|---|---|
| 新增测试 | 17项通过，其中13项实际启动Chromium，4项为启动前边界检查 |
| 全项目 | 767项全部通过，无跳过；包含原750项及新增17项 |
| 原环境Job Agent测试 | 492项运行，479通过、13项可选浏览器测试明确跳过；上述全量浏览器环境已实际覆盖这13项 |
| 依赖 | 原环境与隔离环境pip check均通过；未更改原requirements.txt/生产虚拟环境 |
| 可选新包公告 | OSV查询playwright1.63.0、pyee13.0.1、greenlet3.5.6，未命中公告；非完整安全保证 |
| 运行入口 | 人工材料CLI真实执行两轮，首轮分析1/模拟提交1；重跑分析0/已记录1 |
| Git | 原稳定HEAD保留；新分支开发，未提交/推送本轮修改；原未提交自动化工作全部保留 |
| 每日任务 | 已安装plist与私有每日config SHA256对比原快照一致；未重新加载/安装/启停系统任务 |
| 公开文件检查 | 160个拟公开文件未命中私人文件路径；密钥模式命中仅原SECURITY说明及后台测试的人工占位值；新文件与tracked Diff空白检查通过 |
| 外部行为 | 真实模型调用0、真实申请0、真实个人资料发送0、API费用0元 |

## 覆盖的行为

- 初始静态HTML无法识别的内联JS JSON-LD，经真实Chromium渲染后由原解析器取得岗位。
- 动态岗位卡片、来源/详情链接、JD实际广州地点与解析证据保留，不以来源名称猜城市。
- PublicFetcher的MockTransport/robots前置检查仍有效：禁止时不会进入浏览器渲染。
- 真实Chromium填写文本/下拉/确认框/内存Markdown简历；已确认材料与浏览器传出的表单值逐字段核对。
- 未确认问题、材料篡改、重复申请拦截；原岗位状态未改成applied。
- 明确拒收标failed；无回执网络失败标unknown、不重发；收件后超时通过原回执恢复；错误回执不能标成功；紧急停止阻止恢复。
- HTTP脚本/fetch/iframe/WebSocket拦截、Service Worker禁用；浏览器上下文本身设offline，Chromium sandbox开启。
- 外部脚本SPA/登录验证码快照无可靠JD时明确报告失败，不猜测、不尝试登录或绕过。

## 限制与待验收

本轮只读检查飞派、Coremail均被现有安全地址检查拒绝（HTTP0）。未作网络诊断或防护绕过，未将之前缓存用作实时结果。真实广州岗位覆盖增量没有通过验收。

动态渲染覆盖内联脚本，不是开放任意外部JS/API的SPA抓取器。模拟平台使用原SQLite MockATS账本，并非某个招聘网站生产API。未来逐站接入仍需平台许可、字段/同意项核验、真实资料授权和单次提交验收。

浏览器依赖与运行输出在被Git忽略的 `.agent-platform`；真实简历、生产数据库和日志未参与本轮模拟。秘密扫描与公告查询为风险检查，不能保证不存在未知问题。

本轮新增文件：`requirements-browser.txt`、`job_agent/browser/`四个模块、`test_job_agent_browser.py`、三份开源评估/集成/验收说明；修改README和上一阶段的`automation/applications.py`适配器允许列表。现有五Skill、搜索/评分/LLM/岗位库业务代码未因本轮选型重写。

使用方法见 [浏览器集成](BROWSER_INTEGRATION.md)，选型细节见 [成熟方案评估](OPEN_SOURCE_EVALUATION.md)。
