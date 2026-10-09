"""macOS 部署适配；复用 v5，一次进程、固定 local，不安装即不常驻。"""

import argparse
import asyncio
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import stat
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
LABEL = "local.ai-agent-lab.job-agent"
FIELDS = {"version", "resume", "sources", "state_dir", "daily_at", "search_limit", "analyze_limit", "city_min"}


def ordinary(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("路径不能经过符号链接。")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("配置必须是独立普通文件。")
    return path


def private_directory(path, project=PROJECT):
    path = ordinary_directory(path)
    # 部署资料只能进入现有被 Git 忽略的本地状态树。
    if not path.is_relative_to(project / ".agent-platform") or path == project / ".agent-platform":
        raise ValueError("部署与状态目录必须位于项目 .agent-platform 的子目录。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def ordinary_directory(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or (path.exists() and not path.is_dir()):
        raise ValueError("目录不能经过链接或指向文件。")
    return path


def write_private(path, content, *, exclusive=False):
    path = ordinary(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content if isinstance(content, bytes) else content.encode("utf-8"))


def validate(config, project=PROJECT, *, materials=True):
    from .resume_loader import load_resume
    from .search import load_sources
    from .search_cli import read_json_file
    from .update_store import daily_time
    if not isinstance(config, dict) or set(config) != FIELDS or config["version"] != 1:
        raise ValueError("部署配置格式不正确；不接受自定义模型、命令或环境变量。")
    daily_time(config["daily_at"])
    for name, maximum in (("search_limit", 20), ("analyze_limit", 5), ("city_min", 20)):
        if type(config[name]) is not int or not 1 <= config[name] <= maximum:
            raise ValueError("候选/分析数量或城市阈值无效。")
    if materials:
        ordinary(config["resume"])
        load_resume(config["resume"])
        sources = load_sources(read_json_file(config["sources"]))
        if not any(s.get("search_city", "广州") in ("广州", "不限") for s in sources):
            raise ValueError("每日来源必须包含已启用的广州来源。")
    private_directory(config["state_dir"], project)
    return config


def read_config(path, project=PROJECT, *, materials=True):
    path = ordinary(path)
    if not path.is_relative_to(project / ".agent-platform"):
        raise ValueError("私人部署配置必须位于项目 .agent-platform 内。")
    if path.stat().st_mode & 0o077:
        raise ValueError("部署配置权限必须为600，避免泄露私人路径。")
    return validate(json.loads(path.read_text(encoding="utf-8")), project, materials=materials)


def launch_plist(config_path, project=PROJECT, python=None):
    return {"Label": LABEL,
            "ProgramArguments": [str(python or project / ".venv/bin/python"), "-B", "-m", "job_agent.macos_schedule", "run", "--config", str(config_path)],
            "WorkingDirectory": str(project), "RunAtLoad": True,
            "StartCalendarInterval": [{"Minute": minute} for minute in range(0, 60, 5)],
            "ProcessType": "Background", "Umask": 0o077,
            # 不能 KeepAlive：异常时不允许无限重启/网络请求。
            "StandardOutPath": "/dev/null", "StandardErrorPath": str(Path(config_path).parent / "startup.log")}


def prepare(config, output, project=PROJECT):
    config = validate(config, project)
    output = private_directory(output, project)
    python = project / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("请先建立项目 .venv；launchd 使用绝对解释器路径。")
    path = output / "config.json"
    # 重复 prepare 不覆盖旧配置或验收材料，更新用新输出目录。
    for name in ("config.json", LABEL + ".plist", "enable.sh", "disable.sh"):
        if (output / name).exists() or (output / name).is_symlink():
            raise ValueError("输出目录已有部署文件；请选择新目录，不覆盖原配置。")
    write_private(path, json.dumps(config, ensure_ascii=False, indent=2), exclusive=True)
    write_private(output / (LABEL + ".plist"), plistlib.dumps(launch_plist(path, project)), exclusive=True)
    write_private(output / "startup.log", "", exclusive=True)
    for action, token, flag in (("install", "ENABLE", "--confirm-install"), ("uninstall", "DISABLE", "--confirm-uninstall")):
        command = shlex.join([str(python), "-B", "-m", "job_agent.macos_schedule", action, "--config", str(path), flag])
        script = ("#!/bin/sh\nset -eu\ncd " + shlex.quote(str(project)) + "\n"
                  + "printf '%s' '将" + ("安装并启用" if action == "install" else "停用并移除") + "本用户每日任务；输入 " + token + " 确认：'\n"
                  + "read -r answer\n[ \"$answer\" = " + shlex.quote(token) + " ] || exit 1\nexec " + command + "\n")
        write_private(output / ("enable.sh" if action == "install" else "disable.sh"), script, exclusive=True)
    return path


@contextlib.contextmanager
def dispatch_lock(directory):
    path = ordinary(Path(directory) / "dispatch.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise ValueError("部署锁必须是独立普通文件。")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def checkpoint(directory, data):
    # 原子替换部署状态；不包含简历、密钥、错误原文或完整工具输出。
    path = Path(directory) / "dispatch.json"
    ordinary(path)
    temporary = Path(directory) / "dispatch.next.json"
    write_private(temporary, json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(temporary, path)
    log = ordinary(Path(directory) / "events.jsonl")
    if log.exists() and log.stat().st_size > 1_048_576:
        ordinary(Path(directory) / "events.previous.jsonl")
        os.replace(log, Path(directory) / "events.previous.jsonl")
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False) + "\n")


async def run_once(config, directory, project=PROJECT, *, clock=None, search=None):
    from argparse import Namespace
    from .runtime import JobRuntime
    from .scheduler import DailyUpdates, check_lock
    from .update_store import stamp, utc_clock
    clock = clock or utc_clock
    directory = private_directory(directory, project)
    with dispatch_lock(directory) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "another_dispatch"}
        old = directory / "dispatch.json"
        if old.exists() and json.loads(ordinary(old).read_text()).get("status") == "running":
            checkpoint(directory, {"status": "interrupted", "checked_at": stamp(clock()), "reason": "previous_process_stopped_no_auto_retry"})
        runtime = updates = None
        args = Namespace(**{**config, "mode": "local", "model": None, "llm_dry_run": False,
                            "search_demo": False, "search_fixture": None, "allow_network": True,
                            "search_only": False, "guangzhou_only": False, "session": "daily-system"})
        try:
            # 丢弃完整业务输出，避免简历证据进入系统日志。
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                runtime = JobRuntime(project, demo=True, mode="local", state_dir=config["state_dir"], session_id="daily-system")
                updates = DailyUpdates(runtime, args, clock=clock, **({"search": search} if search else {}))
                with check_lock(runtime.workspace.root) as free:
                    if not free:
                        return {"status": "skipped", "reason": "another_check"}
                    updates.configure(config["daily_at"])
                    if not updates.store.claim_due(clock()):
                        return {"status": "skipped", "reason": "not_due"}
                    checkpoint(directory, {"status": "running", "checked_at": stamp(clock()), "pid": os.getpid()})
                # 到期点已持久化到下一天；异常不得触发每5分钟的网络重试。
                updates.start_check("scheduled")
                result = await updates.wait_current()
                latest = updates.store.last_check()
                summary = (latest or {}).get("summary") or {}
                status = (latest or {}).get("status", "failed")
                if not result or result.get("error") or result.get("result", {}).get("skipped"):
                    status = "failed"
                data = {"status": status, "checked_at": stamp(clock()), "run_id": (latest or {}).get("run_id"),
                        "new_count": summary.get("new_count", 0), "updated_count": summary.get("updated_count", 0),
                        "analyzed_count": summary.get("analyzed_count", 0), "issue_count": len(summary.get("issues", [])),
                        "model_requests": 0, "estimated_rmb": 0}
                checkpoint(directory, data)
                return data
        except Exception as error:
            data = {"status": "failed", "checked_at": stamp(clock()), "error_type": type(error).__name__,
                    "reason": "查看岗位库最近检查；无自动重试，不记录原始错误或私人材料。"}
            checkpoint(directory, data)
            return data
        finally:
            if updates:
                await updates.close()
            if runtime:
                await runtime.close()


def install(path, *, confirmed=False, home=None, project=PROJECT):
    if not confirmed or sys.platform != "darwin":
        raise ValueError("仅macOS；安装需明确 --confirm-install 授权。")
    read_config(path, project)
    target_dir = ordinary_directory((home or Path.home()) / "Library/LaunchAgents")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = ordinary(target_dir / (LABEL + ".plist"))
    if target.exists():
        raise ValueError("已有同名任务；先核对并停用，不能覆盖。")
    source = ordinary(Path(path).parent / (LABEL + ".plist"))
    expected = launch_plist(Path(path), project)
    if plistlib.loads(source.read_bytes()) != expected:
        raise ValueError("plist 与已审核配置不一致，拒绝安装。")
    write_private(target, plistlib.dumps(expected), exclusive=True)
    subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
    return target


def uninstall(path, *, confirmed=False, home=None, project=PROJECT):
    if not confirmed or sys.platform != "darwin":
        raise ValueError("仅macOS；停用需明确 --confirm-uninstall 授权。")
    # 不依赖简历/来源仍存在：原材料搬走后也必须能停用。
    target = ordinary((home or Path.home()) / "Library/LaunchAgents" / (LABEL + ".plist"))
    if not target.exists():
        return
    if plistlib.loads(target.read_bytes()) != launch_plist(Path(path), project):
        raise ValueError("同名任务不属于此部署；拒绝停用或移除。")
    service = f"gui/{os.getuid()}/{LABEL}"
    loaded = subprocess.run(["/bin/launchctl", "print", service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if loaded.returncode == 0:
        subprocess.run(["/bin/launchctl", "bootout", service], check=True)
    target.unlink()  # 仅移除已经明确确认的本部署plist；岗位库和资料保留。


def main():
    parser = argparse.ArgumentParser(description="Job Agent macOS 每日 local 部署（默认不安装）")
    parser.add_argument("action", choices=("prepare", "run", "status", "install", "uninstall"))
    parser.add_argument("--config")
    parser.add_argument("--resume")
    parser.add_argument("--sources", default=str(PROJECT / "job_agent/sources/public-careers.json"))
    parser.add_argument("--state-dir", default=str(PROJECT / ".agent-platform/job-agent"))
    parser.add_argument("--output-dir", default=str(PROJECT / ".agent-platform/macos-daily"))
    parser.add_argument("--daily-at", default="09:00")
    parser.add_argument("--confirm-install", action="store_true")
    parser.add_argument("--confirm-uninstall", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            if not args.resume:
                raise ValueError("prepare需要明确指定本地 --resume。")
            config = {"version": 1, "resume": str(Path(args.resume).expanduser().absolute()),
                      "sources": str(Path(args.sources).expanduser().absolute()),
                      "state_dir": str(Path(args.state_dir).expanduser().absolute()), "daily_at": args.daily_at,
                      "search_limit": 8, "analyze_limit": 2, "city_min": 2}
            print("仅准备，未安装：" + str(prepare(config, args.output_dir)))
            return 0
        if not args.config:
            raise ValueError("需要 --config 指向已准备的私人配置。")
        path = ordinary(args.config)
        if args.action == "uninstall":
            uninstall(path, confirmed=args.confirm_uninstall)
            print("任务已停用；私人数据保留。")
            return 0
        config = read_config(path, materials=args.action != "status")
        if args.action == "install":
            print("已启用：" + str(install(path, confirmed=args.confirm_install)))
        elif args.action == "status":
            from .runtime import JobRuntime
            from .update_store import UpdateStore, next_daily, utc_clock
            async def status():
                runtime = JobRuntime(PROJECT, demo=True, mode="local", state_dir=config["state_dir"])
                try:
                    store = UpdateStore(runtime.workspace.memory)
                    data = {"schedule": store.schedule(), "desired_daily_at_beijing": config["daily_at"],
                            "next_if_enabled": next_daily(utc_clock(), config["daily_at"]).isoformat(),
                            "installed_plist": (Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")).exists(),
                            "last_check": store.last_check(), "new_jobs": store.today_jobs(utc_clock()),
                            "dispatch": json.loads((path.parent / "dispatch.json").read_text()) if (path.parent / "dispatch.json").exists() else None}
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                finally:
                    await runtime.close()
            asyncio.run(status())
        else:
            data = asyncio.run(run_once(config, path.parent))
            return 1 if data["status"] in ("failed", "partial", "interrupted") else 0
        return 0
    except Exception as error:
        # 配置/简历解析错误也可能含私人文本；日志仅输出类型。
        print("部署操作失败（" + type(error).__name__ + "）；检查路径、权限与配置，未自动重试。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
