# AI Agent Platform v1.0：盘点与实施记录

## 修改前盘点（2026-10-04）

项目没有 Git 仓库；复用现有 `.venv`：Python 3.14、openai-agents 0.22.3、openai 3.23.0、pydantic 2.13.5。本次不安装依赖、不建立远程仓库、不上传源码。

| 能力 | 已有文件/入口 | 整合方式 |
| --- | --- | --- |
| Tool Calling / Loop / 费用 | `agent.py`、`tools.py` | 复用 SDK Runner、add、LoopRecorder 费用检查 |
| Terminal / 文件 | `terminal_tool.py`、`file_tools.py`、`repair_tools.py` | 复用白名单与文件身份检查；只操作新工作区样例 |
| 自动测试 / Diff / 权限 | `autotest_*`、`diff_*`、`permission_*` | 复用 Diff、Decision/PendingWrite；修改前展示差异并确认 |
| Session / Context | `session_agent.py`、`compression_agent.py` | 复用 SQLiteSession，按完整轮次缩短历史；保留课程比较实验 |
| Memory | `memory_agent.py`、`memory_tools.py` | 复用 MemoryStore/MemoryGrant，保存一条用户明确指定的偏好 |
| Skills | `skill_agent.py`、`skill_tools.py`、`lab_skills/` | 复用 SkillCatalog，按需加载现有检查程序输出的 Skill |
| Goals | `goal_agent.py`、`goal_tools.py` | 复用 GoalAccess，核对两份人工报表，保留步骤与证据 |
| Background Tasks | `background_*` | 每次任务使用原有 BackgroundTasks 管理器，统一查询与关闭 |
| Scheduled Tasks | `scheduled_*` | 复用进程内 ScheduledTasks；短延迟 0–5 秒，不冒充系统提醒 |
| Subagent / Multi-Agent | `subagent_*` | 复用 SubagentAccess 与 SDK Agent.as_tool，子 Agent 独立读/算/返回 |
| TUI / GUI | `ui_runtime.py`、`tui_agent.py`、`gui_agent.py` | 新终端入口承载平台；原两种教学界面及入口保留 |
| 真实项目实战 | `jieqi_agent.py`（实战记录仅本地保留） | 保留独立入口，不继承其源码上传授权 |

教学代码主要散放在项目根目录，相互通过原模块名导入，不能直接搬走。新建 `agent_platform/` 收拢整合代码，旧 Demo 和既有练习结果留在原位置。

修改前基线：` .venv/bin/python -B -m unittest -q` 运行 221 项，218 项通过、3 项报错。三项均因 `repair_agent.main` 缺少 `resume_file` 参数。实现时单独修复续跑接口，不削弱断言。

## 实施计划

1. 新建统一工作区和工具适配层，复用已有安全实现。
2. 新建 `main.py` 与简单交互式终端：自然语言任务、工具事件、Diff 确认、会话切换、状态查询、退出清理。
3. 加入本地模拟模型和真实模型接口；模拟模式费用为零，真实模型仍从环境变量读取配置，禁用 tracing/自动重试，费用与主子请求统一计数。
4. 接入 Session、Memory、Skills、Goals、后台/定时任务和嵌套 Agent，避免只做菜单展示。
5. 增加端到端与边界测试、演示脚本、README；修复已有续跑测试问题，验证原 Demo 可用。

## 验收标准

- 一个入口能连续接收任务，并显示真实本地工具结果；默认不联网。
- SQLite 会话能恢复；切换会话后仍可读取明确保存的偏好。
- 文件写入先读、展示具体 Diff、用户确认后才写；拒绝、陈旧原文、越界路径均不写入。
- 后台与定时任务不阻塞继续输入；退出时取消未完成任务，不遗留进程。
- 子 Agent 独立执行读取和汇总，主 Agent 接回有证据的结果。
- 旧课程入口/样例保留；新增测试与原有测试通过；README 启动命令实际跑过。
- 不在源码/日志中保存密钥，不调用真实收费 API，不发送现有真实项目源码。

## 范围

v1.0 是可演示的受限工作台。默认模拟模型只识别文档中的固定句式，真实工具、SDK 循环、会话和权限实际执行；模拟通过不能证明真实模型质量。任意 Shell、任意项目改写、MCP 新连接、长期系统调度、互联网发布不属于本次整合。

## 最终实施结果

实施计划已完成。新增 `main.py`、`agent_platform/`（runtime、workspace、demo、terminal_ui、gui）、`test_platform.py`、`requirements.txt` 与 `DEMO.md`，更新 README 和忽略规则。原教学模块没有搬动；只给 `repair_agent.py` 补回缺失的续跑接口，既有练习结果未重置。

平台还直接复用了 PermissionAccess/AutoTestAccess：calculator.py 的真实失败测试、具体 Diff、拒绝/批准和批准后的同一测试验收已接入。网页与终端调用同一个平台核心，原教学全屏 TUI/GUI 仍可按原命令运行。

最终验证（2026-10-04）：

- 全量 ` .venv/bin/python -B -m unittest -q`：243 项全部通过（原有 221 项、新增 22 项）。
- `main.py --self-check`：完整演示通过，临时工作区内实际执行 SDK、程序和测试，没有网络/收费模型请求。
- 实际交互式 Terminal：37+58=95、连续加 2=97、后台汇总=6、定时汇总=15，多行输入及正常退出通过。
- 实际浏览器：计算结果 95、提交按钮禁用/恢复、真实失败与待确认 Diff 可见；GUI 控制器的批准写入另由集成测试验证。
- README 的 `--help`、默认终端、单次任务、自检、网页启动命令已运行；本次启动的进程和临时网页均已关闭。
- 14 个改动文件的语法、末尾空格、配置密钥检查通过；新文档链接存在。没有 Git 仓库，未 commit、push 或公开发布。

真实模型适配仅通过模拟 HTTP 验证地址、store/tracing、请求与费用上限，没有进行真实收费验证。没有发送现有真实项目源码，没有新增依赖、账号权限或 MCP 连接。
