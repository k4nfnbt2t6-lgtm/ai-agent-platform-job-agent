"""单项最终验收：真实SDK + 模拟HTTP，不访问API。"""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI

import accept_job_v6_api as first
import accept_job_v6_interview_final as acceptance
from job_agent.llm import LLMAnalyzer, LLMConfig, LLMError
from test_job_agent_llm import api_response
from test_job_agent_preparation_llm import interview_output


class FinalInterviewAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        shutil.copytree(first.PROJECT / 'job_agent/skills', self.root / 'job_agent/skills')
        shutil.copytree(first.PROJECT / 'job_agent/examples', self.root / 'job_agent/examples')
        self.directory = self.root / '.agent-platform/final-acceptance'
        first.safe_directory(self.directory)
        self.journal = acceptance.Journal(self.directory)
        self.jd, self.resume = first.artificial_materials(self.root)
        self.prepared = acceptance.prepared_materials(self.jd, self.resume)
        self.config = acceptance.acceptance_config(LLMConfig('offline-final-placeholder', first.RECORDED_ENDPOINT, first.MODEL, .30, 1.50), .30)
        self.requests = []
        self.failure = None
        for p in (patch.dict(os.environ, {'OPENAI_API_KEY': self.config.api_key, 'OPENAI_BASE_URL': self.config.base_url}),
                  patch.object(socket, 'getaddrinfo', side_effect=AssertionError('禁止真实网络')),
                  patch('job_agent.llm.AsyncOpenAI', side_effect=self.factory)):
            p.start()
            self.addCleanup(p.stop)

    def factory(self, **kwargs):
        self.assertEqual(kwargs['max_retries'], 0)
        self.assertEqual(kwargs['timeout'], 120)
        return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler), trust_env=False), **kwargs)

    def handler(self, request):
        # 计费尝试必须在网络发送之前落盘，崩溃后也不能再次执行。
        ledger = json.loads((self.directory / 'acceptance.json').read_text())
        self.assertEqual(len(ledger['attempts']), 1)
        self.assertEqual(ledger['budget_rmb'], .30)
        self.assertEqual(ledger['max_requests'], 1)
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertEqual(body['model'], 'gpt-6-sol')
        self.assertEqual(body['max_output_tokens'], 8192)
        self.assertTrue(body['text']['format']['strict'])
        self.assertFalse(body['store'])
        task = json.loads(body['input'][1]['content'])
        self.assertEqual(task['skill'], 'interview-prep')
        self.assertEqual(task['materials'], ledger['confirmed_materials'])
        if self.failure:
            return self.failure(request, task)
        return httpx2.Response(200, json=api_response(interview_output(task['materials'])))

    async def run_acceptance(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return await acceptance.execute(self.config, self.prepared, self.jd, self.resume, self.journal, self.root)

    async def test_one_skill_one_request_validates_saves_and_reports_usage(self):
        self.assertEqual(await self.run_acceptance(), 0)
        data = self.journal.data
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(data['real_api_requests'], 1)
        self.assertEqual(data['usage']['input_tokens'], 1000)
        self.assertEqual(data['usage']['output_tokens'], 200)
        self.assertAlmostEqual(data['usage']['estimated_rmb'], .006)
        self.assertIsNone(data['billed_rmb'])
        self.assertEqual(data['attempts'][0]['schema_validation'], '通过')
        self.assertEqual(data['results'][0]['evidence_validation'], '通过')
        self.assertTrue(data['results'][0]['original_materials_and_job_unchanged'])
        self.assertTrue(all(v is not False for v in data['results'][0]['quality_signals'].values()))
        self.assertIn('待逐题复核', data['quality_conclusion'])
        self.assertTrue((self.directory / 'interview-prep.schema-output.json').is_file())
        self.assertFalse((self.directory / 'resume-tailor.schema-output.json').exists())
        self.assertEqual(self.journal.path.stat().st_mode & 0o777, 0o600)

    def test_preview_is_free_and_does_not_need_credentials(self):
        buffer = io.StringIO()
        with patch.dict(os.environ, {'OPENAI_API_KEY': '', 'OPENAI_BASE_URL': ''}), contextlib.redirect_stdout(buffer):
            self.assertEqual(acceptance.main(['--budget-rmb', '0.30']), 0)
        self.assertIn('仅预览', buffer.getvalue())
        self.assertIn('0.212880', buffer.getvalue())
        self.assertNotIn('resume-tailor', buffer.getvalue())
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.requests, [])

    def test_missing_key_rejected_without_request(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), patch.object(acceptance, 'DIRECTORY', self.directory), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(acceptance.main(['--run', '--budget-rmb', '0.30']), 1)
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.requests, [])

    def test_confirmation_cancel_sends_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(acceptance, 'DIRECTORY', self.directory), \
             patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
             patch('builtins.input', return_value='取消'):
            self.assertEqual(acceptance.main(['--run', '--budget-rmb', '0.30']), 0)
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(self.requests, [])

    def test_noninteractive_run_cannot_charge(self):
        with patch.object(acceptance, 'DIRECTORY', self.directory), patch('sys.stdin.isatty', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(acceptance.main(['--run', '--budget-rmb', '0.30']), 1)
        self.assertEqual(self.requests, [])

    def test_confirmation_displays_scope_and_cost_before_single_mock_request(self):
        output = io.StringIO()
        def confirm(prompt):
            self.assertEqual(self.requests, [])
            self.assertIn('0.212880', output.getvalue())
            self.assertIn('resume_facts', output.getvalue())
            self.assertIn('1次/0.30元', prompt)
            return acceptance.CONFIRMATION
        with contextlib.redirect_stdout(output), patch.object(acceptance, 'DIRECTORY', self.directory), \
             patch.object(acceptance, 'PROJECT', self.root), \
             patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
             patch('builtins.input', side_effect=confirm):
            self.assertEqual(acceptance.main(['--run', '--budget-rmb', '0.30']), 0)
        self.assertEqual(len(self.requests), 1)

    def test_expensive_price_and_changed_endpoint_rejected(self):
        expensive = acceptance.acceptance_config(LLMConfig(self.config.api_key, self.config.base_url, first.MODEL, 1, 2))
        with self.assertRaisesRegex(LLMError, '0.20元'):
            acceptance.checked_estimate(LLMAnalyzer(expensive), self.prepared)
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://example.org/v1'}), \
             patch.object(acceptance, 'DIRECTORY', self.directory), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(acceptance.main(['--run', '--budget-rmb', '0.30']), 1)
        self.assertEqual(self.requests, [])

    async def test_other_skill_and_second_attempt_rejected(self):
        self.journal.start(self.config, self.prepared)
        backend = acceptance.FinalLLM(self.config, self.journal)
        value = self.prepared['interview-prep']
        with self.assertRaisesRegex(LLMError, '一次 interview-prep'):
            await backend.ask('resume-tailor', value['payload'], value['schema'])
        await backend.ask('interview-prep', value['payload'], value['schema'])
        with self.assertRaises(LLMError):
            await backend.ask('interview-prep', value['payload'], value['schema'])
        self.assertEqual(len(self.requests), 1)

    async def test_http_failure_not_retried_fee_unknown(self):
        self.failure = lambda request, task: httpx2.Response(429, json={'error': {'message': 'PRIVATE PROVIDER BODY', 'type': 'rate_limit_error'}})
        self.assertEqual(await self.run_acceptance(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.journal.data['usage']['estimated_rmb'])
        self.assertNotIn('PRIVATE PROVIDER BODY', json.dumps(self.journal.data))

    async def test_schema_failure_retains_known_usage(self):
        self.failure = lambda request, task: httpx2.Response(200, json=api_response({'invalid': True}))
        self.assertEqual(await self.run_acceptance(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertAlmostEqual(self.journal.data['usage']['estimated_rmb'], .006)
        self.assertNotEqual(self.journal.data['attempts'][0]['schema_validation'], '通过')

    async def test_evidence_failure_preserves_structured_response_for_diagnosis(self):
        def bad(request, task):
            value = interview_output(task['materials'])
            value['questions'][7]['resume_ids'] = ['F11']
            return httpx2.Response(200, json=api_response(value))
        self.failure = bad
        self.assertEqual(await self.run_acceptance(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.journal.data['attempts'][0]['schema_validation'], '通过')
        self.assertEqual(self.journal.data['results'][0]['evidence_validation'], '未通过/未完成')
        self.assertTrue((self.directory / 'interview-prep.schema-output.json').is_file())
        self.assertFalse((self.directory / 'runtime/preparations').exists())

    async def test_response_cost_above_budget_fails_without_retry(self):
        def over_budget(request, task):
            value = api_response(interview_output(task['materials']))
            value['usage'].update(input_tokens=90000, output_tokens=4000, total_tokens=94000)
            return httpx2.Response(200, json=value)
        self.failure = over_budget
        self.assertEqual(await self.run_acceptance(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertAlmostEqual(self.journal.data['usage']['estimated_rmb'], .33)
        self.assertIn('超过', self.journal.data['attempts'][0]['error'])

    async def test_repeat_run_displays_existing_record_only(self):
        await self.run_acceptance()
        with patch.object(acceptance, 'DIRECTORY', self.directory), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acceptance.main(['--run', '--budget-rmb', '0.30']), 0)
        self.assertEqual(len(self.requests), 1)

    def test_interrupted_record_prevents_resume_even_without_credentials(self):
        self.journal.start(self.config, self.prepared)
        self.journal.data['attempts'].append({'skill': 'interview-prep', 'status': 'in_flight'})
        self.journal.save()
        output = io.StringIO()
        with patch.object(acceptance, 'DIRECTORY', self.directory), patch.dict(os.environ, {'OPENAI_API_KEY': ''}), contextlib.redirect_stdout(output):
            self.assertEqual(acceptance.main(['--run', '--budget-rmb', '0.30']), 0)
        self.assertIn('未知', output.getvalue())
        self.assertEqual(self.requests, [])

    async def test_scope_prompt_and_fixture_changes_rejected(self):
        self.journal.start(self.config, self.prepared)
        backend = acceptance.FinalLLM(self.config, self.journal)
        value = self.prepared['interview-prep']
        with self.assertRaisesRegex(LLMError, '材料'):
            await backend.ask('interview-prep', {**value['payload'], 'extra': '真实资料'}, value['schema'])
        with self.assertRaisesRegex(LLMError, '提示'):
            await backend.ask('interview-prep', value['payload'], value['schema'], prompt='changed')
        (self.root / 'job_agent/examples/preparation-resume.example.md').write_text('其他个人资料')
        with self.assertRaisesRegex(ValueError, '拒绝替换'):
            first.artificial_materials(self.root)
        self.assertEqual(self.requests, [])

    async def test_schema_failure_preserves_original_model_output(self):
        value = api_response({'invalid': True})
        self.failure = lambda *_: httpx2.Response(200, json=value)
        self.assertEqual(await self.run_acceptance(), 1)
        path = Path(self.journal.data['attempts'][0]['diagnostics']['sdk_response'])
        saved = json.loads(path.read_text())
        raw = json.loads(saved['body'])
        self.assertEqual(raw['output'][0]['content'][0]['text'], value['output'][0]['content'][0]['text'])
        self.assertFalse(saved['credentials_redacted'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.requests), 1)

    async def test_reasoning_incompatibility_preserves_provider_error_and_never_retries(self):
        message = 'Unsupported parameter: reasoning.effort medium'
        self.failure = lambda *_: httpx2.Response(400, json={'error': {'message': message}})
        self.assertEqual(await self.run_acceptance(), 1)
        saved = json.loads(Path(self.journal.data['attempts'][0]['diagnostics']['http_error']).read_text())
        self.assertEqual(json.loads(saved['body'])['error']['message'], message)
        self.assertEqual(len(self.requests), 1)
        self.assertIn('HTTP 400', self.journal.data['attempts'][0]['error'])
        self.assertIsNone(self.journal.data['usage']['estimated_rmb'])
        with patch.object(acceptance, 'DIRECTORY', self.directory), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acceptance.main(['--run', '--budget-rmb', '0.30']), 0)
        self.assertEqual(len(self.requests), 1)

    async def test_diagnostics_never_store_reflected_api_key(self):
        self.failure = lambda *_: httpx2.Response(400, json={'error': {'message': self.config.api_key}})
        self.assertEqual(await self.run_acceptance(), 1)
        path = Path(self.journal.data['attempts'][0]['diagnostics']['http_error'])
        saved = json.loads(path.read_text())
        self.assertTrue(saved['credentials_redacted'])
        self.assertNotIn(self.config.api_key, path.read_text())
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__':
    unittest.main()
