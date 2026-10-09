# 五分钟演示：AI Agent Platform v1.0

启动：

```zsh
cd ~/ai-agent-lab
.venv/bin/python -B main.py --state-dir .agent-platform/interview --session interview
```

已有样例和记忆不会重置；要重新演示失败，用一个新的状态目录名，不删除已有学习结果。

1. 输入 `37 + 58`，实际 add 返回 95；再输入 `继续加 2` 得到 97，展示会话的连续性。
2. 输入 `修复测试样例`，看到真实 unittest 失败、修改 Diff 和申请编号。先 `/reject 该编号`，文件未写入。再次提交任务，然后 `/approve 新编号`，真实测试变为 OK。
3. 输入 `记住：用简单中文解释`，再 `/new`，问 `我的学习偏好是什么`。从磁盘读取同一条偏好，说明持久记忆与临时对话分开。
4. 输入 `2 秒后汇总 4,5,6`，立即得到编号；输入 `37 + 58` 后再 `/tasks`，得到汇总 15。计时器仅依赖当前进程。
5. 输入 `检查 practice.py 的输出`，观察按需加载 Skill、读取、隔离执行与实际输出核对。
6. 输入 `委派汇总 report.json`，观察主 Agent → 子 Agent 的读取/汇总 → 主 Agent 最终结果 400。两层 SDK 循环实际执行，模型决策是本地模拟。
7. 输入 `核对目标：上午 200，下午 250，目标 400`，再 `/goals` 查看四个步骤与证据。复用了原教学的固定报表流程。
8. `/quit` 正常退出。重启相同目录和会话可恢复历史/记忆，后台任务不会跨进程恢复。

网页演示：

```zsh
.venv/bin/python -B main.py --ui gui --state-dir .agent-platform/web-demo
```

在打印的完整本机链接中输入相同任务及确认命令。回到 Terminal 按 Ctrl+C 停止服务。

向面试官如实说明：v1.0 整合了真实工具、会话、记忆、权限和嵌套 SDK 流程；默认模型只识别固定句式。Platform整合时没有做收费验证；后续Job Agent的真实LLM人工材料验收单独记录，不应混为同一验收。它不是通用代码沙箱，不开放任意源码和 Shell，没有新增 MCP 连接。

完整求职项目演示、AI辅助开发的真实边界与答辩准备见 [作品集说明](docs/PORTFOLIO.md)。
