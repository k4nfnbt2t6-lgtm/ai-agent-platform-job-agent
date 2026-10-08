"""v3 全部离线：公开源模拟、持久去重、复用真实 SDK 工作流与排序。"""

import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx2
from openai import AsyncOpenAI

import test_job_agent_llm as samples
from agent_platform.terminal_ui import PROJECT
from job_agent import llm
from job_agent.analysis import ANALYSIS_VERSION, analyze_jd, match_resume, skill_gap_analysis
from job_agent.city_policy import city_fit, select_cities
from job_agent.discovery import DiscoveryStore, analyze_candidates, candidate_filter, graduation_fit, print_priority_list, rank_job, similar_job, sort_ranked
from job_agent.runtime import JobRuntime
from job_agent.search import JobCandidate, PublicFetcher, load_sources, parse_greenhouse, parse_public_page, recruitment_type, safe_url, search_public
from job_agent.search_cli import read_json_file, validate_search_args


JD = "岗位职责：参与 AI Agent 应用开发，验证工具工作流并维护工程质量。\n任职要求：2027届本科在读，专业不限，熟悉 Python、Git，掌握 MCP。"
RESUME = "学历：本科在读，能源与动力工程\n使用 Python、Git 完成项目。\n正在学习 MCP。"


def job(**changes):
    values = dict(company="人工科技", title="AI Agent 实习生", city="深圳", recruitment_type="实习", jd=JD,
                  source="人工公开来源", url="https://careers.example.com/jobs/1", discovered_at="2026-10-08T00:00:00+00:00")
    return JobCandidate(**{**values, **changes})


def structured_html(**changes):
    row = {"@type": "JobPosting", "title": "AI Agent 实习生", "hiringOrganization": {"name": "人工科技"},
           "jobLocation": {"address": {"addressLocality": "深圳"}}, "employmentType": "INTERN",
           "description": "<p>" + JD.replace("\n", "</p><p>") + "</p>", "url": "/jobs/1"}
    row.update(changes)
    return '<script type="application/ld+json">' + json.dumps({"@graph": [row]}, ensure_ascii=False) + '</script>'


class CityPolicyTests(unittest.TestCase):
    def choose(self, jobs, **options):
        return select_cities(jobs, eligible=lambda job: candidate_filter(job)["eligible"], same_job=similar_job, **options)

    def test_guangzhou_then_shanghai_then_shenzhen_when_all_present(self):
        chosen = self.choose([job(city="深圳"), job(city="上海"), job(city="广州")], enough=3)
        self.assertEqual([city_fit(x)["city"] for x in chosen["selected"]], ["广州", "上海", "深圳"])
        self.assertEqual(chosen["policy"]["stages"], ["广州", "上海", "深圳"])

    def test_enough_guangzhou_does_not_enable_other_cities(self):
        chosen = self.choose([job(city="深圳"), job(city="上海"), job(city="广州", company="人工甲"), job(city="广州", company="人工乙")])
        self.assertEqual([city_fit(x)["city"] for x in chosen["selected"]], ["广州", "广州"])
        self.assertEqual(chosen["policy"]["stages"], ["广州"])

    def test_shanghai_fills_deficit_before_shenzhen(self):
        chosen = self.choose([job(city="广州"), job(city="深圳"), job(city="上海")])
        self.assertEqual([city_fit(x)["city"] for x in chosen["selected"]], ["广州", "上海"])
        self.assertEqual(chosen["policy"]["stages"], ["广州", "上海"])

    def test_only_valid_unique_jobs_count_toward_threshold(self):
        gz = job(city="广州")
        chosen = self.choose([gz, replace(gz, source="重复页", url="https://other.example.com/1"),
                              job(city="广州", company="过期", valid_through="2020-01-01"), job(city="上海")])
        self.assertEqual(chosen["policy"]["valid_counts"]["广州"], 1)
        self.assertEqual([city_fit(x)["city"] for x in chosen["selected"]], ["广州", "上海"])

    def test_no_guangzhou_and_insufficient_shanghai_reaches_shenzhen(self):
        chosen = self.choose([job(city="上海"), job(city="深圳"), job(city="北京")])
        self.assertEqual(chosen["policy"]["stages"], ["广州", "上海", "深圳"])
        self.assertEqual([city_fit(x)["city"] for x in chosen["selected"]], ["上海", "深圳"])
        self.assertEqual(len(chosen["excluded"]), 1)

    def test_campus_origin_and_company_address_do_not_determine_work_city(self):
        for origin, work in (("深圳", "广州"), ("广州", "深圳"), ("广州", "上海")):
            with self.subTest(origin=origin, work=work):
                candidate = job(source=origin + "高校公开就业网", city="未知", jd=JD + "\n工作地点：" + work + "\n公司总部：广州\n单位地址：深圳")
                fit = city_fit(candidate)
                self.assertEqual(fit["city"], work)
                self.assertIn("不使用来源网站所在地", fit["reason"])
        unknown = job(city="未知", source="广州就业网", jd=JD + "\n公司总部：广州")
        self.assertFalse(candidate_filter(unknown)["eligible"])

    def test_jd_location_overrides_metadata_and_supports_multiple_cities(self):
        candidate = job(city="上海", jd=JD + "\n【工作地点】\n广州、上海、深圳")
        self.assertEqual(city_fit(candidate)["city"], "广州")
        self.assertEqual(city_fit(candidate)["cities"], ["广州", "上海", "深圳"])
        self.assertIn("多城市", city_fit(candidate)["reason"])
        self.assertFalse(candidate_filter(job(city="广州", jd=JD + "\n工作地点：北京"))["eligible"])

    def test_dedup_uses_actual_work_city_not_source_metadata(self):
        left = job(city="上海", jd=JD + "\n工作地点：广州")
        right = replace(left, city="广州", source="另一个公开页", url="https://other.example.com/2")
        self.assertTrue(similar_job(left, right))
        self.assertEqual(self.choose([left, right])["policy"]["valid_counts"]["广州"], 1)

    def test_guangzhou_only_never_falls_back(self):
        chosen = self.choose([job(city="深圳"), job(city="上海")], guangzhou_only=True)
        self.assertEqual(chosen["selected"], [])
        self.assertEqual(chosen["policy"]["stages"], ["广州"])


