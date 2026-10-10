"""v3第二次公开验收发现的问题；全部离线/Mock，无真实网络或付费调用。"""

import contextlib
import io
import json
import socket
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2

from job_agent import llm
from job_agent.analysis import analyze_jd, match_resume, skill_gap_analysis
from job_agent.memory import JobMemory
from agent_platform.terminal_ui import PROJECT
from job_agent.runtime import JobRuntime
from job_agent.discovery import analyze_candidates, DiscoveryStore, candidate_filter, rank_job
from job_agent.schemas import JDOutput, MatchOutput
from job_agent.search import PublicFetcher, parse_public_page, search_public

FIXTURE = Path(__file__).parent / 'tests/fixtures/job_agent/flypai-careers.html'
URL = 'https://flypai.com/jobs.html'
SOURCE = {'name': '飞派科技公司招聘页', 'url': URL, 'kind': 'html', 'search_city': '广州'}


def match(jd, resume):
    return match_resume(analyze_jd('任职要求\n' + jd), resume)


class RequirementGroupTests(unittest.TestCase):
    def test_one_language_counts_once_and_best_option_wins(self):
        for text in ('至少掌握一门编程语言，如Python/Java/C++', '熟悉Python、Java、C++至少一种语言', '掌握Python或Java或C++'):
            with self.subTest(text=text):
                result = match(text, '熟练使用Python')
                self.assertEqual(result['match_scores']['required']['total'], 1)
                self.assertEqual(result['match_scores']['overall']['percent'], 100)
                self.assertEqual(result['technical_matches'][0]['status'], 'matched')
                self.assertTrue(result['technical_matches'][0]['expression_matches'])
                self.assertFalse(skill_gap_analysis(result)['priority_actions'])

    def test_partial_option_is_half_and_no_evidence_is_zero(self):
        for resume, status, percent in [('Python基础', 'learning', 50), ('正在学习Java', 'learning', 50), ('仅有职业目标', 'not_evidenced', 0)]:
            with self.subTest(resume=resume):
                result = match('掌握Python或Java', resume)
                self.assertEqual(result['technical_matches'][0]['status'], status)
                self.assertEqual(result['match_scores']['required']['percent'], percent)

    def test_and_within_each_or_branch_is_preserved(self):
        result = match('熟悉Python和Git或Java和Docker', '熟练使用Python')
        self.assertEqual(result['match_scores']['overall']['percent'], 0)
        self.assertEqual(match('熟悉Python和Git或Java和Docker', '熟练使用Java和Docker')['match_scores']['overall']['percent'], 100)

    def test_parentheses_keep_external_mandatory_skill(self):
        self.assertEqual(match('掌握Python和(Git或Docker)', '熟练使用Docker')['match_scores']['overall']['percent'], 0)
        self.assertEqual(match('掌握Python和(Git或Docker)', '熟练使用Python和Docker')['match_scores']['overall']['percent'], 100)

    def test_other_requirement_does_not_join_language_options(self):
        result = match('掌握Python或Java，熟悉Git', '熟练使用Python')
        self.assertEqual(result['match_scores']['required']['total'], 2)
        self.assertEqual(result['match_scores']['overall']['percent'], 50)

    def test_multiline_any_group_and_next_soft_condition(self):
        for heading in ('至少满足以下一项：', '以下任一：', '至少满足一项：'):
            with self.subTest(heading=heading):
                jd = heading + '\n会写Python/Shell/JS任一即可\n会搭建AI Agent\n熟悉未登记模型XYZ\n学习能力强\n熟悉Git\n加分项\n掌握RAG'
                result = match(jd, '项目：AI Agent Platform / Job Agent\n熟练使用Git')
                self.assertEqual(result['match_scores']['required']['total'], 2)
                self.assertEqual(result['match_scores']['required']['percent'], 100)
                group = next(x for x in result['technical_matches'] if x.get('group_id'))
                self.assertEqual(group['expression_matches']['selected_branch'], 1)
                self.assertEqual(len(group['expression_matches']['children']), 3)

    def test_project_experience_with_either_language_uses_project_evidence(self):
        jd=analyze_jd('任职要求\n有Python或Java项目经验')
        result=match_resume(jd,'项目：已完成Python工具项目')
        self.assertEqual(result['experience'][0]['status'],'matched')
        self.assertEqual(result['match_scores']['required']['total'],1)
        self.assertEqual(result['match_scores']['overall']['percent'],100)
        missing=match_resume(jd,'没有Python经验\n项目：已完成Java工具项目')
        self.assertEqual(missing['experience'][0]['status'],'matched')

    def test_qualification_or_technology_does_not_require_both(self):
        result=match('至少满足以下一项：\n硕士及以上学历\n掌握Python', '学历：本科\n熟练使用Python')
        self.assertEqual(result['match_scores']['required']['total'],1)
        self.assertEqual(result['match_scores']['overall']['percent'],100)
        self.assertEqual(result['education'][0]['status'],'explicit_gap')
        self.assertTrue(result['education'][0]['group_id'])
        self.assertFalse(skill_gap_analysis(result)['qualification_checks'])

    def test_inline_qualification_or_technology_keeps_or(self):
        result=match('掌握Python或硕士学历','学历：本科\n熟练使用Python')
        self.assertEqual(result['match_scores']['required']['total'],1)
        self.assertEqual(result['match_scores']['overall']['percent'],100)
        self.assertEqual(result['education'][0]['status'],'explicit_gap')
        self.assertTrue(result['education'][0]['group_id'])
        self.assertFalse(skill_gap_analysis(result)['qualification_checks'])

    def test_or_project_experience_does_not_accept_plain_skill_as_project(self):
        result=match('有Python或Java项目经验','熟练使用Python')
        self.assertEqual(result['match_scores']['required']['total'],1)
        self.assertEqual(result['match_scores']['overall']['percent'],0)
        self.assertEqual(result['technical_matches'][0]['status'],'needs_confirmation')

    def test_unknown_alternative_does_not_become_vacuous_success(self):
        result = match('熟悉Python或未登记技术XYZ', '没有Python经验')
        self.assertEqual(result['match_scores']['overall']['percent'], 0)
        self.assertTrue(result['technical_matches'][0].get('group_id'))

    def test_unregistered_project_domain_does_not_accept_unrelated_project(self):
        resume = '项目：已完成Job Agent项目\n技术栈：Python基础\n正在学习FastAPI'
        result = match('有Python / FastAPI或前后端项目经验', resume)
        self.assertEqual(result['experience'][-1]['requirement'], '前后端项目经验')
        self.assertEqual(result['experience'][-1]['status'], 'needs_confirmation')
        self.assertNotEqual(result['technical_matches'][0]['status'], 'matched')
        self.assertLess(result['match_scores']['overall']['percent'], 100)

    def test_generic_project_experience_still_accepts_real_project(self):
        result = match('有项目经验', '项目：已完成Job Agent项目')
        self.assertEqual(result['experience'][0]['status'], 'matched')

    def test_bonus_group_keeps_lower_weight(self):
        result = match('掌握Git\n加分项\n掌握Python或Java', '熟练使用Python')
        self.assertEqual(result['match_scores']['required']['total'], 1)
        self.assertEqual(result['match_scores']['preferred']['total'], 1)
        self.assertEqual(result['match_scores']['overall']['percent'], 33.3)

    def test_independent_occurrence_of_optional_skill_still_required(self):
        result = match('掌握Python或Java\n掌握Python', '熟练使用Java')
        self.assertEqual(result['match_scores']['required']['total'], 2)
        self.assertEqual(result['match_scores']['overall']['percent'], 50)


