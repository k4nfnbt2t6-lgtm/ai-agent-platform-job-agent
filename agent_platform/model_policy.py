"""运行参数与教学常量分离；配置不代表付费授权。"""
import os
import re

EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


def number(name, default, low, high, kind=int):
    import math
    try:
        value = kind(os.environ.get(name, str(default)))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError
        return value
    except (ValueError, OverflowError):
        raise ValueError(f"{name} 必须在 {low}–{high} 范围内。") from None


def effort(name, default):
    value = os.environ.get(name, default)
    if value not in EFFORTS:
        raise ValueError(f"{name} 推理配置无效。")
    return value


def platform_policy(task):
    complex_task = bool(re.search(r"分析|修复|测试|设计|汇总|report|debug|test", task, re.I))
    return {"max_output_tokens": number("PLATFORM_MAX_OUTPUT_TOKENS", 4096 if complex_task else 2048, 1024, 32768),
            "reasoning_effort": effort("PLATFORM_REASONING_EFFORT", "medium" if complex_task else "low")}