class CandidateTests(unittest.TestCase):
    def test_jobposting_keeps_provenance_and_full_text(self):
        rows, _, errors = parse_public_page(structured_html(), {"name": "公司公开页"}, "https://careers.example.com/jobs")
        self.assertEqual(len(rows), 1)
        self.assertFalse(errors)
        self.assertEqual((rows[0].company, rows[0].title, rows[0].city, rows[0].recruitment_type), ("人工科技", "AI Agent 实习生", "深圳", "实习"))
        self.assertEqual(rows[0].url, "https://careers.example.com/jobs/1")
        self.assertIn("2027届", rows[0].jd)
        self.assertTrue(rows[0].discovered_at)

    def test_unknown_city_and_type_not_invented_from_experience(self):
        rows, _, _ = parse_public_page(structured_html(title="AI 应用工程师", employmentType="", jobLocation={}, description="任职要求：熟悉Python，有实习经验优先。"), {"name": "公开页"}, "https://careers.example.com/1")
        self.assertEqual(rows[0].city, "未知")
        self.assertEqual(rows[0].recruitment_type, "未知")

    def test_explicit_html_labels_work_and_login_shell_has_no_job(self):
        page = "<p>公司：人工科技</p><p>岗位：LLM实习生</p><p>城市：广州</p><p>任职要求：熟悉 Python，理解 LLM 应用。</p>"
        rows, _, _ = parse_public_page(page, {"name": "公开页"}, "https://careers.example.com/1")
        self.assertEqual(rows[0].city, "广州")
        rows, _, _ = parse_public_page("<h1>登录后查看职位</h1>", {"name": "公开页"}, "https://careers.example.com/1")
        self.assertEqual(rows, [])

    def test_public_campus_heading_company_name_and_deadline_are_preserved(self):
        body = '<div class="titles c_zise">AI工作流实习生（Codex等）<a>收藏</a></div><p>公司名称：<a>人工科技</a></p><p>工作地点：深圳市</p><p>工作性质：实习</p><span>结束时间：2026-12-31</span><p>任职要求：本科及以上在读，熟悉Python，参与Agent工具开发。</p>'
        rows, _, _ = parse_public_page(body, {"name": "高校公开页"}, "https://careers.example.com/1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "AI工作流实习生(Codex等)")
        self.assertEqual(rows[0].company, "人工科技")
        self.assertEqual(rows[0].valid_through, "2026-12-31T23:59:59+08:00")
        self.assertTrue(candidate_filter(rows[0])["eligible"])

    def test_expired_campus_post_is_rejected_not_recommended(self):
        body = '<h1>大模型研发实习生</h1><p>公司名称：人工科技</p><p>工作地点：广州</p><p>工作性质：实习</p><p>结束时间：2020-06-01</p><p>岗位要求：熟悉Python，了解LLM应用开发，参与模型验证。</p>'
        rows, _, _ = parse_public_page(body, {"name": "高校公开页"}, "https://careers.example.com/1")
        self.assertEqual(len(rows), 1)
        self.assertFalse(candidate_filter(rows[0])["eligible"])

    def test_greenhouse_get_preserves_real_description_and_missing_body_skips(self):
        data = {"jobs": [{"title": "LLM实习生", "content": "<p>任职要求：熟悉 Python；2027届本科在读。</p>",
                          "location": {"name": "广州"}, "absolute_url": "https://careers.example.com/1"}, {"title": "无正文"}]}
        rows, issues = parse_greenhouse(json.dumps(data), {"name": "公开岗位板", "company": "人工科技"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].jd, "任职要求:熟悉 Python;2027届本科在读。")
        self.assertEqual(len(issues), 1)

    def test_config_disabled_by_default_and_no_secrets(self):
        with self.assertRaisesRegex(ValueError, "默认全部禁用"):
            load_sources((PROJECT / "job_agent/examples/search-sources.example.json").read_text())
        sources = load_sources(json.dumps([{"name": "岗位板", "kind": "greenhouse", "board": "artificial", "company": "人工科技", "enabled": True}]))
        self.assertEqual(sources[0]["url"], "https://boards-api.greenhouse.io/v1/boards/artificial/jobs?content=true")
        for url in ("http://example.com/", "https://127.0.0.1/", "https://user:pass@example.com/", "https://example.com/?token=private", "https://localhost/", "https://example.com:444/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_url(url)

    def test_required_change_not_deduplicated_but_cosmetic_change_is(self):
        self.assertTrue(similar_job(job(), job(jd=JD.replace("，", ","))))
        self.assertFalse(similar_job(job(), job(jd=JD.replace("Python", "RAG"))))
        self.assertFalse(similar_job(job(), job(jd=JD.replace("2027", "2026"))))
        self.assertFalse(similar_job(job(), job(city="广州")))
        self.assertFalse(similar_job(job(), job(company="其他科技")))
        self.assertFalse(similar_job(job(), job(jd=JD.replace("掌握 MCP", "不要求 MCP"))))
        self.assertFalse(similar_job(job(jd=JD + "\n要求 C++"), job(jd=JD + "\n要求 C#")))

    def test_long_near_duplicate_keeps_identical_requirement_signature(self):
        long_jd = JD.replace("验证工具工作流并维护工程质量", "维护智能助手工具、会话、权限和任务模块；完善团队文档与演示材料；参与代码审查，支持业务落地和工程质量建设")
        changed = long_jd.replace("完善团队文档", "完善项目文档")
        self.assertTrue(similar_job(job(jd=long_jd), job(jd=changed)))

    def test_filter_senior_wrong_year_irrelevant_expired_and_thin_summary(self):
        for candidate in (job(title="AI高级工程师"), job(jd=JD.replace("2027", "2026")), job(title="Java后端实习生"),
                          job(valid_through="2020-01-01"), job(jd="AI Agent 实习岗位", jd_kind="summary")):
            with self.subTest(candidate=candidate):
                self.assertFalse(candidate_filter(candidate)["eligible"])

    def test_unknown_and_range_graduation_handled_without_guessing(self):
        self.assertEqual(graduation_fit("面向本科在读学生")["status"], "2027届待确认")
        self.assertEqual(graduation_fit("面向2026至2028届毕业生")["points"], 100)
        self.assertEqual(graduation_fit("不接受2027届毕业生")["points"], 0)
        self.assertEqual(graduation_fit("发布时间2027年，面向2026届毕业生")["points"], 0)
        self.assertEqual(graduation_fit("面向2027、2028届毕业生")["points"], 100)
        self.assertTrue(candidate_filter(job(jd=JD.replace("2027届", "")))["eligible"])

    def test_master_only_is_filtered_before_paid_analysis(self):
        self.assertFalse(candidate_filter(job(jd=JD + "\n要求硕士在读。"))["eligible"])

    def test_json_file_cannot_use_symlink_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "jobs.json"
            path.write_text("[]")
            link = Path(directory).resolve() / "linked.json"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                read_json_file(link)
            self.assertEqual(read_json_file(path), "[]")


class PublicSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sources = [{"name": "人工页", "kind": "html", "url": "https://careers.example.com/jobs"}]
        self.requests = []
        self.responses = {"/robots.txt": (200, "User-agent: *\nAllow: /", "text/plain"),
                          "/jobs": (200, '<a href="/1">AI Agent 实习生</a><a href="https://other.example.com/2">RAG实习</a><a href="javascript:alert(1)">LLM实习</a>', "text/html"),
                          "/1": (200, structured_html(), "text/html")}
        for method in ("connect", "connect_ex"):
            guard = patch.object(socket.socket, method, side_effect=AssertionError("测试禁止真实网络"))
            guard.start()
            self.addCleanup(guard.stop)

    def fetcher(self, allow_network=True):
        def handler(request):
            self.requests.append(request)
            code, body, content_type = self.responses[request.url.path]
            return httpx2.Response(code, text=body, headers={"Content-Type": content_type})
        return PublicFetcher(self.sources, allow_network=allow_network, transport=httpx2.MockTransport(handler),
            resolver=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))], delay=0)

    def three_city_sources(self):
        sources = [{"name": city + "人工公司页", "kind": "html", "url": "https://careers.example.com/" + path, "search_city": city}
                   for city, path in (("广州", "gz"), ("上海", "sh"), ("深圳", "sz"))]
        self.sources = sources
        for city, path in (("广州", "gz"), ("上海", "sh"), ("深圳", "sz")):
            self.responses["/" + path] = (200, structured_html(hiringOrganization={"name": city + "人工科技"}, jobLocation={"address": {"addressLocality": city}}), "text/html")
        return sources

    async def test_enough_guangzhou_means_no_shanghai_or_shenzhen_http(self):
        sources = self.three_city_sources()
        self.responses["/gz"] = (200, self.responses["/gz"][1] + structured_html(hiringOrganization={"name": "人工广州乙"}, jobLocation={"address": {"addressLocality": "广州"}}), "text/html")
        result = await search_public(sources, self.fetcher())
        self.assertEqual([x.url.path for x in self.requests], ["/robots.txt", "/gz"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["city_policy"]["search_stages"], ["广州"])

    async def test_deficit_visits_shanghai_but_not_shenzhen(self):
        sources = self.three_city_sources()
        result = await search_public(sources, self.fetcher())
        self.assertEqual([x.url.path for x in self.requests], ["/robots.txt", "/gz", "/sh"])
        self.assertEqual([city_fit(x)["city"] for x in result["candidates"]], ["广州", "上海"])

    async def test_three_stage_search_skips_expired_guangzhou_and_stops_at_limit(self):
        sources = self.three_city_sources()
        self.responses["/gz"] = (200, structured_html(jobLocation={"address": {"addressLocality": "广州"}}, validThrough="2020-01-01"), "text/html")
        result = await search_public(sources, self.fetcher(), limit=2)
        self.assertEqual([x.url.path for x in self.requests], ["/robots.txt", "/gz", "/sh", "/sz"])
        self.assertEqual([city_fit(x)["city"] for x in result["candidates"]], ["上海", "深圳"])
        self.assertEqual(len(result["excluded"]), 1)

    async def test_duplicate_source_aliases_do_not_trigger_premature_stage_stop(self):
        sources = self.three_city_sources()
        first = structured_html(hiringOrganization={"name": "人工广州甲"}, jobLocation={"address": {"addressLocality": "广州"}}, url="/one")
        alias = structured_html(hiringOrganization={"name": "人工广州甲"}, jobLocation={"address": {"addressLocality": "广州"}}, url="/alias")
        self.responses["/gz"] = (200, first + alias, "text/html")
        result = await search_public(sources, self.fetcher())
        self.assertIn("/sh", [x.url.path for x in self.requests])
        self.assertNotIn("/sz", [x.url.path for x in self.requests])
        self.assertEqual(result["city_policy"]["valid_counts"]["广州"], 1)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(sum(x.company == "人工广州甲" for x in result["candidates"]), 2)

    async def test_shared_source_does_not_count_shenzhen_as_guangzhou(self):
        result = await search_public(self.sources, self.fetcher())
        self.assertEqual(result["city_policy"]["valid_counts"]["广州"], 0)
        self.assertEqual(result["city_policy"]["search_stages"], ["广州", "上海", "深圳"])
        self.assertEqual(len(self.requests), 3)
        self.requests.clear()
        result = await search_public(self.sources, self.fetcher(), guangzhou_only=True)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["city_policy"]["search_stages"], ["广州"])

    async def test_public_search_is_get_only_bounded_same_host_and_no_llm(self):
        with patch.object(llm.LLMAnalyzer, "ask", side_effect=AssertionError("搜索不能调用LLM")):
            result = await search_public(self.sources, self.fetcher())
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(x.method == "GET" for x in self.requests))
        self.assertTrue(all("authorization" not in x.headers and "cookie" not in x.headers for x in self.requests))

    async def test_no_network_authorization_performs_zero_requests(self):
        result = await search_public(self.sources, self.fetcher(False))
        self.assertEqual(self.requests, [])
        self.assertIn("尚未授权", result["issues"][0]["reason"])

    async def test_robots_denial_skips_job_page(self):
        self.responses["/robots.txt"] = (200, "User-agent: *\nDisallow: /", "text/plain")
        result = await search_public(self.sources, self.fetcher())
        self.assertEqual(len(self.requests), 1)
        self.assertIn("禁止", result["issues"][0]["reason"])

    async def test_captcha_http_failures_and_redirects_never_retry(self):
        for code, body in ((403, "Forbidden"), (429, "rate limit"), (302, "redirect"), (200, "请输入验证码")):
            with self.subTest(code=code):
                self.requests.clear()
                self.responses["/jobs"] = (code, body, "text/html")
                result = await search_public(self.sources, self.fetcher())
                self.assertEqual(result["candidates"], [])
                self.assertEqual(len(self.requests), 2)
                self.assertTrue(result["issues"])

    async def test_private_dns_response_and_oversized_body_rejected(self):
        fetcher = self.fetcher()
        fetcher.resolver = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaisesRegex(ValueError, "非公开"):
            await fetcher.fetch(self.sources[0]["url"])
        self.assertEqual(self.requests, [])
        self.responses["/jobs"] = (200, "x" * 1_000_001, "text/html")
        with self.assertRaisesRegex(ValueError, "过大"):
            await self.fetcher().fetch(self.sources[0]["url"])

    async def test_request_cap_and_timeout_no_retry(self):
        fetcher = self.fetcher()
        fetcher.requests = 24
        with self.assertRaisesRegex(ValueError, "请求上限"):
            await fetcher.fetch(self.sources[0]["url"])
        self.assertEqual(self.requests, [])
        def timeout(request):
            self.requests.append(request)
            raise httpx2.ReadTimeout("not logged", request=request)
        fetcher = self.fetcher()
        fetcher.transport = httpx2.MockTransport(timeout)
        with self.assertRaisesRegex(ValueError, "未重试"):
            await fetcher.fetch(self.sources[0]["url"])
        self.assertEqual(len(self.requests), 1)

    async def test_long_crawl_delay_and_request_rate_are_not_ignored(self):
        for policy in ("Crawl-delay: 10", "Request-rate: 1/10"):
            self.requests.clear()
            self.responses["/robots.txt"] = (200, "User-agent: *\n" + policy, "text/plain")
            with self.assertRaisesRegex(ValueError, "较长抓取间隔"):
                await self.fetcher().fetch(self.sources[0]["url"])
            self.assertEqual(len(self.requests), 1)


