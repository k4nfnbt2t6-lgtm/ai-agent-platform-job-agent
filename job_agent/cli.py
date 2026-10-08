"""Job Agent 的文本输入与报告；底层输入、会话与运行循环来自 Platform。"""

import asyncio
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_platform.terminal_ui import PROJECT, handle_line, read_line
from agent_platform.workspace import contains_credentials

from .analysis import MATCH_REASONS, STATUS_POINTS, match_scores, validate_text
from .permissions import MAX_MATERIAL_BYTES, TEXT_SUFFIXES
from .runtime import JobRuntime
from .resume_loader import load_resume
from .llm import LLMAnalyzer, LLMConfig
from .job_library import JOB_STATUSES


HELP = """Job Agent v6：分析材料、岗位库、每日 local 更新与岗位简历/面试准备。
直接粘贴个人背景，再粘贴 JD；每份材料用单独一行 .end 结束。
个人背景在本次运行中复用；每份新 JD 自动执行所选 Skill。
/jd 路径                  导入用户明确指定的 UTF-8 .txt/.md 岗位 JD
/resume 路径              导入本地 PDF/DOCX/TXT/MD 简历或个人能力说明
/paste-jd /paste-resume    粘贴多行，单独输入 .end 结束
/analyze                  运行全部三个 Skill，需要 JD 和简历
/analyze-jd               只提取和记录岗位
/match-resume             匹配当前简历
/skill-gap-analysis       根据当前简历找差距并给出准备建议
/jobs [状态]              查看岗位库，可按状态过滤，如 /jobs interview
/job 编号                 查看岗位详情、JD、评分、时间和备注
/tailor 编号              生成新简历与修改说明；llm 模式每次先确认材料和费用
/interview-prep 编号      生成十道技术/项目面试题；llm 模式每次先确认
/status 编号 状态         手动登记状态，如 /status 1 applied（不会实际投递）
/note 编号 备注           追加备注，保留旧备注
/funnel                   查看各当前状态数量及岗位总数
/check-now                在 --watch 入口立即检查一次，不改变每日计划
/last-check               查看最近检查、最近新岗位时间及下次计划时间
/new-jobs                 查看北京时间今日新增岗位
/new /session 名称 /history /usage /quit
状态：discovered / shortlisted / applied / written_test / interview / rejected / offer / withdrawn
管理岗位无需简历；状态、备注在本地保存，重启后保留。
搜索需单独使用已授权的搜索入口；不会因管理岗位自动搜索或投递。
local 默认不联网；llm 每次新材料先确认数据范围和费用，再调用模型。"""
STATUS = {"matched": "已掌握/完全匹配（有文本证据）", "learning": "学习中/部分匹配", "explicit_gap": "未匹配（明确差距）",
          "not_evidenced": "未找到证据/未匹配", "needs_confirmation": "未匹配（待确认）", "not_required": "不要求"}


def read_authorized_text(filename):
    # 路径只来自当前用户的参数/输入，不接受 JD 或模型给出的路径。
    path = Path(filename).expanduser().absolute()
    if path.suffix.lower() not in TEXT_SUFFIXES or path.name.startswith("."):
        raise ValueError("v1 只读取用户指定的普通 .txt/.md 文件；不读取 .env、PDF 或 DOCX。")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("提供的文本文件不能经过符号链接。")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("只可读取独立普通文本文件。")
        data = stream.read(MAX_MATERIAL_BYTES + 1)
    text = validate_text(data.decode("utf-8"), "输入材料")
    if contains_credentials(text):
        raise ValueError("输入材料疑似包含凭据，已拒绝导入。")
    return text, path.name


