"""独立的只读岗位搜索层：公开 HTML/JobPosting 与公开 Job Board GET。"""

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx2

from agent_platform.workspace import contains_credentials
from .analysis import validate_text
from .html_cards import static_cards

USER_AGENT = "JobAgent/3.0"
MAX_RESPONSE_BYTES = 1_000_000
MAX_HTTP_REQUESTS = 24
MAX_SOURCES = 3
AI_DIRECTION = r"ai\s*agent|ai\s*(?:应用|工作流|applications?)|(?<![a-z0-9_])(?:llm|rag)(?![a-z0-9_])|agent\s*(?:开发|工程|实习)|ai\s*(?:工程|engineer)|智能体|大模型(?:应用|开发|研发|工程)|人工智能.*(?:应用|工程)"
INTERNSHIP = r"实习|intern(?:ship)?"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_url(url):
    """只接受无凭据的公开 HTTPS 链接；运行时还会检查 DNS 地址。"""
    if not isinstance(url, str) or len(url) > 2000 or re.search(r"[\s\\\x00-\x1f]", url):
        raise ValueError("来源链接无效。")
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError("来源链接只允许无凭据的 HTTPS 标准端口。")
    if contains_credentials(url) or re.search(r"(?:token|key|password|secret|auth)=", p.query, re.I):
        raise ValueError("来源链接不能携带凭据。")
    if p.hostname.lower() in ("localhost",) or p.hostname.endswith((".local", ".internal")):
        raise ValueError("不能访问本地或内部地址。")
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("不能访问本地或内部地址。")
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.structured = [], [], []
        self.hidden = 0
        self.script = None
        self.anchor = None
        self.role_titles = []
        self.heading_tag = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "nav", "footer"):
            self.hidden += 1
            if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
                self.script = []
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "tr") and not self.hidden:
            self.text.append("\n")
        if tag == "a" and attrs.get("href") and not self.hidden:
            self.anchor = [attrs["href"], []]
        if not self.hidden and (tag == "h1" or "titles" in attrs.get("class", "").split() or bool(set(attrs.get("class", "").split()) & {"job-title", "role-title", "position-title"})):
            self.heading_tag = tag

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if not self.hidden:
            self.text.append(data)
            if self.heading_tag and data.strip():
                self.role_titles.append(data.strip().splitlines()[0])
                self.heading_tag = None
            if self.anchor is not None:
                self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == self.heading_tag:
            self.heading_tag = None
        if tag == "script" and self.script is not None:
            try:
                self.structured.append(json.loads("".join(self.script)))
            except (ValueError, RecursionError):
                pass
            self.script = None
        if tag in ("script", "style", "noscript", "nav", "footer"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "li", "h1", "h2", "h3", "tr") and not self.hidden:
            self.text.append("\n")
        if tag == "a" and self.anchor:
            self.links.append((self.anchor[0], "".join(self.anchor[1]).strip()))
            self.anchor = None


def plain_html(html):
    page = Page()
    page.feed(unescape(html))
    return "\n".join(line.strip() for line in "".join(page.text).splitlines() if line.strip())


def recruitment_type(title, text, declared=""):
    if re.search(INTERNSHIP, title + " " + declared, re.I):
        return "实习"
    if re.search(r"校招|校园招聘|应届|graduate", title + " " + declared + " " + text, re.I):
        return "校招"
    if re.search(r"社招|社会招聘|全职|full.?time|senior|高级|资深", title + " " + declared, re.I):
        return "社招/全职"
    # 正文的“有实习经验”不等于该岗位招实习生。
    return "未知"


