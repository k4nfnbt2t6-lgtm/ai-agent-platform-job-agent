import subprocess


def run_terminal(command, timeout=10, cwd=None, env=None, input_text=None):
    """执行一条命令，返回执行状态、正常输出和错误输出。"""
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) for arg in command)
        or not command[0]
    ):
        raise ValueError('command 必须是非空的字符串列表，例如 ["pwd"]')

    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            cwd=cwd,
            env=env,
            input=input_text,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": f"命令超过 {timeout} 秒，已停止当前子进程。",
        }
    except OSError as error:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": f"命令无法启动：{type(error).__name__}：{error}",
        }


if __name__ == "__main__":
    result = run_terminal(["pwd"])
    print("返回码：", result["returncode"])
    print("正常输出：", result["stdout"])
    print("错误输出：", result["stderr"])