def print_report(data, selected):
    semantic = data.get("mode") == "llm"
    print("\n分析模式：" + ("llm（语义分析 + 原文证据检查）" if semantic else "local（本地规则）"))
    if data.get("quality_warning"):
        print(data["quality_warning"])
    if data.get("fallback_reason"):
        print("LLM fallback：" + data["fallback_reason"])
    jd = data["jd_analysis"]
    if jd:
        analysis = jd["analysis"]
        print(f'\n岗位：{analysis["company"] or "公司未说明"} / {analysis["title"] or "岗位名称未说明"}')
        record_status = "；已有记录，复用 JD 提取结果。" if jd["cache_hit"] else "；已更新原岗位的提取结果。" if jd.get("record_updated") else "；已记录新岗位。"
        if semantic and not jd["cache_hit"]:
            record_status = "；已保存本次 LLM 提取结果。"
        print("岗位记录：" + jd["jd_id"][:12] + record_status)
        if data.get("job_id"):
            print(f"岗位库编号：#{data['job_id']}；/job {data['job_id']} 查看详情。")
        if selected in ("all", "analyze-jd"):
            for field, label in (("responsibilities", "职责"), ("technical_requirements", "技术要求"),
                                 ("education_requirements", "学历/专业"), ("experience_requirements", "经验")):
                print(label + "：")
                for item in analysis[field]:
                    print("  - " + (f'{item["skill"]}（{"必需" if item["level"] == "required" else "加分"}）：{item["evidence"]}' if isinstance(item, dict) else item))
                if not analysis[field]:
                    print("  未明确说明或未识别，请复核原文。")
            print("关键词：" + ("、".join(analysis["keywords"]) or "未识别"))
            classified = {item["evidence"] for item in analysis["technical_requirements"]}
            classified.update(analysis["education_requirements"] + analysis["experience_requirements"])
            other = [line for line in analysis["requirements_original"] if line not in classified]
            if other:
                print("其他原文要求（需人工复核）：")
                for line in other:
                    print("  - " + line)
    matching = data.get("resume_match")
    if matching:
        match = matching["matching"]
        # 从实际证据判断重算，也可展示没有新评分字段的历史报告。
        scores = match_scores(match["technical_matches"])
        def percent(value):
            return f"{value:.1f}%" if value is not None else "不适用（未识别技术要求）"
        print("\n当前简历匹配：")
        for level, label in (("required", "必须技能"), ("preferred", "加分技能")):
            group = scores[level]
            print(label + "：")
            print(f'  完全匹配：{group["matched"]}；部分匹配：{group["partial"]}；未匹配：{group["unmatched"]}（共 {group["total"]} 项）')
            for item in match["technical_matches"]:
                if item["level"] != level:
                    continue
                print(f'  - {item["skill"]}：{STATUS[item["status"]]}（{STATUS_POINTS.get(item["status"], 0.0):.1f} 分）')
                print("    岗位依据：" + " / ".join(item["jd_evidence"]))
                print("    简历依据：" + (" / ".join(item["resume_evidence"]) or "未找到"))
                print("    判定原因：" + (item.get("reason") or MATCH_REASONS.get(item["status"], "未确认能力，不计分。")))
        required = scores["required"]
        coverage = round(100 * required["matched"] / required["total"], 1) if required["total"] else None
        print("硬技能完全覆盖率（必须项）：" + percent(coverage))
        print("必须项匹配度：" + percent(required["percent"]))
        print("加分项匹配度：" + percent(scores["preferred"]["percent"]))
        print("综合匹配度：" + percent(scores["overall"]["percent"]))
        print("评分：完全匹配=1，部分匹配=0.5，未匹配=0；必须项权重2，加分项权重1。")
        print("待确认和明确差距按未匹配计0分，不表示一定不会。只统计已识别技术，学历/经验单独核对；不是录用概率。")
        for item in [*match["education"], *match["experience"]]:
            print(f'  - {item["requirement"]}：{STATUS[item["status"]]}；{item["reason"]}')
    gaps = data.get("skill_gaps")
    if gaps:
        print("\n已有能力（简历文本有证据）：")
        for item in gaps["strengths"]:
            print("  - " + item["skill"] + "：" + " / ".join(item["resume_evidence"]))
        if not gaps["strengths"]:
            print("  暂无已确认的岗位相关技术；可补充真实项目/能力说明后重新匹配。")
        print("\n优先补强或确认（最多三项）：")
        for index, item in enumerate(gaps["priority_actions"], 1):
            print(f'  {index}. {item["priority"]} {item["skill"]}：{item["first_action"]}')
        if not gaps["priority_actions"]:
            print("  已识别技术暂时没有补强项；继续核对完整 JD 与资格条件。")
        print("\n针对本岗位的准备：")
        for category, label in (("learning_gaps", "学习差距"), ("evidence_to_confirm", "先核对证据")):
            for item in gaps[category]:
                print(f'  - {item["priority"]} {item["skill"]}（{label}）：{item["first_action"]}')
                print("    准备：" + item["preparation"] + " 验收：" + item["acceptance"])
        for item in gaps["qualification_checks"]:
            print("  - 资格核对：" + item["requirement"] + "；" + item["reason"])
        if not gaps["learning_gaps"] and not gaps["evidence_to_confirm"]:
            print("  词典识别出的技术没有未覆盖项；仍需核对未识别要求和经验/学历。")
    print("\n" + ("LLM 结论附原文证据，仍需核对项目细节与熟练程度。" if semantic else "本版为规则与词典分析，保留原文要求；建议同时阅读 JD 原文。"))


