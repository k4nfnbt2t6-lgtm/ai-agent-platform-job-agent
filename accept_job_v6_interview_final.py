"""仅 interview-prep 的最后一次人工资料验收；默认预览，终端确认后最多一请求。"""

from dataclasses import replace
import argparse
import asyncio
import json
import re
from pathlib import Path
import sys

from agent_platform.workspace import contains_credentials

import accept_job_v6_api as first
from job_agent.analysis import analyze_jd
from job_agent.llm import LLMAnalyzer, LLMConfig, LLMError
from job_agent.preparation_llm import PROMPT, plan, preflight_payload
from job_agent.runtime import JobRuntime

PROJECT = first.PROJECT
DIRECTORY = PROJECT / '.agent-platform/job-v6-interview-final-api-acceptance'
SKILL = 'interview-prep'
BUDGET = .20
REQUEST_LIMIT = 1
MODEL = first.MODEL
CONFIRMATION = '确认面试验收'


def acceptance_config(config, budget=BUDGET):
    if not .01 <= budget <= .35:
        raise LLMError('单次验收预算必须在0.01–0.35元；更大范围须另行设计确认。')
    return replace(config, preparation_passes=1, max_requests=1, budget_rmb=budget)


def prepared_materials(jd, resume):
    job = {'job_id': 1, 'company': '人工准备科技', 'title': 'AI Agent应用开发实习生',
           'city': '广州', 'jd': jd, 'source_url': None, 'detail_url': None}
    prepared = {SKILL: plan(job, resume, SKILL)}
    preflight_payload(prepared[SKILL])
    return prepared


def checked_estimate(backend, prepared):
    if set(prepared) != {SKILL}:
        raise LLMError('本轮仅允许 interview-prep。')
    if backend.config.input_price <= 0 or backend.config.output_price <= 0:
        raise LLMError('价格必须有效且大于0，未请求。')
    value = prepared[SKILL]
    estimate = backend.estimate_payload(SKILL, value['payload'], value['schema'], prompt=PROMPT)
    if estimate['projected_rmb'] > backend.config.budget_rmb or backend.unknown_usage:
        raise LLMError(f'本次保守预估 {estimate["projected_rmb"]:.6f} 元超过 {backend.config.budget_rmb:.2f}元或费用未知，未请求；请确认建议0.30元预算，不降低输出质量。')
    return estimate


def show_scope(config, prepared, estimate):
    print(f'仅 {SKILL}；接收端：{config.base_url}；模型：{MODEL}。')
    print(f'最多1次；本轮预算{config.budget_rmb:.2f}元；保守预估 {estimate["projected_rmb"]:.6f}元。')
    print(f'保守输入 {estimate["estimated_input_tokens"]} token；最多输出 {estimate["max_output_tokens"]} token。')
    policy = config.policy(SKILL, prepared[SKILL]['payload'])
    print(f'推理强度：{policy["reasoning_effort"] or "模型默认"}；本轮明确只生成一稿，不进行质量修正。')
    print(f'项目配置价格：输入${config.input_price}/百万、输出${config.output_price}/百万，$1=10元。')
    print('这是中转站配置价格；真实扣费以账单为准，客户端不能保证服务商计费硬上限。')
    print('以下为完整待发送材料：人工JD、人工技能/项目事实、十个主题及学习状态。')
    print(json.dumps(prepared[SKILL]['payload'], ensure_ascii=False, indent=2))
    print('不发送真实简历、原文件、姓名/教育/工作经历段、Key、路径、源码或历史会话。')
    print('沿用现有 interview-prep 提示与JSON schema；store=False，120秒超时，无自动重试。')


