"""公开回归使用合成 JSON；本机另行重放未纳入 Git 的真实响应，禁止网络。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from accept_job_v6_api import artificial_materials, prepared_materials
from job_agent.llm import LLMError
from job_agent.preparation_files import markdown
from job_agent.preparation_llm import checked_interview
from job_agent.preparation_questions import preparation_evidence
from job_agent.preparation_schemas import InterviewOutput
from test_job_agent_preparation_llm import JOB, interview_output
from job_agent.preparation_llm import plan

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'tests/fixtures/job_agent/interview.synthetic.json'
LOCAL_RESPONSE = ROOT / 'tests/fixtures/job_agent/interview-first-api.json'


class InterviewEvidenceRegressionTests(unittest.TestCase):
    def setUp(self):
        deny = patch.object(socket, 'getaddrinfo', side_effect=AssertionError('离线回归禁止网络'))
        deny.start()
        self.addCleanup(deny.stop)
        self.fixture = json.loads(FIXTURE.read_text())
        self.jd, self.resume = artificial_materials()
        self.prepared = prepared_materials(self.jd, self.resume)['interview-prep']

    def check(self, value, prepared=None):
        return checked_interview(InterviewOutput.model_validate(value), prepared or self.prepared)

    def test_synthetic_payload_hash_and_exact_two_false_positives(self):
        payload = self.fixture['original_payload']
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, self.fixture['original_payload_sha256'])
        topics = {t['id']: t for t in payload['topics']}
        mismatches = {q['topic_id']: set(q['resume_ids']) - set(topics[q['topic_id']]['resume_ids'])
                      for q in self.fixture['response']['questions']}
        self.assertEqual({key: ids for key, ids in mismatches.items() if ids},
                         {'T2': {'F8', 'F9'}, 'T5': {'F8'}})
        old = {**self.prepared, 'payload': payload}
        with self.assertRaisesRegex(LLMError, r'T2.*F8, F9'):
            self.check(self.fixture['response'], old)
        value = deepcopy(self.fixture['response'])
        value['questions'][1]['resume_ids'] = ['F7']
        with self.assertRaisesRegex(LLMError, '仅以项目名称'):
            self.check(value, old)
        value['questions'][1]['resume_ids'] = []
        with self.assertRaisesRegex(LLMError, r'T5.*F8'):
            self.check(value, old)

    def test_saved_actual_response_passes_without_changing_its_citations(self):
        if not LOCAL_RESPONSE.is_file():
            self.skipTest('真实 API 响应仅保留本地；公开副本的其余证据回归使用合成数据。')
        value = json.loads(LOCAL_RESPONSE.read_text())['response']
        original = deepcopy(value)
        data = self.check(value)
        self.assertEqual(value, original)
        self.assertEqual(len(data['questions']), 10)
        self.assertTrue(data['questions'][1]['resume_evidence'])
        self.assertIn('异常处理', data['questions'][4]['resume_evidence'][0])
        self.assertEqual(data['questions'][5]['status'], 'needs_confirmation')
        self.assertEqual(data['questions'][6]['status'], 'learning')

    def test_related_implementation_evidence_is_explicitly_offered(self):
        topics = {t['id']: t for t in self.prepared['payload']['topics']}
        self.assertTrue({'F8', 'F9'} <= set(topics['T2']['resume_ids']))
        self.assertIn('F8', topics['T5']['resume_ids'])
        self.assertNotIn('F11', topics['T2']['resume_ids'])  # SQLite 保存不证明 AI 能力。
        self.assertNotIn('F14', topics['T2']['resume_ids'])  # 重连不证明 AI 能力。

    def test_wrong_project_still_rejected_with_question_and_id(self):
        value = deepcopy(self.fixture['response'])
        value['questions'][7]['resume_ids'] = ['F11']
        with self.assertRaisesRegex(LLMError, r'T8.*F11'):
            self.check(value)

    def test_unrelated_technical_citation_still_rejected(self):
        value = deepcopy(self.fixture['response'])
        value['questions'][2]['resume_ids'] = ['F11']
        with self.assertRaisesRegex(LLMError, r'T3.*F11'):
            self.check(value)

    def test_general_technical_questions_need_no_resume_evidence(self):
        value = deepcopy(self.fixture['response'])
        for q, topic in zip(value['questions'], self.prepared['payload']['topics']):
            if topic['type'] == 'technical':
                q['resume_ids'] = []
        data = self.check(value)
        self.assertEqual(data['questions'][3]['resume_evidence'], [])
        self.assertEqual(data['questions'][3]['status'], 'not_evidenced')

    def test_project_questions_need_concrete_facts_not_only_title(self):
        value = deepcopy(self.fixture['response'])
        value['questions'][7]['resume_ids'] = ['F7']
        with self.assertRaisesRegex(LLMError, r'T8.*具体实现或测试'):
            self.check(value)

    def test_ai_technical_question_cannot_cite_only_project_title(self):
        value = deepcopy(self.fixture['response'])
        value['questions'][1]['resume_ids'] = ['F7']
        with self.assertRaisesRegex(LLMError, '仅以项目名称'):
            self.check(value)

    def test_sqlite_practice_does_not_prove_sql_proficiency(self):
        status, evidence = preparation_evidence(self.resume, 'SQL')
        self.assertEqual(status, 'needs_confirmation')
        self.assertTrue(any('SQLite' in line for line in evidence))
        self.assertEqual(preparation_evidence('## 技术技能\n熟练 SQL', 'SQL')[0], 'matched')

    def test_project_name_alone_is_not_ai_capability_or_implementation_question(self):
        resume = '## 项目经历\n### AI Agent Platform\n### Job Agent'
        status, evidence = preparation_evidence(resume, 'AI 应用开发')
        self.assertEqual((status, evidence), ('not_evidenced', []))
        prepared = plan(JOB, resume, 'interview-prep')
        self.assertFalse(any(t['type'] == 'project' for t in prepared['payload']['topics']))
        self.assertEqual(len(self.check(interview_output(prepared['payload']), prepared)['questions']), 10)

    def test_implementation_is_partial_experience_not_complete_ai_ability(self):
        status, evidence = preparation_evidence(self.resume, 'AI 应用开发')
        self.assertEqual(status, 'needs_confirmation')
        self.assertTrue(any('输入校验' in line for line in evidence))
        self.assertNotIn('仅凭名称', ' '.join(evidence))

    def test_weak_and_career_goal_evidence_boundaries(self):
        for text, topic, status in (
                ('## 技术技能\n正在学习 MCP', 'MCP', 'learning'),
                ('## 技术技能\n正在学习 SQL\n## 项目经历\n### Job Agent\n- 使用SQLite保存岗位。', 'SQL', 'learning'),
                ('目标: AI应用开发实习', 'AI 应用开发', 'not_evidenced'),
                ('## 技术技能\nAPI基础', 'LLM API', 'needs_confirmation'),
                ('## 技术技能\nPython基础', 'Python', 'learning')):
            with self.subTest(text=text):
                self.assertEqual(preparation_evidence(text, topic)[0], status)

    def test_three_question_kinds_and_learning_state_are_visible(self):
        data = self.check(self.fixture['response'])
        self.assertEqual({q['question_kind'] for q in data['questions']},
                         {'jd_technical', 'project_followup', 'learning'})
        data.update(mode='llm', model_requests=0, estimated_rmb=0)
        rendered = markdown(data)
        for label in ('JD 通用技术题', '项目经历追问', '学习／待确认题', '学习中／部分匹配'):
            self.assertIn(label, rendered)

    def test_learning_question_cannot_claim_personal_mastery(self):
        value = deepcopy(self.fixture['response'])
        value['questions'][6]['reference_answer'] = '你已经掌握RAG检索和向量数据库，因此可以直接完成生产环境的实现和性能调优。'
        with self.assertRaisesRegex(LLMError, '个人能力/项目事实'):
            self.check(value)


if __name__ == '__main__':
    unittest.main()