def display_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S 北京时间") if value else "未登记"


def print_jobs(runtime, status=None):
    jobs = runtime.workspace.memory.library.list(status)
    print(f"岗位库：{len(jobs)} 条" + (f"；筛选：{JOB_STATUSES[status]}（{status}）" if status else "") + "。")
    for job in jobs:
        score = f'{job["overall_match"]:.1f}%' if job["overall_match"] is not None else "尚未评分"
        print(f'#{job["job_id"]} | {job["company"] or "公司未说明"} | {job["title"] or "岗位未说明"} | '
              f'{job["city"]} | {JOB_STATUSES[job["status"]]}（{job["status"]}） | {score} | {job["recommendation"] or "尚未推荐"}')
    print("用 /job 编号 查看详情；/status 编号 状态 登记进度。")


def handle_library_command(runtime, command, argument):
    """宿主 CLI 直接操作本地记录，不交给模型修改状态，也不运行 Skill。"""
    library = runtime.workspace.memory.library
    if command == "/jobs":
        print_jobs(runtime, argument.strip() or None)
    elif command == "/job":
        job = library.get(argument.strip())
        print(f'\n岗位 #{job["job_id"]}：{job["company"] or "公司未说明"} / {job["title"] or "岗位未说明"}')
        print("工作城市：" + job["city"] + "；城市偏好：广州 >>> 上海 > 深圳。")
        print("当前状态：" + JOB_STATUSES[job["status"]] + "（" + job["status"] + "）")
        print("首次发现：" + display_time(job["first_discovered_at"]))
        print("投递时间：" + display_time(job["applied_at"]))
        print("最近状态更新：" + display_time(job["status_updated_at"]))
        print("来源：" + job["source"])
        print("来源链接：" + (job["source_url"] or "未提供"))
        print("岗位详情链接：" + (job["detail_url"] or "未提供"))
        print("综合匹配度：" + (f'{job["overall_match"]:.1f}%' if job["overall_match"] is not None else "尚未评分"))
        print("推荐等级：" + (job["recommendation"] or "尚未推荐"))
        if job["ranking"]:
            for reason in job["ranking"]["reasons"]:
                print("  - " + reason)
            print("城市越级判断：" + job["ranking"]["city_override"]["reason"])
        print("备注：\n" + (job["notes"] or "暂无"))
        print("JD（" + job["jd_kind"] + "）：\n" + (job["jd"] or "旧版本未保存 JD 原文；重新导入后补齐。"))
    elif command == "/status":
        parts = argument.split()
        if len(parts) != 2:
            raise ValueError("用法：/status 编号 状态，例如 /status 1 applied。")
        job = library.set_status(*parts)
        print(f'岗位 #{job["job_id"]} 状态已登记：{JOB_STATUSES[job["status"]]}（{job["status"]}）。仅本地记录，无外部操作。')
    elif command == "/note":
        identity, _, note = argument.strip().partition(" ")
        job = library.add_note(identity, note.strip())
        print(f'岗位 #{job["job_id"]} 备注已追加。')
    elif command == "/funnel":
        if argument.strip():
            raise ValueError("用法：/funnel（无需参数）。")
        funnel = library.funnel()
        print(f'求职漏斗：岗位总数 {funnel["total"]}；以下按当前状态计数，每个岗位只计一次。')
        for status, label in JOB_STATUSES.items():
            print(f'  {label}：{funnel["counts"][status]}')
    else:
        return False
    return True


