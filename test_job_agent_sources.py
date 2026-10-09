"""来源扩展回归：人工 HTML/Mock，无真实招聘数据、网络或模型请求。"""
import contextlib
import io
import json
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx2

from job_agent.analysis import analyze_jd
from job_agent.city_policy import city_fit
from job_agent.discovery import candidate_filter
from job_agent.memory import JobMemory
from job_agent.search import PublicFetcher, load_sources, parse_public_page, search_public
from job_agent.source_health import SourceHealthStore, print_source_checks, source_check

FIXTURES = Path(__file__).parent / 'tests/fixtures/job_agent/public-sources'
STAMP = '2026-10-08T04:00:00+00:00'


class SearchExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_failed_sources_return_failure_but_keep_report(self):
        from argparse import Namespace
        from job_agent.search_cli import run_search
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            import shutil
            shutil.copytree(Path(__file__).parent / 'job_agent/skills', root / 'job_agent/skills')
            args = Namespace(search_demo=False, search_fixture=None, search_only=False, mode='local',
                             state_dir=str(root / '.agent-platform/jobs'), session='test', model=None, fallback='local',
                             analyze_limit=2, city_min=2, search_limit=5, guangzhou_only=True)
            data = {'candidates': [], 'issues': [{'source': '人工来源', 'reason': '模拟来源访问失败'}],
                    'http_requests': 0, 'city_policy': {}, 'source_checks': []}
            with patch('job_agent.search_cli.PROJECT', root), patch('job_agent.search_cli.collect_search', AsyncMock(return_value=data)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(await run_search(args, 'Python基础', None, None), 1)
                report = next((root / '.agent-platform/jobs').glob('priority-list-*.json'))
                self.assertTrue(json.loads(report.read_text())['search_issues'])
                data['issues'] = []
                # 成功读取但确实没有岗位，不是执行失败。
                self.assertEqual(await run_search(args, 'Python基础', None, None), 0)


def source(name='人工公开公司', city='广州', path='campus.html'):
    return dict(name=name, company=name, kind='html', enabled=True, search_city=city,
                url='https://careers.example.com/' + path, supported_parsers=['html_card'])


def parsed(name='campus-cards.html', config=None):
    s = config or source()
    return parse_public_page((FIXTURES / name).read_text(), s, s['url'], now=STAMP)


class SourceParserTests(unittest.TestCase):
    def test_campus_accordion_extracts_isolated_complete_jd_and_actual_city(self):
        jobs, _, errors = parsed()
        self.assertEqual(errors, [])
        self.assertEqual(len(jobs), 2)
        first = jobs[0]
        self.assertEqual(first.title, 'AI开发工程师')
        self.assertEqual(first.city, '广州')
        self.assertEqual(first.recruitment_type, '校招')
        self.assertTrue(candidate_filter(first)['eligible'])
        self.assertNotIn('已关闭', first.jd)
        self.assertEqual(first.source_url, source()['url'])
        self.assertEqual(first.detail_url, source()['url'])
        self.assertEqual(first.parsing_evidence['recruitment_context'], '校园招聘')
        self.assertEqual(len(first.parsing_evidence['raw_jd_sha256']), 64)
        analysis = analyze_jd(first.analysis_text())
        self.assertTrue(analysis['responsibilities'])
        self.assertNotIn('LangChain', [r['skill'] for r in analysis['technical_requirements'] if r['level'] == 'required'])
        self.assertIn('LangChain', [r['skill'] for r in analysis['technical_requirements'] if r['level'] == 'preferred'])

    def test_closed_job_is_excluded_before_analysis(self):
        closed = parsed()[0][1]
        self.assertEqual(closed.parsing_evidence['availability'], 'closed')
        self.assertFalse(candidate_filter(closed)['eligible'])

    def test_campus_navigation_alone_cannot_prove_campus_hiring(self):
        body = (FIXTURES / 'campus-cards.html').read_text().replace('class="joiems on"', 'class="joiems"')
        jobs, _, _ = parse_public_page(body, source(), source()['url'])
        self.assertEqual(jobs[0].recruitment_type, '未知')

    def test_same_path_with_different_query_cannot_prove_campus_context(self):
        body = (FIXTURES / 'campus-cards.html').read_text().replace('href="campus.html"', 'href="campus.html?type=campus"')
        jobs, _, _ = parse_public_page(body, source(), 'https://careers.example.com/campus.html?type=social')
        self.assertEqual(jobs[0].recruitment_type, '未知')
        body = (FIXTURES / 'campus-cards.html').read_text()
        jobs, _, _ = parse_public_page(body, source(), 'https://careers.example.com/social.html')
        self.assertEqual(jobs[0].recruitment_type, '未知')

    def test_shared_location_is_scoped_to_section_and_overridden_by_job(self):
        jobs, _, errors = parsed('accordion-cards.html')
        self.assertEqual(errors, [])
        self.assertEqual(jobs[1].city, '上海')
        self.assertEqual(city_fit(jobs[1])['city'], '上海')
        self.assertEqual(jobs[2].city, '深圳')
        self.assertEqual(jobs[3].city, '未知')
        self.assertFalse(candidate_filter(jobs[3])['eligible'])
        self.assertEqual(jobs[1].recruitment_type, '实习')
        self.assertNotIn('全栈工程师', jobs[1].jd)
        analysis = analyze_jd(jobs[1].analysis_text())
        self.assertTrue(analysis['responsibilities'])
        self.assertIn('Python', [r['skill'] for r in analysis['technical_requirements'] if r['level'] == 'required'])
        self.assertIn('RAG', [r['skill'] for r in analysis['technical_requirements'] if r['level'] == 'preferred'])

    def test_one_job_location_declaration_cannot_leak_to_its_siblings(self):
        body = (FIXTURES / 'accordion-cards.html').read_text().replace(
            '<p class="rolenote">工作地点均为上海，个别岗位涉及出差，详见岗位说明。</p>', '').replace(
            '<p>工作地点：深圳</p>', '<p>工作地点均为深圳</p>')
        jobs, _, _ = parse_public_page(body, source(), source()['url'])
        self.assertEqual(jobs[1].city, '未知')
        self.assertEqual(jobs[2].city, '深圳')

    def test_readable_title_only_and_expired_notice_are_not_success(self):
        for body, reason in [('<h1>AI应用实习生</h1><p>公司地址：广州</p>', '解析失败'),
                             ('<p>信息已过期，无法查看</p>', '已过期')]:
            with self.subTest(body=body):
                jobs, links, issues = parse_public_page(body, source(), source()['url'])
                self.assertEqual(jobs, [])
                self.assertIn(reason, ' '.join(issues))

    def test_bounded_source_expansion_does_not_remove_request_limit(self):
        sources = [source(str(i), path=f'{i}.html') for i in range(8)]
        self.assertEqual(len(load_sources(json.dumps(sources))), 8)
        with self.assertRaisesRegex(ValueError, '1 到 8'):
            load_sources(json.dumps(sources + [source('9', path='9.html')]))
        from job_agent.search import MAX_HTTP_REQUESTS
        self.assertEqual(MAX_HTTP_REQUESTS, 24)

    def test_duplicate_source_urls_cannot_claim_separate_verifications(self):
        with self.assertRaisesRegex(ValueError, '来源URL重复'):
            load_sources(json.dumps([source(), source('另一个名称')]))

    def test_curated_catalog_records_verified_and_disabled_sources_separately(self):
        rows = json.loads((Path(__file__).parent / 'job_agent/sources/public-careers.json').read_text())
        enabled = load_sources(json.dumps(rows))
        self.assertEqual([row['search_city'] for row in enabled], ['广州', '广州', '上海'])
        for row in rows:
            self.assertTrue(row['supported_parsers'])
            v = row['verification']
            self.assertIsNotNone(datetime.fromisoformat(v['checked_at']).tzinfo)
            if row['enabled']:
                self.assertTrue(v['verified_real_search'])
                self.assertGreater(v['eligible_count'], 0)
                self.assertIsNotNone(v['last_success_at'])
            else:
                self.assertFalse(v['verified_real_search'])
                self.assertTrue(v['failure_reason'])


class SourceHealthTests(unittest.TestCase):
    def test_reachable_but_unparsed_and_filtered_are_not_verified(self):
        failed = source_check(source(), [], [source()['url']], ['缺少完整JD'], STAMP)
        self.assertEqual(failed['status'], 'readable_parse_failed')
        self.assertIsNone(failed['last_success_at'])
        closed = source_check(source(), [parsed()[0][1]], [source()['url']], [], STAMP)
        self.assertEqual(closed['status'], 'parsed_no_eligible')
        self.assertFalse(closed['verified_real_search'])
        self.assertTrue(closed['excluded'][0]['reasons'])
        expired = source_check(source(), [], [source()['url']], ['来源明确标记信息已过期/职位已下线，不导入旧岗位。'], STAMP)
        self.assertEqual(expired['status'], 'no_open_jobs')
        self.assertFalse(expired['verified_real_search'])

    def test_success_timestamp_survives_failure_restart_and_skipped_city(self):
        with tempfile.TemporaryDirectory() as root:
            memory = JobMemory(Path(root) / "jobs.sqlite")
            store = SourceHealthStore(memory)
            good = source_check(source(), parsed()[0], [source()['url']], [], STAMP)
            store.record([good])
            self.assertEqual(store.list()[0]['eligible_count'], 1)
            self.assertEqual(store.list()[0]['eligible_cities']['广州'], 1)
            later = '2026-10-09T04:00:00+00:00'
            store.record([source_check(source(), [], [], ['HTTP 403'], later)])
            self.assertEqual(store.list()[0]['last_success_at'], STAMP)
            self.assertEqual(store.list()[0]['checked_at'], later)
            memory.close()
            memory = JobMemory(Path(root) / "jobs.sqlite")
            store = SourceHealthStore(memory)
            store.record([dict(name='人工公开公司', url=source()['url'], status='not_checked')])
            self.assertEqual(store.list()[0]['status'], 'fetch_failed')
            self.assertEqual(store.list()[0]['last_success_at'], STAMP)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                print_source_checks(store.list())
            self.assertIn('访问失败', output.getvalue())
            memory.close()


class SourceNetworkMockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('禁止真实网络'))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.seen = []
        self.sources = [source('广州公司'), source('上海公司', '上海', 'sh.html'), source('深圳公司', '深圳', 'sz.html')]
        self.pages = {'/campus.html': (FIXTURES / 'campus-cards.html').read_text(),
                      '/sh.html': (FIXTURES / 'accordion-cards.html').read_text(),
                      '/sz.html': '<p>没有公开JD</p>'}

    def fetcher(self):
        def handler(request):
            self.seen.append(request.url.path)
            body = 'User-agent: *\nAllow: /' if request.url.path == '/robots.txt' else self.pages[request.url.path]
            return httpx2.Response(200, text=body, headers={'Content-Type': 'text/html'})
        return PublicFetcher(self.sources, allow_network=True, transport=httpx2.MockTransport(handler), delay=0,
                             resolver=lambda *a, **kw: [(None, None, None, None, ('8.8.8.8', 443))])

    async def test_guangzhou_sufficient_skips_other_cities_and_reports_not_checked(self):
        result = await search_public(self.sources, self.fetcher(), city_min=1)
        self.assertEqual(self.seen, ['/robots.txt', '/campus.html'])
        self.assertEqual([c['status'] for c in result['source_checks']], ['verified', 'not_checked', 'not_checked'])
        self.assertEqual(result['city_policy']['search_stages'], ['广州'])

    async def test_guangzhou_insufficient_expands_shanghai_and_prefers_explicit_internship(self):
        result = await search_public(self.sources, self.fetcher(), city_min=2)
        self.assertEqual(self.seen, ['/robots.txt', '/campus.html', '/sh.html'])
        self.assertEqual([city_fit(j)['city'] for j in result['candidates']], ['广州', '上海'])
        self.assertEqual(result['candidates'][1].recruitment_type, '实习')
        self.assertEqual(result['source_checks'][1]['status'], 'verified')
        self.assertEqual(result['source_checks'][2]['status'], 'not_checked')

    async def test_parse_failure_is_explicit_and_does_not_get_success_timestamp(self):
        self.pages['/campus.html'] = '<h1>AI Agent实习生</h1>'
        result = await search_public(self.sources, self.fetcher(), guangzhou_only=True)
        self.assertEqual(result['candidates'], [])
        check = result['source_checks'][0]
        self.assertEqual(check['status'], 'readable_parse_failed')
        self.assertFalse(check['verified_real_search'])
        self.assertIsNone(check['last_success_at'])
        self.assertIn('解析失败', ' '.join(check['failure_reasons']))

    async def test_detail_link_success_preserves_list_and_detail_urls(self):
        self.pages['/campus.html'] = '<a href="/detail.html">AI应用开发实习生</a>'
        self.pages['/detail.html'] = '<h1>AI应用开发实习生</h1><p>公司：人工公司</p><p>工作地点：广州</p><p>岗位职责：开发AI应用和工具调用，参与测试。</p><p>任职要求：2027届本科，熟悉Python。</p>'
        result = await search_public(self.sources, self.fetcher(), guangzhou_only=True)
        self.assertEqual(len(result['candidates']), 1)
        job = result['candidates'][0]
        self.assertEqual(job.source_url, self.sources[0]['url'])
        self.assertEqual(job.detail_url, 'https://careers.example.com/detail.html')
        self.assertEqual(result['source_checks'][0]['status'], 'verified')
        self.assertEqual(len(result['source_checks'][0]['readable_pages']), 2)


if __name__ == '__main__':
    unittest.main()