class Journal(first.Journal):
    def start(self, config, prepared):
        if self.path.exists():
            raise ValueError('已有本轮记录，不允许重置收费次数。')
        self.data = {'status': 'running', 'started_at': first.now(), 'model': MODEL,
                     'base_url': config.base_url, 'budget_rmb': config.budget_rmb, 'max_requests': REQUEST_LIMIT,
                     'real_api_requests': 0, 'skills': [SKILL],
                     'pricing': {'input_usd_per_million': config.input_price,
                                 'output_usd_per_million': config.output_price, 'usd_to_rmb': 10},
                     'material_hashes': {SKILL: first.scope_hash(prepared[SKILL])},
                     'prompt_sha256': first.digest(PROMPT), 'request_policy': config.policy(SKILL, prepared[SKILL]['payload']),
                     'confirmed_materials': prepared[SKILL]['payload'],
                     'attempts': [], 'results': [], 'usage': None, 'billed_rmb': None,
                     'quality_conclusion': '未验收；JD相关性、技术正确性及生成质量待逐题复核'}
        self.save()


class FinalLLM(first.AcceptanceLLM):
    """继承原验收的原子记录和schema响应保存，只收紧为一请求/终端明确所选预算。"""
    def record_api_result(self, kind, content):
        # 仅独立人工资料验收启用；不保存请求头或密钥，正文不打印到终端。
        original_hash = first.digest(content)
        cleaned = content.replace(self.config.api_key, "[已隐藏]") if self.config.api_key else content
        cleaned = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[已隐藏]", cleaned)
        redacted = cleaned != content
        if contains_credentials(cleaned):
            cleaned, redacted = "响应疑含其他凭据，正文不保存；保留原响应摘要用于核对。", True
        artifact = self.journal.directory / f"interview-prep.{kind}.json"
        first.write_private(artifact, json.dumps({"kind": kind, "recorded_at": first.now(),
                            "body": cleaned, "sha256_before_redaction": original_hash,
                            "credentials_redacted": redacted}, ensure_ascii=False, indent=2) + "\n")
        self.journal.data['attempts'][-1].setdefault('diagnostics', {})[kind] = str(artifact)
        self.journal.save()

    def preflight(self, estimate):
        super().preflight(estimate)
        if (self.unknown_usage or self.requests + estimate['calls'] > REQUEST_LIMIT
                or estimate['projected_rmb'] > self.config.budget_rmb):
            raise LLMError('达到本轮一次请求/所选预算边界或费用未知，停止且不重试。')

    async def ask(self, skill, payload, schema, *, prompt=PROMPT):
        if skill != SKILL or self.journal.data['attempts']:
            raise LLMError('本轮仅允许一次 interview-prep；禁止其他Skill或重复请求。')
        if first.digest(prompt) != self.journal.data['prompt_sha256']:
            raise LLMError('确认后提示发生变化，未请求。')
        output = await super().ask(skill, payload, schema, prompt=prompt)
        if self.usage()['known_estimated_rmb'] > self.config.budget_rmb:
            self.failed = True
            self.last_error = '返回用量按配置估算超过所选预算，已停止；请核对账单，不重试。'
            self.journal.data['attempts'][-1].update(status='failed', error=self.last_error)
            self.journal.save()
            raise LLMError(self.last_error)
        return output


def quality_signals(data, prepared):
    signals = first.quality_signals(SKILL, data)
    questions = data['questions']
    expected = {topic['id']: topic['status'] for topic in prepared[SKILL]['payload']['topics']}
    signals.update(
        explanation_quality_signals_clear=not data.get("quality_needs_review", True),
        readiness_preserved=all(q['status'] == expected[f'T{q["id"]}'] for q in questions),
        sql_not_promoted=all(q['status'] != 'matched' for q in questions if q.get('skill') == 'SQL'),
        ai_application_not_promoted=all(q['status'] != 'matched' for q in questions if q.get('skill') == 'AI 应用开发'),
        rag_learning_preserved=all(q['status'] == 'learning' for q in questions if q.get('skill') == 'RAG'),
        question_kinds_present={q['question_kind'] for q in questions}
            == {'jd_technical', 'project_followup', 'learning'})
    return signals