def handle_update_command(runtime, command, argument, controller=None):
    if command not in ("/check-now", "/last-check", "/new-jobs"):
        return False
    if argument.strip():
        raise ValueError(command + " 无需参数。")
    from .update_store import UpdateStore, make_summary, print_update, utc_clock
    store = controller.store if controller else UpdateStore(runtime.workspace.memory)
    if command == "/check-now":
        if controller is None:
            raise ValueError("请先用 --watch 配置本地简历和公开来源；或带 --check-now 参数运行一次。")
        submitted = controller.start_check()
        print("已提交后台检查：" + submitted["task"]["task_id"] + "；/last-check 查看进度，每日计划不变。" if submitted["ok"] else submitted["reason"])
    elif command == "/last-check":
        latest = store.last_check()
        if latest:
            print("最近一次检查时间：" + display_time(latest["finished_at"] or latest["started_at"]) + "；状态：" + latest["status"])
            if latest["summary"]:
                print_update(latest["summary"])
        else:
            print("最近一次检查：尚无记录。")
        plan = store.schedule()
        print("最近一次发现新岗位：" + display_time(plan["last_found_at"]))
        print("每日时间：" + (plan["daily_at"] or "未设置") + " 北京时间；下次计划：" + display_time(plan["next_run_at"]))
        print("当前进程调度：" + ("运行中" if controller and controller.watching else "未启动（保存计划不代表正在运行）"))
    else:
        rows = store.today_jobs(controller.clock() if controller else utc_clock())
        print(f"今日新增岗位：{len(rows)}（北京时间，自首次发现起统计；重复检查不重复计数）。")
        print_update(make_summary(rows))
    return True


async def paste_material(label, first_line=None):
    print("粘贴 " + label + "，单独输入 .end 结束，.cancel 取消；不要粘贴密钥。")
    lines = [] if first_line is None else [first_line]
    while True:
        line = await read_line("… ")
        if line is None:
            raise EOFError("材料未以 .end 结束，未导入或分析。")
        if line.strip() == ".cancel":
            raise ValueError("已取消这份材料，未导入或分析。")
        if line.strip() == ".end":
            break
        lines.append(line)
        if sum(len(x.encode("utf-8")) + 1 for x in lines) > MAX_MATERIAL_BYTES:
            raise ValueError("材料超过 64 KB。")
    text = validate_text("\n".join(lines), label)
    if contains_credentials(text):
        raise ValueError("材料疑似含凭据，未导入或保存。")
    return text


