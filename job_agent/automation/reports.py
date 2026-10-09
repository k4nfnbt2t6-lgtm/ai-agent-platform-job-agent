"""独立、脱敏的运行摘要；不包含简历、表单答案、请求或原始异常。"""

import json
import os
from pathlib import Path
from uuid import uuid4


def save_report(root, summary):
    directory = Path(root) / "automation-reports"
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError("自动化报告目录不能经过符号链接。")
    directory.mkdir(mode=0o700, exist_ok=True)
    bundle = directory / uuid4().hex
    bundle.mkdir(mode=0o700)
    lines = ["# Job Agent自动化运行报告", f"分析模式：{summary['mode']}",
             f"本次新分析：{summary['new_analyses']}；重复线索：{summary['duplicates']}；本地跳过：{summary['local_skipped']}；待处理：{summary['pending']}",
             f"真实API请求：{summary['real_api_requests']}；真实申请提交：0；估算模型费用：{summary['estimated_real_fee_rmb']}元。",
             "账本金额为配置价格估算，未知费用须核对账单；mock账本金额仅为离线假设，不是收费。",
             "故障类别：" + (summary.get("failure_kind") or "无"), "\n## 岗位与资格"]
    for row in summary["jobs"]:
        lines.append(f"- #{row['job_id']} {row['company']} / {row['title']} / {row['city']}：{row['overall_match']}%，{row['recommendation']}，决策{row['decision']}。")
        lines.append("  原始链接：" + row["url"])
        lines.extend("  核对：" + reason for reason in [*row["reasons"], *row["review_items"]])
    lines.append("\n## 申请处理（仅本地人工材料或模拟）")
    for row in summary["applications"]:
        lines.append(f"- 岗位#{row['job_id']}：{row['state']}" + ("；材料：" + row["materials_file"] if row.get("materials_file") else ""))
    for name, content in (("report.json", json.dumps(summary, ensure_ascii=False, indent=2)), ("report.md", "\n\n".join(lines) + "\n")):
        fd = os.open(bundle / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
    return {"json": str(bundle / "report.json"), "markdown": str(bundle / "report.md")}
