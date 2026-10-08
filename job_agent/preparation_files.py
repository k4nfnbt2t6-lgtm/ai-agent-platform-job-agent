"""仅在私有状态目录创建全新报告包，不接收输出路径、不覆盖任意文件。"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent_platform.workspace import contains_credentials


def usage_line(data):
    fee = data.get("estimated_rmb", 0)
    label = "未知（请求可能已计费，请核对服务商账单）" if fee is None else f"约 {fee:.6f} 元（配置价格估算）"
    text = f"实际模式：{data['mode']}；本次模型请求 {data.get('model_requests', 0)}；费用 {label}。"
    if data.get("quality_warning"):
        text += " " + data["quality_warning"]
    if data.get("quality_needs_review"):
        text += " 自动质量检查仍有待复核项，详见JSON中的quality_review。"
    if data.get("fallback_reason"):
        text += " 回退原因：" + data["fallback_reason"]
    return text


def markdown(data):
    lines = [f"# {data['skill']}：岗位 #{data['job_id']}",
             f"{data['company'] or '公司未说明'} / {data['title'] or '岗位未说明'} / {data['city']}",
             usage_line(data), data["note"]]
    if data["skill"] == "resume-tailor":
        lines.append("正式简历见同目录 tailored-resume.md；本文件仅保存修改说明和核对事项。")
        lines.append("项目顺序：" + " → ".join(data["project_order"]))
        for change in data["changes"]:
            lines.extend(["\n## " + change["section"], "修改前：\n" + change["before"],
                          "修改后：\n" + change["after"], "理由：" + change["reason"],
                          "岗位依据：" + " / ".join(change["jd_evidence"])])
        lines.append("\n缺失或待确认技能不能写成已掌握：")
        for item in [*data["gaps"]["learning_gaps"], *data["gaps"]["evidence_to_confirm"]]:
            lines.append(f"- {item['skill']}：{item['first_action']}；{item['preparation']}")
    else:
        for index, question in enumerate(data["questions"], 1):
            label = {"jd_technical": "JD 通用技术题", "project_followup": "项目经历追问",
                     "learning": "学习／待确认题"}.get(question.get("question_kind"), question["type"])
            lines.extend([f"\n## 问题 {index}（{label}）", question["question"], question["readiness"],
                "参考答案（学习材料）：\n" + question["reference_answer"],
                "知识点：" + "、".join(question["knowledge_points"]),
                "学习／复盘建议：" + question["learning_advice"]])
            if question["follow_ups"]:
                lines.append("追问：\n" + "\n".join("- " + followup for followup in question["follow_ups"]))
        lines.append("\n## 本岗位准备清单")
        for item in data["checklist"]:
            lines.append(f"- {item['priority']} {item['skill']}：{item['action']}；{item['task']} 验收：{item['acceptance']}")
            for topic in item.get("preparation_topics", []):
                evidence = " / ".join(topic["resume_evidence"]) or "未提供该主题的简历证据"
                lines.append(f"  准备主题：{topic['skill']}；状态：{topic['status']}；原文依据：{evidence}")
    lines.append("\n## 资格条件需单独核对")
    for item in data["gaps"]["qualification_checks"]:
        lines.append(f"- {item['requirement']}：{item['reason']}")
    return "\n\n".join(lines) + "\n"


def save_preparation(root, data):
    """每次 UUID 新目录；即使原简历在同一状态目录内也不可能被覆盖。"""
    root = Path(root)
    directory = root / "preparations"
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("准备报告目录不能经过符号链接。")
    if directory.exists() and not directory.is_dir():
        raise ValueError("准备报告目录必须是文件夹。")
    directory.mkdir(exist_ok=True, mode=0o700)
    data = {**data, "created_at": datetime.now(timezone.utc).isoformat()}
    contents = {"report.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n", "report.md": markdown(data)}
    if data["skill"] == "resume-tailor":
        contents["tailored-resume.md"] = data["tailored_resume"]
    if any(contains_credentials(content) for content in contents.values()):
        raise ValueError("报告疑似包含凭据，已拒绝保存。")
    bundle = directory / f"job-{data['job_id']}-{uuid4().hex}"
    bundle.mkdir(mode=0o700)
    paths = {}
    for name, content in contents.items():
        path = bundle / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        paths[name] = str(path)
    return {**data, "files": paths}
