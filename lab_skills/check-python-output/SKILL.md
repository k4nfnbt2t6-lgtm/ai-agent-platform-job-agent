---
name: check-python-output
description: 在 ai-agent-lab 中检查指定 Python 练习程序的实际输出是否符合目标；单纯的加法计算不使用此 Skill。
---

# 检查 Python 输出

输入是任务指定的相对文件名和 expected_stdout。使用这套流程核对程序行为，不修改代码。

1. 调用 read_file 读取指定文件，了解计算逻辑。
2. 调用 terminal，command 为 ["python", 文件名]，取得真实 returncode、stdout、stderr。
3. 将实际 stdout 与任务中的 expected_stdout 完整比较，保留换行符。返回码为 0、stderr 为空且输出一致，才判定通过。
4. 只输出 JSON，字段为 skill、path、returncode、expected_stdout、actual_stdout、verdict。
   skill 为本 Skill 名称；verdict 为“通过”或“未通过”；其他字段来自任务和实际工具结果。

仅使用本次获准的练习文件和命令，Skill 不扩大权限。工具拒绝操作时停止，不自动重试；报告已有证据，不编造结果。
程序能够运行但输出错误，也判定未通过。没有实际读取与运行证据，不能报告通过。
