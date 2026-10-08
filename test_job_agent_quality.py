"""质量参数、无损上下文、有界草稿修正和预算授权；全程离线。"""
from copy import deepcopy
from dataclasses import replace
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx2

from agent import LoopRecorder, MAX_OUTPUT_TOKENS
from agent_platform.model_policy import platform_policy
from agent_platform.runtime import ContextSession, PlatformRuntime
from job_agent.llm import LLMAnalyzer, LLMConfig, LLMError
from job_agent.preparation_llm import PROMPT, plan
from test_job_agent_llm import api_response
import test_job_agent_preparation_llm as preparation_tests
from test_job_agent_preparation_llm import RESUME, JOB, interview_output
import accept_job_v6_interview_final as final


class QualityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = LLMConfig('', 'https://example.org/v1', 'gpt-6-sol', .3, 1.5)

    def test_profiles_use_task_depth_and_preserve_demo_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(platform_policy('37+58')['max_output_tokens'], 2048)
            self.assertEqual(platform_policy('分析失败的测试')['max_output_tokens'], 4096)
            self.assertEqual(PlatformRuntime.model_settings('分析项目').reasoning.effort, 'medium')
        self.assertEqual(MAX_OUTPUT_TOKENS, 1024)
        self.assertEqual(LoopRecorder().max_output_tokens, 1024)
        self.assertEqual(self.config.policy('analyze-jd')['reasoning_effort'], 'low')
        self.assertEqual(self.config.policy('interview-prep')['max_output_tokens'], 8192)
        self.assertEqual(self.config.policy('interview-prep')['reasoning_effort'], 'medium')

    def test_explicit_overrides_and_invalid_values(self):
        values = {'OPENAI_BASE_URL': 'https://example.org/v1', 'JOB_AGENT_MAX_OUTPUT_TOKENS': '12288',
                  'JOB_AGENT_REASONING_EFFORT': 'high', 'JOB_AGENT_PREPARATION_PASSES': '3',
                  'JOB_AGENT_BUDGET_RMB': '3', 'JOB_AGENT_MAX_REQUESTS': '12', 'JOB_AGENT_TIMEOUT_SECONDS': '240'}
        with patch.dict(os.environ, values, clear=True):
            config = LLMConfig.from_env(require_key=False)
            self.assertEqual(config.policy('interview-prep'), {'max_output_tokens': 12288, 'reasoning_effort': 'high'})
            self.assertEqual((config.preparation_passes, config.budget_rmb, config.max_requests, config.timeout_seconds), (3, 3, 12, 240))
            for name, bad in [('JOB_AGENT_MAX_OUTPUT_TOKENS', '0'), ('JOB_AGENT_PREPARATION_PASSES', '99'),
                              ('JOB_AGENT_REASONING_EFFORT', 'invalid'), ('JOB_AGENT_BUDGET_RMB', 'nan')]:
                with patch.dict(os.environ, {name: bad}), self.assertRaises(LLMError):
                    LLMConfig.from_env(require_key=False)

    def test_large_materials_expand_without_discarding_evidence(self):
        small = self.config.policy('interview-prep', {'facts': 'a'})
        large = self.config.policy('interview-prep', {'facts': '证据'*15000})
        self.assertEqual(large['max_output_tokens'], small['max_output_tokens']*2)
        self.assertEqual(self.config.policy('interview-prep', {'facts': 'a', 'validated_draft': '长草稿'*20000}), small)
        self.assertIsNone(replace(self.config, model='custom').policy('interview-prep')['reasoning_effort'])

    def test_estimate_covers_all_approved_passes_and_draft_input(self):
        prepared = plan(JOB, RESUME, 'interview-prep')
        backend = LLMAnalyzer(self.config)
        one = backend.estimate_payload('interview-prep', prepared['payload'], prepared['schema'], prompt=PROMPT)
        total = backend.estimate_preparation(prepared, PROMPT)
        self.assertEqual(total['calls'], 2)
        self.assertEqual(total['max_output_tokens'], one['max_output_tokens']*2)
        self.assertGreater(total['estimated_input_tokens'], one['estimated_input_tokens']*2)
        self.assertAlmostEqual(total['projected_rmb'], .843552)

    def test_final_acceptance_keeps_one_request_and_requires_new_budget(self):
        prepared = final.prepared_materials(JOB['jd'], RESUME)
        old = final.acceptance_config(self.config)
        with self.assertRaisesRegex(LLMError, '0.30元预算'):
            final.checked_estimate(LLMAnalyzer(old), prepared)
        adequate = final.acceptance_config(self.config, .3)
        estimate = final.checked_estimate(LLMAnalyzer(adequate), prepared)
        self.assertEqual((estimate['calls'], adequate.preparation_passes, adequate.max_requests), (1, 1, 1))
        self.assertEqual(estimate['max_output_tokens'], 8192)
        self.assertAlmostEqual(estimate['projected_rmb'], .21288)

    def test_platform_cost_reserves_current_output_cap(self):
        recorder = LoopRecorder(max_output_tokens=8192)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            recorder.check_projected_cost(1, 0)
        self.assertIn('0.2129', out.getvalue())


class ContextQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_old_messages_and_tool_evidence_survive(self):
        fact = '前文'*120 + '尚未熟练MCP，不能作为已经掌握的证据'
        items = [{'role': 'user', 'content': fact},
                 {'type': 'function_call', 'call_id': 'old', 'name': 'read_file', 'arguments': '{}'},
                 {'type': 'function_call_output', 'call_id': 'old', 'output': '完整的原项目测试证据'}]
        items += [{'role': 'user', 'content': f'任务{n}'} for n in range(8)]
        storage = AsyncMock()
        storage.get_items.return_value = deepcopy(items)
        session = ContextSession(storage, lambda *_: None)
        self.assertEqual(await session.get_items(), items)
        self.assertIn('尚未熟练MCP', (await session.get_items())[0]['content'])


class QualityGenerationTests(unittest.IsolatedAsyncioTestCase):
    # 复用已有真实SDK+MockTransport的隔离环境，不继承或重复执行整套测试。
    setUp = preparation_tests.PreparationLLMRuntimeTests.setUp
    asyncTearDown = preparation_tests.PreparationLLMRuntimeTests.asyncTearDown
    factory = preparation_tests.PreparationLLMRuntimeTests.factory
    runtime = preparation_tests.PreparationLLMRuntimeTests.runtime

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertEqual(body['max_output_tokens'], 8192)
        self.assertEqual(body['reasoning']['effort'], 'medium')
        self.assertFalse(body['store'])
        task = json.loads(body['input'][1]['content'])
        if self.failure:
            return self.failure(request, task)
        value = interview_output(task['materials'])
        if len(self.requests) == 1:
            for index, q in enumerate(value['questions']):
                q['reference_answer'] = f'第{index}题可以先校验输入，失败时保留错误信息，避免把尚未完成的操作当作成功。'
        else:
            self.assertIn('validated_draft', task['materials'])
            self.assertIn('quality_feedback', task['materials'])
        return httpx2.Response(200, json=api_response(value))

    async def test_quality_repair_uses_approved_materials_and_validates_both_drafts(self):
        runtime, identity = self.runtime(fallback='none')
        scopes = []
        async def approved(workspace, estimate, config):
            self.assertEqual(estimate['calls'], 2)
            scopes.append(deepcopy(workspace.preparation_plan['payload']))
            return True
        runtime.approve_llm = approved
        data = await runtime.prepare_job(identity, RESUME, 'interview-prep')
        self.assertEqual(data['model_requests'], 2)
        self.assertEqual(data['generation_passes'], 2)
        self.assertTrue(data['evidence_checked'])
        self.assertFalse(data['quality_needs_review'])
        self.assertTrue(data['quality_review'][0]['issues'])
        self.assertEqual(data['quality_review'][1]['issues'], [])
        second = json.loads(self.requests[1]['input'][1]['content'])['materials']
        for key, value in scopes[0].items():
            self.assertEqual(second[key], value)
        self.assertEqual(data['questions'][0]['status'], 'learning')

    async def test_explicit_single_pass_reports_quality_gap_without_second_charge(self):
        runtime, identity = self.runtime(fallback='none')
        runtime.llm.config = replace(runtime.llm.config, preparation_passes=1)
        data = await runtime.prepare_job(identity, RESUME, 'interview-prep')
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(data['quality_needs_review'])
        self.assertIn('待复核', Path(data['files']['report.md']).read_text())

    async def test_budget_shortfall_does_not_silently_degrade_to_local(self):
        runtime, identity = self.runtime()
        runtime.llm.config = replace(runtime.llm.config, budget_rmb=.2)
        self.assertIsNone(await runtime.prepare_job(identity, RESUME, 'interview-prep'))
        self.assertEqual(self.requests, [])
        runtime.approve_llm.assert_not_called()
        self.assertFalse((runtime.workspace.root/'preparations').exists())

    async def test_network_failure_has_clear_saved_quality_degradation(self):
        self.failure = lambda *_: httpx2.Response(503, json={'error': {'message': 'mock error'}})
        runtime, identity = self.runtime()
        data = await runtime.prepare_job(identity, RESUME, 'interview-prep')
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(data['quality_degraded'])
        self.assertFalse(data['semantic_complete'])
        self.assertIn('质量降级', Path(data['files']['report.md']).read_text())
        self.assertTrue(any('质量降级' in e.text for e in self.events))

    async def test_evidence_failure_stops_before_quality_repair(self):
        def wrong(request, task):
            value = interview_output(task['materials'])
            value['questions'][0]['resume_ids'] = ['F9999']
            return httpx2.Response(200, json=api_response(value))
        self.failure = wrong
        runtime, identity = self.runtime(fallback='none')
        self.assertIsNone(await runtime.prepare_job(identity, RESUME, 'interview-prep'))
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((runtime.workspace.root/'preparations').exists())

    async def test_config_change_after_consent_is_not_authorized(self):
        runtime, identity = self.runtime()
        async def approved(*_):
            runtime.llm.config = replace(runtime.llm.config, preparation_passes=3)
            return True
        runtime.approve_llm = approved
        with self.assertRaisesRegex(ValueError, '重新确认'):
            await runtime.prepare_job(identity, RESUME, 'interview-prep')
        self.assertEqual(self.requests, [])

    async def test_quality_still_insufficient_stops_at_approved_two_passes(self):
        def short(request, task):
            value = interview_output(task['materials'])
            for index, q in enumerate(value['questions']):
                q['reference_answer'] = f'第{index}题应当先检查参数有效性，失败时保留可解释错误，再检查结果是否与预期一致。'
            return httpx2.Response(200, json=api_response(value))
        self.failure = short
        runtime, identity = self.runtime(fallback='none')
        data = await runtime.prepare_job(identity, RESUME, 'interview-prep')
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(data['quality_needs_review'])
        self.assertEqual(len(data['quality_review']), 2)
        self.assertIn('待复核', Path(data['files']['report.md']).read_text())

    async def test_repair_with_bad_evidence_is_rejected_not_saved(self):
        def bad_second(request, task):
            value = interview_output(task['materials'])
            if len(self.requests) == 1:
                for index, q in enumerate(value['questions']):
                    q['reference_answer'] = f'第{index}题应当先检查参数有效性，失败时保留可解释错误，再检查结果是否与预期一致。'
            else:
                value['questions'][0]['resume_ids'] = ['F9999']
            return httpx2.Response(200, json=api_response(value))
        self.failure = bad_second
        runtime, identity = self.runtime(fallback='none')
        self.assertIsNone(await runtime.prepare_job(identity, RESUME, 'interview-prep'))
        self.assertEqual(len(self.requests), 2)
        self.assertFalse((runtime.workspace.root/'preparations').exists())

    async def test_unexpected_usage_blocks_repair_without_local_downgrade(self):
        def expensive_draft(request, task):
            value = interview_output(task['materials'])
            for index, q in enumerate(value['questions']):
                q['reference_answer'] = f'第{index}题应当先检查参数有效性，失败时保留可解释错误，再检查结果是否与预期一致。'
            response = api_response(value)
            response['usage'].update(input_tokens=280000, total_tokens=280200)
            return httpx2.Response(200, json=response)
        self.failure = expensive_draft
        runtime, identity = self.runtime()  # 即使允许fallback，也不能因预算不足降级。
        self.assertIsNone(await runtime.prepare_job(identity, RESUME, 'interview-prep'))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(runtime.llm.last_error_kind, 'budget')
        self.assertFalse((runtime.workspace.root/'preparations').exists())
