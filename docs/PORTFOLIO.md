# 面向 AI Agent 开发实习的项目展示

## 可以使用的项目描述

在 Codex 辅助下开发并验证 AI Agent Platform 与 Job Agent：复用 Agents SDK、工具适配、SQLite会话和记忆、Skill与权限机制，实现公开岗位搜索、证据约束匹配、分层评分、岗位状态管理，以及针对JD的简历与面试准备。保留local与需授权的LLM模式，覆盖去重、异常恢复、个人资料保护和原文真实性回归。

这段描述不声称独立从零完成全部代码，不把测试数量等同于生产用户规模，不编造投递、录用、效率提升或业务营收。此前人工材料真实LLM验收通过，但当前搜索/系统部署验收应按最新记录分别说明。

## 七到十分钟演示

1. **底座，约2分钟**：按根目录 `DEMO.md` 演示连续计算、工具事件、修复样例Diff与拒绝/批准。说明本地模型是固定句式的模拟，真实工具和测试确实执行。
2. **求职闭环，约2分钟**：运行人工JD/简历，展示职责、必须项、加分项和组合条件；对照Python基础、学习中RAG等证据解释部分匹配。不要为面试公开真实简历。
3. **岗位库，约1分钟**：`/jobs`、`/job 1`、`/status 1 shortlisted`、`/note 1 待准备项目答辩`。重复同一人工岗位演示编号不增加、状态不重置。
4. **准备材料，约2分钟**：`/tailor 1` 和 `/interview-prep 1`，打开独立Markdown材料，比较修改前后、项目证据、参考答案与学习状态。说明local结果与真实LLM语义生成的区别。
5. **可靠性，约1分钟**：展示人工公开搜索fixture及回归测试，解释网页变化、身份未知、断网、睡眠、JD更新处理。用准备好的plist讲系统部署。本机已授权安装且系统启动退出正常；手动联网复验遇到安全地址校验拒绝，真实到点/唤醒仍待观察，不能据此宣称长期稳定抓取。其他开发者安装前仍需自己授权。
6. **边界，约1分钟**：说明AI辅助写代码，自己实际参与需求、运行、复测和质量审查；哪些实现能解释、哪些仍学习中必须如实回答。没有自动投递或不受限浏览器自动化。

安全的人工演示命令（项目根目录）：

```sh
.venv/bin/python -B main.py --self-check
.venv/bin/python -B main.py --agent job --mode local \
  --resume job_agent/examples/resume.example.md \
  --jd job_agent/examples/jd.example.md \
  --state-dir .agent-platform/portfolio-demo
.venv/bin/python -B main.py --agent job --search-demo --mode local \
  --state-dir .agent-platform/portfolio-search
.venv/bin/python -B main.py --agent job --mode local \
  --resume job_agent/examples/resume.example.md \
  --state-dir .agent-platform/portfolio-demo
```

## 技术答辩准备

| 主题 | 应能展示的证据 | 不应夸大的范围 |
| --- | --- | --- |
| Agent Loop | Runtime、SDK工具事件、子Agent调用 | 固定编排不等于自主任务规划 |
| 权限 | 允许列表、Diff审批、文件身份与链接检查 | 不是通用不可信代码沙箱 |
| Memory/Session | SQLite记录、重启恢复、偏好与会话分离 | 不是经过大规模验证的长期智能记忆 |
| 求职证据 | JD与简历原文、学习状态、结构校验 | 模型/规则命中不证明实际熟练度 |
| 搜索和排序 | 解析fixture、来源证据、组合评分与城市越级理由 | 不证明全网覆盖或录用概率 |
| 调度与持久化 | 可控时间测试、同岗位去重、状态备注保留 | 离线通过不等于系统部署成功 |

每个主题先自己阅读对应代码并能解释测试，再列为“已理解并验证”。不理解的实现写“AI辅助实现，正在补充理解”，不要照背成个人独立设计成果。个人贡献表和技能熟练度需要由本人确认，项目文档不能替本人作证明。

## 公开发布准备

公开文件只含源码、五个Skill、人工样例、合成测试、依赖与产品文档。真实简历、数据库、LLM原始记录、本机部署文件和授权指令不上传。发布前重新审查暂存区、历史和许可；本地Commit不等于已发布。所有者已决定许可证暂不添加：公开展示和授予他人复用许可是不同操作。历史身份已按授权清理，正式创建Public仓库与上传仍等待另一次明确授权。