@dataclass(frozen=True)
class JobCandidate:
    company: str
    title: str
    city: str
    recruitment_type: str
    jd: str
    source: str
    url: str
    discovered_at: str
    jd_kind: str = "full"
    valid_through: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    parsing_evidence: dict | None = None

    def __post_init__(self):
        for name in ("company", "title", "city", "recruitment_type", "jd", "source"):
            value = validate_text(getattr(self, name), name)
            if contains_credentials(value):
                raise ValueError("岗位材料疑似含凭据，未导入。")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "url", safe_url(self.url))
        for name in ("source_url", "detail_url"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, safe_url(value))
        if self.parsing_evidence is not None:
            if not isinstance(self.parsing_evidence, dict):
                raise ValueError("解析证据需要JSON对象。")
            proof = validate_text(json.dumps(self.parsing_evidence, ensure_ascii=False), "解析证据")
            if contains_credentials(proof):
                raise ValueError("解析证据疑似含凭据。")
        if self.jd_kind not in ("full", "summary"):
            raise ValueError("JD 质量标签只能是 full 或 summary。")
        if datetime.fromisoformat(self.discovered_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("发现时间需要时区。")
        # 整体输入沿用 v2 的材料上限，避免组合后才失败。
        validate_text(self.analysis_text(), "岗位材料")

    def analysis_text(self):
        # JD 自带一致的公司/岗位头时保留输入，复用 v2 手动分析的原岗位哈希。
        company = re.search(r"(?:^|\n)公司[:：]\s*([^\n]+)", self.jd)
        title = re.search(r"(?:^|\n)岗位[:：]\s*([^\n]+)", self.jd)
        if company and title and company[1].strip() == self.company and title[1].strip() == self.title:
            return self.jd
        return f"公司：{self.company}\n岗位：{self.title}\n城市：{self.city}\n招聘类型：{self.recruitment_type}\n{self.jd}"

    def to_dict(self):
        return asdict(self)


def walk_jobs(value):
    if isinstance(value, list):
        for item in value:
            yield from walk_jobs(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if "JobPosting" in ([types] if isinstance(types, str) else types):
            yield value
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in value:
                yield from walk_jobs(value[key])


def structured_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from structured_nodes(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (list, dict)):
                yield from structured_nodes(item)


def parse_public_page(body, source, url, now=None):
    """只认结构化数据或明确标签；不让 LLM 猜公司、城市或缺失 JD。"""
    page = Page()
    page.feed(body)
    result, issues = [], []
    ids = {node["@id"]: node for structured in page.structured for node in structured_nodes(structured)
           if isinstance(node.get("@id"), str)}

    def resolve(value):
        if isinstance(value, dict) and value.get("@id") in ids:
            return {**ids[value["@id"]], **value}
        return value

    for structured in page.structured:
        for row in walk_jobs(structured):
            try:
                org = resolve(row.get("hiringOrganization", {}))
                company = org.get("name") if isinstance(org, dict) else None
                locations = row.get("jobLocation", [])
                locations = [locations] if isinstance(locations, dict) else locations
                addresses = [resolve(resolve(place).get("address", {})) for place in locations if isinstance(place, dict)]
                cities = [address.get("addressLocality") for address in addresses if isinstance(address, dict)]
                description = plain_html(row.get("description", ""))
                for key, label in (("responsibilities", "职责"), ("qualifications", "任职要求"),
                                   ("skills", "技术要求"), ("educationRequirements", "学历要求"), ("experienceRequirements", "经验要求")):
                    if isinstance(row.get(key), str):
                        description += "\n" + label + "：" + plain_html(row[key])
                declared = row.get("employmentType", "")
                declared = " / ".join(declared) if isinstance(declared, list) else declared
                result.append(JobCandidate(company or source.get("company", "未知公司"), row["title"],
                    " / ".join(x for x in cities if x) or "未知", recruitment_type(row["title"], description, declared),
                    description, source["name"], urljoin(url, row.get("url") or url), now or utc_now(),
                    valid_through=row.get("validThrough"), source_url=source["url"] if source.get("url") else url,
                    detail_url=urljoin(url, row.get("url") or url),
                    parsing_evidence={"method": "json_ld_jobposting", "title": row["title"], "location": cities, "company": company} ))
            except (ValueError, TypeError, KeyError, AttributeError):
                issues.append("来源可读但解析失败：JSON-LD岗位字段不完整，跳过。")
    cards, card_links, card_issues = static_cards(body, source)
    page.links.extend(link for link in card_links if link not in page.links)
    issues.extend(card_issues)
    for card in cards:
        try:
            detail = safe_url(urljoin(url, card["detail_link"])) if card["detail_link"] else safe_url(url)
            job = JobCandidate(card["company"], card["title"], card["city"],
                               recruitment_type(card["title"], card["jd"], card["declared"]),
                               card["jd"], source["name"], detail, now or utc_now(),
                               source_url=source.get("url") or url, detail_url=detail, parsing_evidence=card["evidence"])
            if not any(old.company == job.company and old.title == job.title and old.city == job.city and old.recruitment_type == job.recruitment_type and old.jd == job.jd for old in result):
                result.append(job)
        except ValueError:
            issues.append("来源可读但解析失败：岗位卡片字段或详情链接无效。")
    if not result and not cards:
        text = plain_html(body)
        fields = {}
        for key, pattern in (("company", r"(?:公司名称|单位名称|公司|企业名称)[:：]\s*(.+)"), ("title", r"(?:岗位名称|招聘岗位|职位名称|岗位)[:：]\s*(.+)"),
                             ("city", r"(?:工作地点|工作城市|城市)[:：]\s*(.+)")):
            found = re.search(pattern, text)
            fields[key] = found[1].strip() if found else None
        if not fields["title"]:
            fields["title"] = next((title for title in page.role_titles if re.search(AI_DIRECTION, title, re.I)), None)
        # 高校页面常把导航放在无语义 div 中；限定 JD 从真实岗位标题/公司标签开始。
        starts = [match.start() for pattern in (re.escape(fields["title"]) if fields["title"] else r"(?!)", r"(?:公司名称|单位名称|公司|企业名称)[:：]")
                  if (match := re.search(pattern, text))]
        if starts:
            text = text[min(starts):]
        text = re.split(r"(?:^|\n)(?:企业简介|公司简介|其他信息)\s*(?:\n|$)", text, maxsplit=1)[0]
        if fields["title"] and (fields["company"] or source.get("company")) and re.search(r"岗位职责|任职要求|职位描述|岗位要求", text):
            try:
                declared = re.search(r"(?:工作性质|招聘类型)[:：]\s*([^\n]+)", text)
                deadline = re.search(r"(?:结束时间|截止日期|截止时间)[:：]\s*(\d{4}-\d{2}-\d{2})", text)
                result.append(JobCandidate(fields["company"] or source["company"], fields["title"], fields["city"] or "未知",
                    recruitment_type(fields["title"], text, declared[1] if declared else ""), text, source["name"], url, now or utc_now(),
                    valid_through=deadline[1] + "T23:59:59+08:00" if deadline else None,
                    source_url=source.get("url") or url, detail_url=url,
                    parsing_evidence={"method": "html_labels", "title": fields["title"], "company": fields["company"] or source.get("company"), "location": fields["city"]}))
            except ValueError:
                issues.append("岗位正文无效或过大，跳过。")
    if not result and not issues:
        issues.append("来源可读但解析失败：未取得完整岗位；如有公开职位详情链接，将在请求上限内继续核对。")
    return result, page.links, issues


def parse_greenhouse(body, source, now=None):
    result, issues = [], []
    rows = json.loads(body)["jobs"]
    if not isinstance(rows, list):
        raise ValueError("岗位接口结构不符合预期。")
    for row in rows:
        try:
            description = plain_html(row["content"])
            result.append(JobCandidate(source["company"], row["title"], row.get("location", {}).get("name") or "未知",
                recruitment_type(row["title"], description), description, source["name"], row["absolute_url"], now or utc_now(),
                source_url=source.get("url"), detail_url=row["absolute_url"],
                parsing_evidence={"method": "greenhouse", "title": row["title"], "location": row.get("location")}))
        except (ValueError, TypeError, KeyError, AttributeError):
            issues.append("接口岗位没有完整正文或链接，跳过。")
    return result, issues


def load_sources(text):
    sources = json.loads(text)
    if not isinstance(sources, list):
        raise ValueError("来源配置应为 JSON 数组。")
    enabled = []
    for source in sources:
        if not isinstance(source, dict) or type(source.get("enabled", False)) is not bool:
            raise ValueError("来源配置格式错误。")
        if not source.get("enabled", False):
            continue
        if source.get("kind") not in ("html", "greenhouse") or not isinstance(source.get("name"), str):
            raise ValueError("来源只支持 html / greenhouse，并需要名称。")
        if source["kind"] == "greenhouse":
            board = source.get("board", "")
            if not isinstance(board, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", board) or not isinstance(source.get("company"), str) or not source["company"].strip():
                raise ValueError("公开 Job Board 需要已核验的 board 与公司名称。")
            source = {**source, "url": f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"}
        else:
            for field in ("link_pattern",):
                if field in source:
                    raise ValueError("本版不执行来源中的自定义正则或脚本。")
        source = {**source, "url": safe_url(source["url"])}
        if source.get("search_city", "广州") not in ("广州", "上海", "深圳", "不限"):
            raise ValueError("search_city 只能为广州、上海、深圳或不限；它表示搜索阶段，不是岗位地点。")
        if contains_credentials(json.dumps(source, ensure_ascii=False)):
            raise ValueError("来源配置疑似含凭据。")
        enabled.append(source)
    if not 1 <= len(enabled) <= MAX_SOURCES:
        raise ValueError("需要启用 1 到 3 个已核验公开来源；模板默认全部禁用。")
    return enabled


class PublicFetcher:
    """无 Cookie/Key、无重试、无重定向；逐站 robots + 请求/大小/时间边界。"""
    def __init__(self, sources, allow_network=False, transport=None, resolver=None, delay=1.0):
        self.allow_network = allow_network
        self.hosts = {urlsplit(s["url"]).hostname for s in sources}
        self.transport, self.resolver, self.delay = transport, resolver or socket.getaddrinfo, delay
        self.requests = 0
        self.robots = {}
        self.last_request = None

    async def request(self, url):
        if not self.allow_network:
            raise ValueError("真实岗位网络搜索尚未授权；请先确认来源后使用 --allow-network。")
        url = safe_url(url)
        host = urlsplit(url).hostname
        if host not in self.hosts:
            raise ValueError("跳过未授权来源域名。")
        addresses = await asyncio.to_thread(self.resolver, host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("来源 DNS 指向非公开地址，跳过。")
        if self.requests >= MAX_HTTP_REQUESTS:
            raise ValueError("达到本轮公开请求上限，停止搜索。")
        loop = asyncio.get_running_loop()
        if self.last_request is not None:
            await asyncio.sleep(max(0, self.delay - (loop.time() - self.last_request)))
        self.requests += 1
        self.last_request = loop.time()
        try:
            async with httpx2.AsyncClient(transport=self.transport, trust_env=False, timeout=15, follow_redirects=False,
                                         headers={"User-Agent": USER_AGENT, "Accept": "text/html, application/json, text/plain"}) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code == 404 and urlsplit(url).path == "/robots.txt":
                        return ""
                    if response.status_code != 200:
                        raise ValueError(f"公开来源返回 HTTP {response.status_code}，跳过；不重试或绕过。")
                    if not any(t in response.headers.get("content-type", "").lower() for t in ("text/", "application/json", "application/ld+json")):
                        raise ValueError("来源不是公开文本/JSON，跳过。")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise ValueError("来源响应过大，停止读取。")
                        chunks.append(chunk)
                    body = b"".join(chunks).decode(response.encoding or "utf-8", errors="strict")
        except (httpx2.HTTPError, UnicodeError):
            raise ValueError("公开来源请求失败或解码失败，未重试。") from None
        if re.search(r"captcha|verify you are human|人机验证|请输入验证码|登录后(?:查看|访问)", body, re.I):
            raise ValueError("来源要求登录或验证码，跳过；不绕过。")
        return body

    async def fetch(self, url):
        url = safe_url(url)
        p = urlsplit(url)
        origin = f"https://{p.netloc}"
        if origin not in self.robots:
            body = await self.request(origin + "/robots.txt")
            robots = RobotFileParser()
            robots.parse(body.splitlines())
            self.robots[origin] = robots
        robots = self.robots[origin]
        if not robots.can_fetch(USER_AGENT, url):
            raise ValueError("robots.txt 禁止访问该页，跳过。")
        crawl_delay = robots.crawl_delay(USER_AGENT)
        rate = robots.request_rate(USER_AGENT)
        minimum_delay = max(crawl_delay or 0, rate.seconds / rate.requests if rate else 0)
        if minimum_delay:
            if minimum_delay > 5:
                raise ValueError("来源要求较长抓取间隔，本轮跳过。")
            self.delay = max(self.delay, minimum_delay)
        return await self.request(url)


async def search_public(sources, fetcher, limit=8, city_min=2, guangzhou_only=False):
    """按显式来源阶段搜索；上一阶段已足够时不访问下一城市来源。"""
    from .city_policy import CITY_ORDER, city_fit, select_cities, selected_with_aliases
    from .discovery import candidate_filter, similar_job
    if not 1 <= limit <= 20 or not 1 <= city_min <= 20:
        raise ValueError("候选上限与城市扩展阈值需要 1 到 20。")
    found, issues, visited, stages = [], [], set(), []
    selected = None
    for city in CITY_ORDER:
        enough_now = sum(city_fit(job)["city"] in stages for job in selected["selected"]) if selected else 0
        if city != "广州" and (guangzhou_only or enough_now >= min(city_min, limit)):
            break
        stages.append(city)
        stage_sources = [s for s in sources if s.get("search_city", "广州") in (city, "不限")]
        for source in stage_sources:
            queue = [source["url"]]
            pages = 0
            while queue and pages < 4:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                pages += 1
                try:
                    body = await fetcher.fetch(url)
                    if source["kind"] == "greenhouse":
                        jobs, errors = parse_greenhouse(body, source)
                    else:
                        jobs, links, errors = parse_public_page(body, source, url)
                        if pages < 4:
                            for href, title in links:
                                if re.search(AI_DIRECTION, title, re.I):
                                    try:
                                        linked = safe_url(urljoin(url, href))
                                    except ValueError:
                                        continue
                                    if urlsplit(linked).hostname == urlsplit(source["url"]).hostname:
                                        queue.append(linked)
                    for job in jobs:
                        if re.search(AI_DIRECTION, job.title, re.I) and not any(job.url == old.url and similar_job(job, old) for old in found):
                            found.append(job)
                    issues.extend({"source": source["name"], "source_url": source["url"], "page_url": url, "reason": error} for error in errors)

                    counted = select_cities(found, city_min, guangzhou_only, limit,
                                            eligible=lambda job: candidate_filter(job)["eligible"], same_job=similar_job)
                    count = sum(city_fit(job)["priority"] <= CITY_ORDER.index(city) + 1 for job in counted["selected"])
                    if count >= min(city_min, limit):
                        break
                except (ValueError, KeyError, TypeError, RecursionError, OSError) as error:
                    message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "来源格式或网络无效，跳过。"
                    issues.append({"source": source["name"], "source_url": source["url"], "page_url": url, "reason": message})
            selected = select_cities(found, city_min, guangzhou_only, limit,
                                     eligible=lambda job: candidate_filter(job)["eligible"], same_job=similar_job)
            # 上海/深圳混入前一阶段的公开列表，不计为广州有效数量。
            current_count = sum(city_fit(job)["priority"] <= CITY_ORDER.index(city) + 1 for job in selected["selected"])
            if current_count >= min(city_min, limit):
                break
        selected = select_cities(found, city_min, guangzhou_only, limit,
                                 eligible=lambda job: candidate_filter(job)["eligible"], same_job=similar_job)
        # 共享来源已经读过：复用其中下一城市的岗位，不再次抓相同 URL。
        current_count = sum(city_fit(job)["priority"] <= CITY_ORDER.index(city) + 1 for job in selected["selected"])
        if current_count >= min(city_min, limit):
            break
    selected["policy"]["search_stages"] = stages
    # 输出只含核心候选；被筛除/城市未启用的线索只保留解释，不继续分析。
    excluded = [{"candidate": job.to_dict(), "reasons": candidate_filter(job)["reasons"] or ["该城市阶段未启用或无需补充，不进入核心候选。"]}
                for job in [*selected["excluded"], *selected["deferred"]][:limit]]
    return {"candidates": selected_with_aliases(selected, found, limit, similar_job), "issues": issues, "http_requests": fetcher.requests,
            "city_policy": selected["policy"], "excluded": excluded}
