# AI Agent Platform + Job Agent v6

一个复用统一 Agent 底座的技术实习求职项目。Platform 提供工具调用、文件与 Terminal、会话、记忆、权限、任务管理和子 Agent；Job Agent 在同一底座上完成岗位搜索、分析、评分、岗位库、每日检查、简历优化与面试准备。

默认使用 **local / 本地模拟模式**，无需 API Key，不产生模型 API 费用。模拟和规则测试不能代替真实模型质量验收。Job Agent v6 已完成真实人工材料验收与人工质量审查；原始请求、响应和个人求职资料仅保留本地。

## 环境与安装

已验证 Python 3.14。依赖固定在 [requirements.txt](requirements.txt)。在项目目录执行：

```zsh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

已有虚拟环境可以直接复用，安装前核对依赖文件。PDF 使用 pypdf 本地解析，DOCX/TXT/MD 同样不需要模型 API。

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

公开岗位搜索需要明确启用可信来源与网络访问；不登录、不绕验证码。每日检查固定使用 local，不自动调用收费模型。长期任务需要进程持续运行，本次 Git 初始化不启动定时任务。

详细命令、搜索与调度设置见 [Job Agent 说明](job_agent/README.md)。

## LLM 配置与授权

沿用环境变量 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`，配置项见 [.env.example](.env.example)。示例 Key 为空；程序不会自动加载 `.env`。不要把真实 Key 写进源码、命令历史或聊天。

`--mode llm` 在发送前展示材料范围、目标 API、模型、次数与预计费用，等待当前用户确认。连接配置或历史验收不代表授权新的付费请求或真实简历上传。保留结构校验、原文证据核对、有界预算与失败停止；不自动重试可能收费的请求。定时检查仍为 local。

## 测试

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
*_agent.py / *_tools.py  保留原教学入口与底层能力
.agent-platform/         私人状态、数据库及真实验收记录（不纳入 Git）
.local-history/          初始化前文档备份（不纳入 Git）
```

## 版本管理与安全

本地 Git 已初始化，尚未配置远程或发布。可纳入 Git 的内容是源码、五个 Skill、人工样例、离线测试、依赖声明和经过清理的说明文档。

真实简历、个人信息、密钥、数据库、状态、运行日志、真实 API 请求/响应、个人授权指令与历史运行笔记仅留本地。`.gitignore` 是防误提交措施，不能加密文件，也不能阻止 `git add -f`；每次提交/上传前仍需审查暂存区。详见 [安全说明](SECURITY.md)。未经明确授权不推送、不发布、不上传材料。