async def interactive(runtime, jd=None, resume=None, source="用户粘贴", selected="all", updates=None):
    """引导式业务输入；仍复用 Platform 的异步终端、会话和 Agent 循环。"""
    print("直接粘贴材料，每份用单独一行 .end 结束；/help 查看原有命令，/quit 退出。")
    print("管理已有岗位无需简历：/jobs、/job 编号、/status 编号 状态、/note 编号 备注、/funnel。")
    if resume is None and selected != "analyze-jd":
        print("先提供个人背景或简历：学历/年级、专业、已完成项目、能使用的技能、正在学习或尚未掌握的技能。")
        print("不清楚的内容可写未知；不用填写身份证、联系方式或密钥。")
    else:
        print("现在粘贴岗位 JD，建议包含公司、岗位名称、职责和任职要求。")
    while True:
        needs_profile = resume is None and selected != "analyze-jd"
        line = await read_line("个人背景 > " if needs_profile else "岗位 JD > ")
        if line is None:
            break
        command, _, argument = line.strip().partition(" ")
        try:
            if contains_credentials(line):
                raise ValueError("输入疑似含凭据，已阻止。")
            if command == "/help":
                print(HELP)
            elif command == "/jd":
                jd, source = read_authorized_text(argument.strip())
                print("JD 已导入内存；/analyze-jd 可提取，/analyze 可完整分析。")
            elif command == "/resume":
                resume = load_resume(argument.strip().strip('"\''))[0]
                print("当前个人背景已导入；现在可粘贴 JD，或 /analyze 分析已导入的岗位。")
            elif command in ("/paste-jd", "/paste-resume"):
                text = await paste_material("JD" if command == "/paste-jd" else "简历/能力文本")
                if command == "/paste-jd":
                    jd, source = text, "用户粘贴"
                else:
                    resume = text
                print("材料已导入内存；/analyze 运行三个 Skill。")
            elif handle_update_command(runtime, command, argument, updates):
                pass
            elif handle_library_command(runtime, command, argument):
                pass
            elif command in ("/tailor", "/interview-prep"):
                if len(argument.split()) != 1:
                    raise ValueError(f"用法：{command} 岗位编号，例如 {command} 1。")
                if resume is None:
                    raise ValueError("请先用 --resume、/resume 路径 或 /paste-resume 提供当前真实简历。")
                data = await runtime.prepare_job(argument.strip(), resume,
                    "resume-tailor" if command == "/tailor" else "interview-prep")
                if data is not None:
                    print_preparation(data)
            elif command in ("/analyze", "/analyze-jd", "/match-resume", "/skill-gap-analysis"):
                workflow = "all" if command == "/analyze" else command[1:]
                data = await runtime.analyze(jd, resume, workflow, source)
                if data is not None:
                    print_report(data, workflow)
            elif command in ("/new", "/session", "/history", "/usage", "/quit", "/exit"):
                if not await handle_line(runtime, line):
                    break
            elif command.startswith("/"):
                print("未知命令；输入 /help 查看用法。")
            elif line.strip() == ".end":
                raise ValueError("材料不能为空；请先粘贴内容再输入 .end。")
            elif line.strip():
                text = await paste_material("个人背景" if needs_profile else "岗位 JD", first_line=line)
                if needs_profile:
                    resume = text
                    print("个人背景已载入，本次运行中复用；现在粘贴岗位 JD，.end 后自动分析。")
                else:
                    jd, source = text, "用户粘贴"
                    data = await runtime.analyze(jd, resume, selected, source)
                    if data is not None:
                        print_report(data, selected)
                    print("可继续粘贴下一份 JD，或 /jobs 查看记录，/quit 退出。")
        except EOFError as error:
            print(str(error))
            break
        except (ValueError, OSError) as error:
            print("输入未处理：" + str(error))