class RankingTests(unittest.TestCase):
    def report(self, candidate, resume):
        jd = analyze_jd(candidate.analysis_text())
        matching = match_resume(jd, resume)
        return {"jd_analysis": {"analysis": jd}, "resume_match": {"matching": matching}, "skill_gaps": skill_gap_analysis(matching)}

    def test_high_priority_needs_required_evidence_city_intern_and_confirmed_year(self):
        candidate = job(city="广州")
        report = self.report(candidate, "学历：本科在读\n使用 Python、Git、MCP 完成项目")
        rank = rank_job(candidate, report)
        self.assertEqual(rank["category"], "高优先级")
        self.assertEqual(sum(x["contribution"] for x in rank["components"].values()), rank["rank_score"])
        for alternate in (replace(candidate, city="北京"), replace(candidate, jd=JD.replace("2027届", "")), replace(candidate, jd_kind="summary")):
            self.assertEqual(rank_job(alternate, report)["category"], "可以尝试")

    def ranking_row(self, city, resume, *, good=True, year=True, company=None, skills="Python、Git、MCP、SQL"):
        duties = "岗位职责：开发AI Agent系统，实现工具调用和工作流；开展自动测试和效果评估。\n" if good else "岗位职责：参与AI应用开发。\n"
        requirements = "任职要求：" + ("2027届" if year else "") + "本科在读，专业不限；熟悉" + skills + "。\n"
        benefits = "五、你将获得\n导师带教与技术分享。" if good else ""
        candidate = job(city=city, jd=duties + requirements + benefits, company=company or city + "人工科技")
        return {"candidate_id": candidate.company, "candidate": candidate.to_dict(), "report": self.report(candidate, resume),
                "ranking": rank_job(candidate, self.report(candidate, resume))}

    def test_similar_quality_defaults_to_guangzhou_shanghai_shenzhen(self):
        resume = "本科在读\n掌握Python、Git、MCP、SQL"
        rows = [self.ranking_row(city, resume) for city in ("深圳", "上海", "广州")]
        sort_ranked(rows)
        self.assertEqual([row["ranking"]["city"]["city"] for row in rows], ["广州", "上海", "深圳"])
        self.assertGreater(rows[0]["ranking"]["components"]["城市偏好"]["contribution"], rows[1]["ranking"]["components"]["城市偏好"]["contribution"])
        self.assertTrue(all(not row["ranking"]["city_override"]["overtaken"] for row in rows))

    def test_strong_shanghai_and_shenzhen_can_overtake_ordinary_guangzhou(self):
        for city in ("上海", "深圳"):
            with self.subTest(city=city):
                gz = self.ranking_row("广州", "本科在读\n掌握Python、Git\n正在学习MCP、SQL", good=False)
                better = self.ranking_row(city, "本科在读\n掌握Python、Git\n正在学习MCP、SQL", skills="Python、Git")
                rows = sort_ranked([gz, better])
                self.assertIs(rows[0], better)
                self.assertEqual(better["ranking"]["category"], "高优先级")
                self.assertEqual(better["ranking"]["city_override"]["overtaken"], [gz["candidate_id"]])
                self.assertIn("至少高20个百分点", better["ranking"]["city_override"]["reason"])

    def test_company_name_alone_missing_year_and_thin_duties_cannot_override(self):
        gz = self.ranking_row("广州", "本科在读\nPython基础、Git基础、MCP基础、SQL基础", good=False)
        for changes in ({"good": False, "company": "人工全球知名头部科技"}, {"year": False}):
            with self.subTest(changes=changes):
                other = self.ranking_row("上海", "本科在读\n掌握Python、Git、MCP、SQL", **changes)
                rows = sort_ranked([gz, other])
                self.assertIs(rows[0], gz)
                self.assertFalse(other["ranking"]["city_override"]["eligible"])
                self.assertIsNone(other["ranking"]["quality"]["company_quality"]["score"])

    def test_negated_mentorship_is_not_quality_evidence(self):
        row = self.ranking_row("上海", "本科在读\n掌握Python、Git、MCP、SQL", good=True)
        candidate = job(city="上海", jd=row["candidate"]["jd"].replace("导师带教与技术分享", "不提供导师带教与技术分享"))
        ranking = rank_job(candidate, self.report(candidate, "本科在读\n掌握Python、Git、MCP、SQL"))
        self.assertTrue(all(x["dimension"] != "带教与成长" for x in ranking["quality"]["evidence"]))

    def test_bonus_only_cannot_hide_unmatched_required_skills(self):
        candidate = job(jd="岗位职责：开发 AI 应用并维护服务。\n任职要求：2027届本科在读，熟悉 RAG。\n加分项：Python、Git、MCP、SQL。")
        report = self.report(candidate, "本科在读；使用 Python、Git、MCP、SQL 完成项目")
        rank = rank_job(candidate, report)
        self.assertGreater(rank["overall_match"], 40)
        self.assertEqual(rank["category"], "当前差距较大")

    def test_qualification_gap_prevents_high_priority(self):
        candidate = job(jd=JD + "\n要求：硕士及以上。")
        report = self.report(candidate, "学历：本科在读\n使用 Python、Git、MCP 完成项目")
        self.assertEqual(rank_job(candidate, report)["category"], "当前差距较大")


class DiscoveryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.runtimes, self.requests = [], []
        for method in ("connect", "connect_ex"):
            guard = patch.object(socket.socket, method, side_effect=AssertionError("测试禁止真实网络"))
            guard.start()
            self.addCleanup(guard.stop)
        env = patch.dict(os.environ, {"OPENAI_API_KEY": "offline-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        client = patch.object(llm, "AsyncOpenAI", side_effect=self.factory)
        client.start()
        self.addCleanup(client.stop)

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.close()

    def runtime(self, **kwargs):
        runtime = JobRuntime(self.root, lambda event: None, demo=True, **kwargs)
        self.runtimes.append(runtime)
        return runtime

    def factory(self, **kwargs):
        self.assertEqual(kwargs["max_retries"], 0)
        return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler), trust_env=False), **kwargs)

    def handler(self, request):
        task = json.loads(json.loads(request.content)["input"][1]["content"])
        self.requests.append(task["skill"])
        if task["skill"] == "analyze-jd":
            value = samples.jd_output()
        elif task["skill"] == "match-resume":
            value = samples.match_output(task["materials"]["resume_segments"])
        else:
            value = {"preparations": [{"skill": row["skill"], "preparation": "本地练习并验证。", "acceptance": "解释真实结果。"}
                      for row in task["materials"]["matching"]["technical_matches"] if row["status"] != "matched"], "note": "人工验证建议。"}
        return httpx2.Response(200, json=samples.api_response(value))

    async def test_local_pipeline_reuses_three_skills_saves_and_deduplicates_across_runs(self):
        runtime = self.runtime()
        first = await analyze_candidates(runtime, [job(), job(url="https://other.example.com/1", source="人工第二来源")], RESUME)
        self.assertEqual(first["new_analyses"], 1)
        self.assertEqual(len(first["ranked"]), 1)
        self.assertEqual(len(first["ranked"][0]["sources"]), 2)
        self.assertTrue(first["ranked"][0]["report"]["skill_gaps"])
        self.assertEqual(len(runtime.workspace.memory.list_jobs()), 1)
        await runtime.close()
        self.runtimes.remove(runtime)
        reopened = self.runtime()
        with patch.object(reopened, "analyze", side_effect=AssertionError("重复岗位不应再分析")):
            second = await analyze_candidates(reopened, [job()], RESUME)
        self.assertTrue(second["ranked"][0]["reused_analysis"])
        self.assertEqual(second["new_analyses"], 0)
        self.assertEqual(self.requests, [])

    async def test_changed_resume_and_changed_jd_are_not_stale_cache(self):
        runtime = self.runtime()
        await analyze_candidates(runtime, [job()], RESUME)
        new_resume = await analyze_candidates(runtime, [job()], "本科在读\n目标：AI 应用开发实习")
        self.assertEqual(new_resume["new_analyses"], 1)
        self.assertEqual(new_resume["ranked"][0]["ranking"]["overall_match"], 0)
        changed = await analyze_candidates(runtime, [job(jd=JD + "\n要求熟悉 RAG。")], RESUME)
        self.assertEqual(changed["new_analyses"], 1)
        self.assertEqual(len(runtime.workspace.memory.list_jobs()), 2)

    async def test_filters_and_limit_happen_before_analysis(self):
        runtime = self.runtime()
        result = await analyze_candidates(runtime, [job(title="Java实习生"), job(jd=JD.replace("2027", "2026")),
             job(city="北京"), job(city="广州", company="人工广深科技")], RESUME, max_analyses=1)
        self.assertEqual(len(result["rejected"]), 3)
        self.assertEqual(result["new_analyses"], 1)
        self.assertEqual(result["ranked"][0]["candidate"]["city"], "广州")
        self.assertEqual(len(result["pending"]), 0)

    async def test_search_only_does_not_call_analysis_or_require_resume(self):
        runtime = self.runtime()
        with patch.object(runtime, "analyze", side_effect=AssertionError("只搜索不能分析")):
            result = await analyze_candidates(runtime, [job()], None, search_only=True)
        self.assertEqual(result["new_analyses"], 0)
        self.assertEqual(len(result["pending"]), 1)

    async def test_real_sdk_mock_paid_workflow_is_not_repeated_for_alias_or_restart(self):
        candidate = job(jd=samples.JD)
        runtime = self.runtime(mode="llm", approve_llm=AsyncMock(return_value=True))
        first = await analyze_candidates(runtime, [candidate], samples.RESUME)
        self.assertEqual(self.requests, ["analyze-jd", "match-resume", "skill-gap-analysis"])
        self.assertEqual(first["ranked"][0]["report"]["mode"], "llm")
        await runtime.close()
        self.runtimes.remove(runtime)
        reopened = self.runtime(mode="llm", approve_llm=AsyncMock(side_effect=AssertionError("缓存不应再确认付费")))
        second = await analyze_candidates(reopened, [replace(candidate, url="https://second.example.com/1")], samples.RESUME)
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(second["ranked"][0]["reused_analysis"])
        self.assertEqual(len(reopened.workspace.memory.list_jobs()), 1)

    async def test_local_report_never_substitutes_for_llm_mode(self):
        candidate = job(jd=samples.JD)
        await analyze_candidates(self.runtime(), [candidate], samples.RESUME)
        result = await analyze_candidates(self.runtime(mode="llm", approve_llm=AsyncMock(return_value=True)), [candidate], samples.RESUME)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result["ranked"][0]["report"]["mode"], "llm")

    async def test_existing_v2_exact_jd_report_reused_without_paid_analysis(self):
        candidate = job(title="AI应用实习", jd=samples.JD)
        self.assertEqual(candidate.analysis_text(), candidate.jd)
        runtime = self.runtime(mode="llm", approve_llm=AsyncMock(return_value=True))
        await runtime.analyze(candidate.jd, samples.RESUME)
        self.assertEqual(len(self.requests), 3)
        with patch.object(runtime, "analyze", side_effect=AssertionError("现有v2完整报告不重复付费")):
            result = await analyze_candidates(runtime, [candidate], samples.RESUME)
        self.assertEqual(result["new_analyses"], 0)
        self.assertTrue(result["ranked"][0]["reused_analysis"])
        self.assertEqual(len(runtime.workspace.memory.list_jobs()), 1)

    async def test_interrupted_or_refused_analysis_is_not_automatically_retried(self):
        runtime = self.runtime(mode="llm", approve_llm=AsyncMock(return_value=False))
        first = await analyze_candidates(runtime, [job()], RESUME)
        self.assertEqual(len(first["pending"]), 1)
        with patch.object(runtime, "analyze", side_effect=AssertionError("不能静默重试")):
            second = await analyze_candidates(runtime, [job()], RESUME)
        self.assertEqual(len(second["pending"]), 1)
        self.assertEqual(self.requests, [])

    async def test_previous_local_analysis_version_is_not_reused(self):
        runtime = self.runtime()
        candidate = job(city="广州")
        first = await analyze_candidates(runtime, [candidate], RESUME)
        old_report = first["ranked"][0]["report"]
        old_report["jd_analysis"]["analysis"]["analysis_version"] = ANALYSIS_VERSION - 1
        runtime.workspace.memory.save_report(candidate.analysis_text(), RESUME, "all", old_report)
        runtime.workspace.memory.connection.execute("DELETE FROM discovery_analyses")
        runtime.workspace.memory.connection.commit()
        second = await analyze_candidates(runtime, [candidate], RESUME)
        self.assertEqual(second["new_analyses"], 1)
        self.assertFalse(second["ranked"][0]["reused_analysis"])
        self.assertEqual(second["ranked"][0]["report"]["jd_analysis"]["analysis"]["analysis_version"], ANALYSIS_VERSION)

    async def test_city_selection_precedes_analysis_and_deferred_is_explained(self):
        result = await analyze_candidates(self.runtime(), [job(city="深圳"), job(city="上海"),
                         job(city="广州", company="人工广州甲"), job(city="广州", company="人工广州乙")], RESUME)
        self.assertEqual([row["ranking"]["city"]["city"] for row in result["ranked"]], ["广州", "广州"])
        self.assertEqual(result["new_analyses"], 2)
        self.assertEqual(len(result["deferred"]), 2)

    async def test_shared_resume_end_to_end_shanghai_overtakes_guangzhou(self):
        helper = RankingTests()
        resume = "学历：本科在读\n掌握Python、Git\n正在学习MCP、SQL"
        gz = helper.ranking_row("广州", resume, good=False)
        sh = helper.ranking_row("上海", resume, skills="Python、Git")
        result = await analyze_candidates(self.runtime(), [JobCandidate(**gz["candidate"]), JobCandidate(**sh["candidate"])], resume)
        self.assertEqual(result["new_analyses"], 2)
        self.assertEqual([row["ranking"]["city"]["city"] for row in result["ranked"]], ["上海", "广州"])
        rank = result["ranked"][0]["ranking"]
        self.assertEqual(rank["overall_match"], 100)
        self.assertTrue(rank["city_override"]["overtaken"])
        self.assertEqual(self.requests, [])

    async def test_claim_is_persistent_and_updated_expiry_is_respected(self):
        runtime = self.runtime()
        store = DiscoveryStore(runtime.workspace.memory)
        identity, _, _ = store.remember(job())
        key = store.context_key(runtime, RESUME)
        self.assertTrue(store.claim(identity, key))
        self.assertFalse(store.claim(identity, key))
        result = await analyze_candidates(runtime, [job()], RESUME)
        self.assertEqual(result["new_analyses"], 0)
        expired = await analyze_candidates(runtime, [job(valid_through="2020-01-01")], RESUME)
        self.assertEqual(len(expired["rejected"]), 1)

    async def test_priority_output_contains_links_reasons_and_no_auto_apply(self):
        result = await analyze_candidates(self.runtime(), [job()], RESUME)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            print_priority_list(result)
        text = stream.getvalue()
        for expected in ("高优先级", "可以尝试", "当前差距较大", "2027", "综合匹配度", "技能证据", "原始链接", "没有自动投递"):
            self.assertIn(expected, text)


