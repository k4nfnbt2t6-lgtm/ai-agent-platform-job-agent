"""现有结构化 LLM 的持久化计费适配，保留原来的证据检查与质量策略。"""

from job_agent.llm import LLMAnalyzer, LLMBudgetError, PROMPT
from .store import GuardError
from .policy import digest


class MeteredAnalyzer(LLMAnalyzer):
    def __init__(self, config, store, limits, consent, resume, *, client_factory=None, simulation=False):
        super().__init__(config, client_factory=client_factory)
        self.store, self.limits, self.consent, self.resume = store, limits, consent, resume
        self.plan_id = None
        self.simulation = simulation

    @property
    def cache_model(self):
        return self.config.model + ":automation:" + digest([self.config.base_url, self.simulation])

    def check_consent(self):
        self.consent.check(self.resume, self.config, self.limits, self.store.clock())
        self.store.check_running()

    async def approve(self, workspace, estimate, config):
        # 回调只验证本机显式授权，不从业务内容推断授权。
        self.consent.check(workspace.resume, config, self.limits, self.store.clock())
        self.check_consent()
        return self.plan_id is not None

    def start_plan(self, key, jd, resume, calls):
        self.consent.check(resume, self.config, self.limits, self.store.clock())
        self.check_consent()
        estimate = self.estimate(jd, resume, calls)
        self.preflight(estimate)
        self.plan_id = self.store.reserve_plan(key, estimate["projected_rmb"] - self.usage()["known_estimated_rmb"], calls, self.limits)

    async def ask(self, skill, payload, schema, *, prompt=PROMPT):
        try:
            self.check_consent()
            if self.plan_id is None:
                raise GuardError("缺少完整分析的持久化预算计划。")
            estimate = self.estimate_payload(skill, payload, schema, prompt=prompt)
            request = self.store.begin_request(self.plan_id, estimate["projected_rmb"] - self.usage()["known_estimated_rmb"], self.limits)
        except (GuardError, PermissionError) as error:
            raise LLMBudgetError(str(error)) from None
        before = self.usage()
        error_kind = None
        try:
            return await super().ask(skill, payload, schema, prompt=prompt)
        except BaseException as error:
            error_kind = type(error).__name__
            raise
        finally:
            after = self.usage()
            sent = self.requests > before["llm_requests"]
            known = not after["unknown_usage"]
            # 在发送前被原客户端阻止的请求费用为0；发送后的用量未知保留全额预留。
            cost = (after["known_estimated_rmb"] - before["known_estimated_rmb"]) if known or not sent else None
            self.store.settle_request(request, input_tokens=after["input_tokens"] - before["input_tokens"],
                output_tokens=after["output_tokens"] - before["output_tokens"], cost=cost, error_kind=error_kind)

    def finish(self, success):
        if self.plan_id:
            self.store.finish_plan(self.plan_id, success)
            self.plan_id = None
