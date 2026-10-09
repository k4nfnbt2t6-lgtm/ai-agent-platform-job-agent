"""自动化授权与额度：质量配置沿用 v2，许可与业务材料分开。"""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from job_agent.analysis import fingerprint


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def aware(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("授权有效期必须包含时区。")
    return result


@dataclass(frozen=True)
class Limits:
    daily_rmb: float = 0
    daily_requests: int = 0
    daily_applications: int = 2
    jobs_per_run: int = 2

    def __post_init__(self):
        if isinstance(self.daily_rmb, bool) or not math.isfinite(self.daily_rmb) or not 0 <= self.daily_rmb <= 100:
            raise ValueError("每日预算需要是 0-100 元的有限数值。")
        for name, maximum in (("daily_requests", 100), ("daily_applications", 50), ("jobs_per_run", 5)):
            value = getattr(self, name)
            if type(value) is not int or not (0 if name == "daily_requests" else 1) <= value <= maximum:
                raise ValueError(name + " 超出安全配置范围。")


@dataclass(frozen=True)
class LLMConsent:
    """只从本机操作者提供的配置加载，不能从 JD、模型输出生成授权。"""
    resume_hash: str
    base_url: str
    model: str
    expires_at: str
    limits_hash: str
    input_price: float
    output_price: float
    confirmed_data_transfer: bool = False
    confirmed_paid_calls: bool = False

    @classmethod
    def for_materials(cls, resume, config, limits, expires_at, **confirmations):
        return cls(fingerprint(resume), config.base_url, config.model, expires_at,
                   digest(asdict(limits)), config.input_price, config.output_price, **confirmations)

    def check(self, resume, config, limits, now=None):
        now = now or datetime.now(timezone.utc)
        if self.confirmed_data_transfer is not True or self.confirmed_paid_calls is not True:
            raise PermissionError("未确认简历/JD外发与自动付费，LLM 未启用。")
        scope = (self.resume_hash == fingerprint(resume) and self.base_url == config.base_url
                 and self.model == config.model and self.limits_hash == digest(asdict(limits))
                 and self.input_price == config.input_price and self.output_price == config.output_price)
        if not scope or aware(self.expires_at) <= now:
            raise PermissionError("LLM 授权过期或材料、模型、价格、额度发生变化。")
        if limits.daily_requests < 1 or limits.daily_rmb <= 0:
            raise PermissionError("自动 LLM 的每日预算和次数尚未配置。")
        parts = urlsplit(config.base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise PermissionError("LLM 地址必须是无凭据的 HTTPS 地址。")