async def run(args):
    if args.live:
        raise ValueError("Job Agent 不使用旧 --live 开关；请用 --mode llm 启动模型分析，默认 --mode local 不联网。")
    if getattr(args, "watch", False) or getattr(args, "check_now", False):
        return await run_updates(args)
    if getattr(args, "daily_at", None):
        raise ValueError("--daily-at 需要 --watch；当前未启动定时任务。")
    from .search_cli import is_search, run_search, validate_search_args
    validate_search_args(args)
    if args.list_jobs and (args.jd or args.resume):
        raise ValueError("列岗位时只使用 --list-jobs，不同时导入材料。")
    mode = getattr(args, "mode", "local")
    dry_run = getattr(args, "llm_dry_run", False)
    if dry_run and mode != "llm":
        raise ValueError("--llm-dry-run 需要 --mode llm。")
    if mode == "local" and getattr(args, "model", None):
        raise ValueError("--model 只用于 --mode llm。")
    if mode == "llm" and not dry_run and not args.list_jobs and not getattr(args, "search_only", False) and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ValueError("llm 模式需要交互式 Terminal 确认；管道不可静默收费，可先用 --llm-dry-run。")
    jd, source = read_authorized_text(args.jd) if args.jd else (None, "用户粘贴")
    resume = load_resume(args.resume)[0] if args.resume else None
    if jd is not None and args.skill != "analyze-jd" and resume is None:
        raise ValueError("匹配与差距分析需要 --resume；只提取可加 --skill analyze-jd。")
    if dry_run:
        if jd is None:
            raise ValueError("LLM 预览需要 --jd；完整分析也需要 --resume。")
        analyzer = LLMAnalyzer(LLMConfig.from_env(getattr(args, "model", None), require_key=False))
        calls = {"all": 3, "analyze-jd": 1, "match-resume": 2, "skill-gap-analysis": 3}[args.skill]
        estimate = analyzer.estimate(jd, resume, calls)
        print_llm_scope(analyzer.config, estimate)
        print("只预览：没有模型请求、没有岗位/简历状态写入；实际可能命中 JD 缓存减少一轮。")
        return 0
    def emit(event):
        if event.kind in ("status", "tool", "error", "context", "usage"):
            print(f"[{event.kind}] {event.text}", flush=True)
    async def approve_llm(workspace, estimate, config):
        print_llm_scope(config, estimate)
        if workspace.preparation_plan is not None:
            payload = workspace.preparation_plan["payload"]
            print("本次目的：" + workspace.selected + f"；岗位 #{workspace.preparation_job['job_id']}。")
            print(f"发送范围：本岗位脱敏 JD 共 {len(payload['jd_segments'])} 段；当前简历的技能/项目事实共 {len(payload['resume_facts'])} 条及其原文、语境；面试准备另含十个主题与本地准备状态。")
            print("姓名/联系方式/教育/工作经历段在本地保留；不发送简历原文件、路径、项目源码、其他岗位或会话历史。项目/技能段内仍可能有间接个人信息，请核对下方完整待发材料。")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print(f"本 Skill 最多 {estimate['calls']} 次生成；仅已通过证据校验的草稿可按质量反馈修正。网络/证据失败不重试，发送后失败也可能计费。")
            return await read_line("确认上方具体材料、接收地址和费用，请输入“确认 LLM 分析”（其他输入不发送）：") == "确认 LLM 分析"
        print("本次 JD 来源：" + workspace.source_label + "；使用本次已导入的个人背景。")
        print("仅发送本次 JD、提取后的简历证据段及本次中间分析；不发送原文件、项目源码、路径、Key 或历史会话。")
        print("可以在 /paste-resume 中替换为脱敏背景。发送后的请求即使失败也可能计费。")
        return await read_line("确认这份材料与费用范围，请输入“确认 LLM 分析”（其他输入不发送）：") == "确认 LLM 分析"
    if is_search(args):
        return await run_search(args, resume, emit, approve_llm)
    runtime = JobRuntime(PROJECT, emit, demo=True, state_dir=args.state_dir, session_id=args.session,
                         mode="local" if args.list_jobs else mode, model=getattr(args, "model", None),
                         fallback=getattr(args, "fallback", "local"), approve_llm=approve_llm)
    try:
        print("Job Agent v6 | " + ("local · 本地规则 · 费用 0 元" if runtime.requested_mode == "local" else "llm · 确认后调用模型 · 失败回退策略：" + runtime.fallback))
        if resume is not None:
            print("简历已在本地载入，本次运行可复用；无需重复输入个人背景。")
        if args.list_jobs:
            print_jobs(runtime)
            return 0
        if jd is not None:
            data = await runtime.analyze(jd, resume, args.skill, source)
            if data is None:
                return 1
            print_report(data, args.skill)
            return 0
        await interactive(runtime, jd, resume, source, args.skill)
        return 0
    finally:
        await runtime.close()
        print("Job Agent 已退出；" + json.dumps(runtime.usage(), ensure_ascii=False))