def summary_markdown(data):
    attempt = next(iter(data['attempts']), {})
    result = next(iter(data['results']), {})
    usage = data.get('usage') or {}
    fee = usage.get('estimated_rmb')
    fee_text = '未知，请核对服务商账单' if data['attempts'] and fee is None else f'{fee or 0:.6f}元'
    lines = ['# Job Agent v6 interview-prep 最后一次真实验收',
             f'运行状态：{data["status"]}；最多1次，本轮预算{data["budget_rmb"]:.2f}元。',
             f'请求数：{data["real_api_requests"]}；schema：{attempt.get("schema_validation", "未检查")}；'
             f'证据检查：{result.get("evidence_validation", "未完成")}。',
             f'输入 token：{attempt.get("input_tokens", "未知")}；输出 token：{attempt.get("output_tokens", "未知")}。',
             f'按配置估算费用：{fee_text}。实际扣费：未取得账单，需本人核对。',
             '生成质量结论：' + data['quality_conclusion']]
    if result.get('reason') or attempt.get('error') or data.get('error'):
        lines.append('停止原因：' + (result.get('reason') or attempt.get('error') or data['error']))
    if attempt.get('diagnostics'):
        lines.append('原始响应/错误诊断文件（仅本地，疑似凭据会隐藏）：' + json.dumps(attempt['diagnostics'], ensure_ascii=False))
    if result.get('quality_signals'):
        lines.append('客观信号：' + json.dumps(result['quality_signals'], ensure_ascii=False))
    if result.get('files'):
        lines += ['产物：'] + [f'- {name}：{path}' for name, path in result['files'].items()]
    lines.append('重复运行只查看现有记录；不继续、不重试、不调用 resume-tailor。')
    return '\n\n'.join(lines) + '\n'