class ClassificationBoundaryTests(unittest.TestCase):
    def test_official_careers_headings_separate_duties_requirements_and_bonus(self):
        jd = analyze_jd('你将负责\n参与RAG与Tool Calling功能开发\n我们期待你\n'
                        'Python / FastAPI 项目经验\n加分项\n熟悉Docker')
        self.assertEqual(jd['responsibilities'], ['参与RAG与Tool Calling功能开发'])
        self.assertEqual({(r['skill'], r['level']) for r in jd['technical_requirements']},
                         {('Python', 'required'), ('FastAPI', 'required'), ('Docker', 'preferred')})

    def test_official_careers_heading_does_not_upgrade_resume_evidence(self):
        result = match_resume(analyze_jd('我们期待你\nPython项目经验'),
                              '技能：Python基础\n目标：AI Agent开发实习')
        self.assertNotEqual(result['technical_matches'][0]['status'], 'matched')
        self.assertEqual(result['match_scores']['required']['matched'], 0)

    def test_interest_is_not_technical_requirement(self):
        for text in ('对AI Agent/AI应用开发感兴趣', '对Agent、RAG、大模型应用有强烈兴趣与实践热情'):
            with self.subTest(text=text):
                jd = analyze_jd('任职要求\n'+text)
                self.assertEqual(jd['technical_requirements'], [])
                self.assertTrue(jd['interests'])

    def test_interest_does_not_remove_separate_real_requirement(self):
        jd = analyze_jd('任职要求\n对AI Agent感兴趣，熟悉Python')
        self.assertEqual([x['skill'] for x in jd['technical_requirements']], ['Python'])

    def test_application_materials_never_become_git_skill(self):
        for text in ('附Git/GitHub链接、作品链接', '欢迎附项目材料或Git链接，作为加分参考', '提交GitHub链接和作品集'):
            with self.subTest(text=text):
                jd=analyze_jd('加分项\n'+text)
                self.assertEqual(jd['technical_requirements'], [])
                self.assertTrue(jd['application_materials'])
        jd=analyze_jd('任职要求\n熟悉Git\n投递方式\n提供Git链接和作品集')
        self.assertEqual([x['skill'] for x in jd['technical_requirements']], ['Git'])

    def test_degree_is_required_major_is_preferred(self):
        jd=analyze_jd('任职要求\n本科及以上，计算机/软件工程相关专业优先，在校生可投')
        self.assertEqual(jd['education_requirements'], ['本科及以上', '计算机/软件工程相关专业优先,在校生可投'])
        self.assertEqual(jd['qualification_levels']['本科及以上'], 'required')
        result=match_resume(jd, '学历：大专，能源与动力工程')
        self.assertEqual(result['education'][0]['status'], 'explicit_gap')
        self.assertEqual(result['education'][1]['status'], 'needs_confirmation')
        result=match_resume(jd, '学历：本科，能源与动力工程')
        self.assertEqual(result['education'][0]['status'], 'matched')
        self.assertEqual(result['education'][1]['status'], 'needs_confirmation')

    def test_weak_resume_and_goals_remain_weak(self):
        result=match('熟悉Python\n掌握MCP\n有LLM API经验\n具备AI应用开发项目经验',
                     '学历：本科\nPython基础\n正在学习MCP\nAPI基础\n目标：AI应用开发实习')
        rows={r['skill']:r['status'] for r in result['technical_matches']}
        self.assertEqual(rows, {'Python':'learning','MCP':'learning','LLM API':'needs_confirmation','AI 应用开发':'not_evidenced'})