async def run_updates(args):
    from .scheduler import DailyUpdates, validate_update_args
    validate_update_args(args)
    runtime = JobRuntime(PROJECT, demo=True, mode="local", state_dir=args.state_dir, session_id=args.session)
    updates = None
    try:
        updates = DailyUpdates(runtime, args)
        print("Job Agent v5 | 每日更新只用 local · 模型费用 0 元")
        if getattr(args, "watch", False):
            if getattr(args, "daily_at", None):
                updates.configure(args.daily_at)
            updates.start_watch()
            handle_update_command(runtime, "/last-check", "", updates)
            print("进程需保持运行；/check-now 手动检查，/new-jobs 查看今日新增，/quit 停止当前进程。")
        if getattr(args, "check_now", False):
            updates.start_check()
            finished = await updates.wait_current()
            if finished and finished["status"] == "failed":
                print("后台检查未完成：" + (finished["error"] or "未知错误") + "；本次没有自动重试。")
                if not getattr(args, "watch", False):
                    return 1
            if finished and (finished.get("result") or {}).get("skipped"):
                print(finished["result"]["reason"])
                if not getattr(args, "watch", False):
                    return 1
            if not getattr(args, "watch", False):
                last = updates.store.last_check()
                return 0 if last and last["status"] in ("completed", "partial") else 1
        resume_path = args.resume or (PROJECT / "job_agent/examples/resume.example.md")
        await interactive(runtime, resume=load_resume(resume_path)[0], updates=updates)
        return 0
    finally:
        if updates:
            await updates.close()
        await runtime.close()


def print_llm_scope(config, estimate):
    print(f"LLM 目标：{config.base_url}；模型：{config.model}。")
    print(f'预计最多 {estimate["calls"]} 次请求；保守输入估算 {estimate["estimated_input_tokens"]} token，最大输出 {estimate["max_output_tokens"]} token。')
    print(f'本进程累计保守费用约 {estimate["projected_rmb"]:.4f} 元；边界 {config.budget_rmb:g} 元/{config.max_requests} 次请求，无自动网络重试。')
    if estimate.get("policy"):
        print(f'本次每稿输出上限 {estimate["policy"]["max_output_tokens"]}；推理强度 {estimate["policy"]["reasoning_effort"] or "模型默认"}。修正仅使用已确认材料与通过校验的草稿。')
    print(f"价格按输入 ${config.input_price}/百万 token、输出 ${config.output_price}/百万 token，$1=10元估算；不是官方报价或实际账单保证。")


def print_preparation(data):
    print(f"\n岗位 #{data['job_id']}：{data['company'] or '公司未说明'} / {data['title'] or '岗位未说明'}")
    print(data["note"])
    if data["skill"] == "resume-tailor":
        print(data["tailored_resume"])
        print("修改前后对比和理由另存 report.md，正式简历见 tailored-resume.md。")
    else:
        for question in data["questions"]:
            print("\n问题：" + question["question"] + "\n准备状态：" + question["readiness"])
            print("参考答案（学习材料）：" + question["reference_answer"])
            print("学习／复盘建议：" + question["learning_advice"])
            for followup in question["follow_ups"]:
                print("追问：" + followup)
    for label, path in data["files"].items():
        print(label + "：" + path)
    from .preparation_files import usage_line
    print("原简历未覆盖，投递状态未修改；" + usage_line(data))


def main(args):
    try:
        return asyncio.run(run(args))
    except (ValueError, OSError) as error:
        print("Job Agent 启动失败：" + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Job Agent 已停止。")
        return 130