class SearchCLITests(unittest.TestCase):
    def args(self, **changes):
        from argparse import Namespace
        return Namespace(**{**dict(search=True, search_demo=False, search_fixture=None, sources=None, allow_network=False,
                         search_only=False, jd=None, list_jobs=False, skill="all", llm_dry_run=False, resume=None,
                         search_limit=8, analyze_limit=2), **changes})

    def test_network_requires_sources_and_explicit_authorization(self):
        with self.assertRaisesRegex(ValueError, "allow-network"):
            validate_search_args(self.args())
        validate_search_args(self.args(search_demo=True))

    def test_city_options_validate_before_any_search(self):
        for options in ({"city_min": 0}, {"city_min": 21}):
            with self.assertRaises(ValueError):
                validate_search_args(self.args(search_demo=True, **options))
        validate_search_args(self.args(search_demo=True, guangzhou_only=True, city_min=3))
        with self.assertRaises(ValueError):
            validate_search_args(self.args(search=False, guangzhou_only=True))
        source = {"name": "人工公开页", "kind": "html", "url": "https://careers.example.com/gz", "enabled": True, "search_city": "北京"}
        with self.assertRaisesRegex(ValueError, "search_city"):
            load_sources(json.dumps([source]))

    def test_conflicting_or_excessive_search_options_rejected(self):
        for args in (self.args(search_demo=True, allow_network=True), self.args(search_demo=True, search_fixture="x.json"),
                     self.args(search_demo=True, analyze_limit=6), self.args(search_demo=True, jd="jd.md")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_search_args(args)


if __name__ == "__main__":
    unittest.main()