class LLMBoundaryMockTests(unittest.TestCase):
    def test_host_corrects_group_interest_material_and_degree_without_api(self):
        degree='本科及以上，计算机相关专业优先'
        text='任职要求\n掌握Python或Java\n对AI Agent感兴趣\n'+degree+'\n加分项\n附Git链接'
        output=JDOutput.model_validate({'company':None,'title':None,'responsibilities':[],
            'technical_requirements':[{'skill':skill,'level':'required','evidence':evidence} for skill,evidence in
                                      [('Python','掌握Python或Java'),('Java','掌握Python或Java'),('AI 应用开发','对AI Agent感兴趣'),('Git','附Git链接')]],
            'education_requirements':[{'text':degree,'evidence':degree,'level':'preferred'}],
            'experience_requirements':[],'other_requirements':[],'keywords':[],'limitations':[]})
        jd=llm.checked_jd(output,text)
        self.assertEqual({x['skill'] for x in jd['technical_requirements']},{'Python','Java'})
        self.assertEqual(jd['education_requirements'],['本科及以上','计算机相关专业优先'])
        self.assertEqual(jd['qualification_levels']['本科及以上'],'required')
        resume='熟练使用Python\n学历：本科，能源与动力工程'
        refs=llm.evidence_segments(resume)
        python=next(x['id'] for x in refs if 'Python' in x['text'])
        degree_ref=next(x['id'] for x in refs if '本科' in x['text'])
        output=MatchOutput.model_validate({'technical_matches':[
            {'skill':'Python','status':'matched','evidence_ids':[python],'reason':'真实技能证据'},
            {'skill':'Java','status':'not_evidenced','evidence_ids':[],'reason':'没有证据'}],
            'education':[{'requirement':x,'status':'matched' if i==0 else 'needs_confirmation',
                          'evidence_ids':[degree_ref],'reason':'核对学历和专业'} for i,x in enumerate(jd['education_requirements'])],
            'experience':[],'limitations':[]})
        result=llm.checked_match(output,jd,resume)
        self.assertEqual(result['match_scores']['required']['total'],1)
        self.assertEqual(result['match_scores']['overall']['percent'],100)


    def test_host_keeps_inline_qualification_as_optional_branch(self):
        evidence='掌握Python或硕士学历'
        output=JDOutput.model_validate({'company':None,'title':None,'responsibilities':[],
            'technical_requirements':[{'skill':'Python','level':'required','evidence':evidence}],
            'education_requirements':[{'text':evidence,'evidence':evidence,'level':'required'}],
            'experience_requirements':[],'other_requirements':[],'keywords':[],'limitations':[]})
        jd=llm.checked_jd(output,'任职要求\n'+evidence)
        self.assertEqual(jd['education_requirements'],['硕士学历'])
        self.assertIn('硕士学历',jd['qualification_groups'])

    def test_host_corrects_bonus_level_in_mock_result(self):
        evidence='掌握RAG'
        output=JDOutput.model_validate({'company':None,'title':None,'responsibilities':[],
            'technical_requirements':[{'skill':'RAG','level':'required','evidence':evidence}],
            'education_requirements':[],'experience_requirements':[],'other_requirements':[],'keywords':[],'limitations':[]})
        jd=llm.checked_jd(output,'加分项\n'+evidence)
        self.assertEqual(jd['technical_requirements'][0]['level'],'preferred')



