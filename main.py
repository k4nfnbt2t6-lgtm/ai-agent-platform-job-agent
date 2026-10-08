"""统一入口：使用项目现有虚拟环境启动 AI Agent Platform v1.0。"""

import sys
from pathlib import Path


if __name__ == "__main__":
    try:
        from agent_platform.terminal_ui import main
    except ModuleNotFoundError as error:
        project = Path(__file__).resolve().parent
        print(f"缺少依赖 {error.name}。请使用项目现有解释器：\n"
              f'"{project / ".venv/bin/python"}" -B "{project / "main.py"}"', file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(main())