async def execute(config, prepared, jd, resume, journal, project=PROJECT):
    # main 已在交互终端确认完整材料、价格、一次请求范围；此处不扩大确认范围。
    checked_estimate(LLMAnalyzer(config), prepared)
    journal.start(config, prepared)
    backend = FinalLLM(config, journal)
    async def approved(workspace, estimate, actual_config):
        if (workspace.selected != SKILL or actual_config != config
                or first.scope_hash(workspace.preparation_plan) != journal.data['material_hashes'][SKILL]):
            raise ValueError('确认后材料或接收端发生变化，未请求。')
        backend.preflight(estimate)
        print(f'即将请求 interview-prep：保守预估 {estimate["projected_rmb"]:.6f}元；最多1次，不重试。', flush=True)
        return True

    runtime = None
    try:
        runtime = JobRuntime(project, demo=True, mode='llm', model=MODEL, fallback='none', approve_llm=approved,
                             state_dir=journal.directory / 'runtime')
        runtime.llm = backend
        record = runtime.workspace.memory.remember(jd, analyze_jd(jd), '固定人工验收资料')
        identity = runtime.workspace.memory.library.linked('jd', record['jd_id'])
        before_job = runtime.workspace.memory.library.get(identity)
        fixture_before = first.artificial_materials(project)
        data = await runtime.prepare_job(identity, resume, SKILL)
        if data is None:
            journal.data.update(status='failed', results=[{
                'skill': SKILL, 'evidence_validation': '未通过/未完成',
                'reason': backend.last_error or '工作流未完成，未把模型文字当作成功。'}])
        else:
            signals = quality_signals(data, prepared)
            valid = data['mode'] == 'llm' and data['model_requests'] == 1 and data.get('evidence_checked')
            unchanged = (before_job == runtime.workspace.memory.library.get(identity)
                         and fixture_before == first.artificial_materials(project))
            journal.data['results'] = [{'skill': SKILL, 'evidence_validation': '通过' if valid else '未通过',
                                       'files': data['files'], 'quality_signals': signals,
                                       'original_materials_and_job_unchanged': unchanged}]
            passed = valid and unchanged and not any(value is False for value in signals.values())
            journal.data['status'] = 'automated_checks_passed' if passed else 'failed'
            if passed:
                journal.data['quality_conclusion'] = 'schema、证据规则、学习状态与客观信号通过；JD相关性和生成质量待逐题复核'
    except (ValueError, OSError):
        journal.data.update(status='failed', error='验收中断或材料/文件检查失败，停止且不重试。')
    finally:
        if runtime:
            await runtime.close()
        journal.data.update(finished_at=first.now(), usage=backend.usage(), real_api_requests=backend.requests)
        journal.save()
        first.write_private(journal.directory / 'summary.md', summary_markdown(journal.data))
    return 0 if journal.data['status'] == 'automated_checks_passed' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='仅interview-prep人工资料验收，默认预览；最多1次/0.20元，必须终端确认。')
    parser.add_argument('--run', action='store_true', help='展示完整材料与费用，经交互确认后请求；已有记录只查看')
    parser.add_argument('--budget-rmb', type=float, default=BUDGET,
                        help='明确选择本次预算；默认保留0.20元，质量配置建议0.30元，仍须终端确认')
    args = parser.parse_args(argv)
    try:
        if not args.run:
            jd, resume = first.artificial_materials(PROJECT)
            prepared = prepared_materials(jd, resume)
            try:
                config = acceptance_config(LLMConfig.from_env(MODEL, require_key=False), args.budget_rmb)
            except LLMError:
                config = acceptance_config(LLMConfig('', first.RECORDED_ENDPOINT, MODEL, .30, 1.50), args.budget_rmb)
                print('当前环境配置不可用；仅按项目记录的接收端与价格预览，不自动配置。')
            print('仅预览，不请求API，不创建收费记录。')
            show_scope(config, prepared, LLMAnalyzer(config).estimate_payload(SKILL, prepared[SKILL]['payload'], prepared[SKILL]['schema'], prompt=PROMPT))
            checked_estimate(LLMAnalyzer(config), prepared)
            return 0
        with first.exclusive(DIRECTORY):
            journal = Journal(DIRECTORY)
            if journal.read() is not None:
                print(summary_markdown(journal.data))
                print('已有记录，不再次调用API。记录：' + str(journal.path))
                return 0
            jd, resume = first.artificial_materials(PROJECT)
            prepared = prepared_materials(jd, resume)
            config = acceptance_config(LLMConfig.from_env(MODEL), args.budget_rmb)
            if config.base_url != first.RECORDED_ENDPOINT:
                raise LLMError('接收端与项目记录的中转站不同，未请求；请先核对并确认新的目标。')
            show_scope(config, prepared, LLMAnalyzer(config).estimate_payload(SKILL, prepared[SKILL]['payload'], prepared[SKILL]['schema'], prompt=PROMPT))
            checked_estimate(LLMAnalyzer(config), prepared)
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise LLMError('必须在Mac Terminal交互确认；禁止通过管道静默收费。')
            if input(f'确认上述材料、接收端和1次/{config.budget_rmb:.2f}元预算，输入“{CONFIRMATION}”（其他输入取消）：').strip() != CONFIRMATION:
                print('已取消，未请求API。')
                return 0
            code = asyncio.run(execute(config, prepared, jd, resume, journal, project=PROJECT))
            print(summary_markdown(journal.data))
            print('结果目录：' + str(DIRECTORY))
            return code
    except LLMError as error:
        print('验收未完成：' + str(error), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print('验收未完成：人工资料、配置或状态目录检查失败。请检查本地环境，不要将Key发送到聊天。', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('验收已停止；已发送请求可能计费，不会自动重试。')
        return 130
    except Exception:
        print('验收异常停止，不输出可能含凭据的异常正文；检查记录，不重新收费。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