class PublicCardTests(unittest.TestCase):
    def test_saved_real_guangzhou_page_is_automatic_and_separates_cards(self):
        jobs, links, issues=parse_public_page(FIXTURE.read_text(), SOURCE, URL)
        self.assertFalse(issues)
        self.assertEqual(len(jobs), 5)
        core=[j for j in jobs if candidate_filter(j)['eligible']]
        self.assertEqual(len(core), 1)
        j=core[0]
        self.assertEqual((j.company,j.city,j.recruitment_type), ('飞派科技','广州','实习'))
        self.assertEqual(j.source_url, URL)
        self.assertEqual(j.detail_url, URL)
        self.assertEqual(j.parsing_evidence['method'], 'html_card')
        self.assertIn('2027',j.jd)
        self.assertNotIn('投递方式',j.jd)
        self.assertNotIn('mailto:',j.jd)
        self.assertNotIn('社招',j.jd)
        a=analyze_jd(j.jd)
        self.assertEqual(len(a['responsibilities']),8)
        self.assertNotIn('Git', [x['skill'] for x in a['technical_requirements']])
        self.assertEqual(match_resume(a,'Python基础')['match_scores']['required']['total'],1)

    def test_generic_card_work_city_overrides_summary_and_source_origin(self):
        html='<title>加入我们｜人工科技</title><article class="job-card"><h3 class="job-title">RAG开发实习生</h3><span class="job-location">深圳·实习</span><p>工作地点：广州</p><p>岗位职责：构建RAG系统</p><p>任职要求：熟悉Python</p><a class="detail-link" href="/job/1">职位详情</a></article>'
        jobs, links, issues=parse_public_page(html, {'name':'深圳高校','url':'https://careers.example.com/jobs'}, 'https://careers.example.com/jobs')
        self.assertFalse(issues);self.assertEqual(jobs[0].city,'广州')
        self.assertEqual(jobs[0].source_url,'https://careers.example.com/jobs')
        self.assertEqual(jobs[0].detail_url,'https://careers.example.com/job/1')
        self.assertIn('工作地点',jobs[0].parsing_evidence['location'])

    def test_same_title_but_different_jd_cards_are_not_silently_merged(self):
        def card(skill):
            return '<div class="job-card"><h3>AI Agent实习生</h3><span class="job-location">广州·实习</span><p>任职要求：熟悉'+skill+'</p></div>'
        jobs,_,issues=parse_public_page(card('Python')+card('Java'),{'name':'人工公司页','company':'人工公司'},'https://careers.example.com/jobs')
        self.assertFalse(issues)
        self.assertEqual(len(jobs),2)
        self.assertNotEqual(jobs[0].jd,jobs[1].jd)

    def test_card_missing_company_and_unsupported_page_are_explicit_failures(self):
        for body in ('<article class="job-card"><h3>AI Agent实习</h3><p>任职要求：熟悉Python</p></article>', '<div>公开页面，无可识别岗位结构</div>'):
            with self.subTest(body=body):
                jobs, links, issues=parse_public_page(body, {'name':'公开页'}, 'https://careers.example.com/jobs')
                self.assertEqual(jobs,[]);self.assertTrue(any('来源可读但解析失败' in x for x in issues))

    def test_jsonld_list_and_local_id_references_keep_provenance(self):
        posting={'@type':'JobPosting','title':'LLM应用开发实习生','hiringOrganization':{'@id':'#company'}, 'jobLocation':{'@id':'#place'}, 'employmentType':'INTERN', 'description':'岗位职责：构建LLM应用。任职要求：熟悉Python。', 'url':'/jobs/1'}
        graph=[posting,{'@id':'#company','@type':'Organization','name':'人工公司'},{'@id':'#place','@type':'Place','address':{'addressLocality':'广州'}}]
        body='<script type="application/ld+json">'+json.dumps({'@graph':graph})+'</script>'
        jobs,_,issues=parse_public_page(body, {'name':'公开页','url':'https://careers.example.com/list'}, 'https://careers.example.com/detail')
        self.assertFalse(issues);self.assertEqual(jobs[0].company,'人工公司');self.assertEqual(jobs[0].city,'广州')
        self.assertEqual(jobs[0].source_url,'https://careers.example.com/list')
        self.assertEqual(jobs[0].detail_url,'https://careers.example.com/jobs/1')
        self.assertEqual(jobs[0].parsing_evidence['method'],'json_ld_jobposting')

    def test_provenance_survives_save_and_repeat(self):
        jobs,_,_=parse_public_page(FIXTURE.read_text(), SOURCE, URL)
        j=next(j for j in jobs if candidate_filter(j)['eligible'])
        with tempfile.TemporaryDirectory() as folder:
            memory=JobMemory(Path(folder)/'jobs.sqlite')
            store=DiscoveryStore(memory)
            identity,stored,duplicate=store.remember(j)
            self.assertFalse(duplicate)
            self.assertTrue(store.remember(j)[2])
            self.assertEqual(store.sources(identity)[0]['parsing_evidence'],j.parsing_evidence)
            memory.close()


class PublicLinkMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_follow_listing_card_detail_without_llm_or_external_apply(self):
        source={'name':'人工公司页','kind':'html','url':'https://careers.example.com/list','company':'人工公司','search_city':'广州'}
        requests=[]
        pages={'/robots.txt':'User-agent: *\nAllow: /',
               '/list':'<div class="job-card"><h3 class="job-title"><a href="/detail">AI Agent开发实习生</a></h3><span class="job-location">广州·实习</span></div><a href="https://external.example.com/apply">AI Agent投递</a>',
               '/detail':'<p>公司：人工公司</p><p>岗位：AI Agent开发实习生</p><p>工作地点：广州</p><p>工作性质：实习</p><p>任职要求：2027届本科，熟悉Python</p>'}
        def handler(request):
            requests.append(str(request.url));return httpx2.Response(200,text=pages[request.url.path],headers={'Content-Type':'text/html'})
        fetcher=PublicFetcher([source],allow_network=True,transport=httpx2.MockTransport(handler),
                              resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))],delay=0)
        with patch.object(llm.LLMAnalyzer,'ask',side_effect=AssertionError('不许调用LLM')):
            result=await search_public([source],fetcher,city_min=1,guangzhou_only=True)
        self.assertEqual(requests,['https://careers.example.com/robots.txt','https://careers.example.com/list','https://careers.example.com/detail'])
        self.assertEqual(len(result['candidates']),1)
        self.assertEqual(result['candidates'][0].detail_url,'https://careers.example.com/detail')
        self.assertEqual(result['candidates'][0].source_url,source['url'])

    async def test_automatic_real_fixture_three_skills_and_repeat_cache(self):
        jobs,_,_=parse_public_page(FIXTURE.read_text(),SOURCE,URL)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shutil.copytree(PROJECT/'job_agent/skills',root/'job_agent/skills')
            with patch.object(socket.socket,'connect',side_effect=AssertionError('禁止真实网络')), \
                 patch.object(socket.socket,'connect_ex',side_effect=AssertionError('禁止真实网络')), \
                 patch.object(llm.LLMAnalyzer,'ask',side_effect=AssertionError('不许调用LLM')):
                runtime=JobRuntime(root,lambda event:None,demo=True,mode='local')
                try:
                    resume='学历：本科，能源与动力工程\n项目：AI Agent Platform / Job Agent\nPython基础\n工具：Tool Calling\nAPI基础'
                    first=await analyze_candidates(runtime,jobs,resume,guangzhou_only=True,max_analyses=4)
                    self.assertEqual(first['new_analyses'],1)
                    self.assertEqual(len(first['ranked']),1)
                    row=first['ranked'][0]
                    self.assertEqual(row['report']['resume_match']['matching']['match_scores']['required']['total'],1)
                    self.assertTrue(row['report']['skill_gaps'])
                    self.assertEqual(row['candidate']['parsing_evidence']['method'],'html_card')
                    repeat=await analyze_candidates(runtime,jobs,resume,guangzhou_only=True,max_analyses=4)
                    self.assertEqual(repeat['new_analyses'],0)
                    self.assertTrue(repeat['ranked'][0]['reused_analysis'])
                finally:
                    await runtime.close()

    async def test_real_saved_card_page_through_network_mock(self):
        requests=[]
        def handler(request):
            requests.append(request.url.path)
            return httpx2.Response(200,text='User-agent: *\nAllow: /' if request.url.path=='/robots.txt' else FIXTURE.read_text(),headers={'Content-Type':'text/html'})
        fetcher=PublicFetcher([SOURCE],allow_network=True,transport=httpx2.MockTransport(handler),
                              resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))],delay=0)
        result=await search_public([SOURCE],fetcher,guangzhou_only=True)
        self.assertEqual(requests,['/robots.txt','/jobs.html'])
        self.assertEqual(result['city_policy']['valid_counts']['广州'],1)
        self.assertEqual(len(result['candidates']),1)
        self.assertFalse(result['issues'])
