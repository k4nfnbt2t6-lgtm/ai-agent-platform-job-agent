"""Playwright 负责浏览器机制；现有搜索层负责岗位语义与来源边界。"""

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256

from job_agent.search import parse_public_page, safe_url

ORIGIN = "https://job-agent-browser.invalid"


class BrowserUnavailable(RuntimeError):
    pass


@asynccontextmanager
async def offline_page(html, *, handler=None, java_script_enabled=True):
    """只在内存中提供页面；HTTP/WebSocket 全部拦截，不读取用户 Chrome 资料。"""
    if not isinstance(html, str) or len(html.encode("utf-8")) > 1_000_000:
        raise ValueError("离线页面必须是小于1MB的HTML文本。")
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise BrowserUnavailable("请先安装 requirements-browser.txt 和 Playwright Chromium。") from error
    async with async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
        except Exception as error:
            raise BrowserUnavailable("无法启动隔离Chromium，请检查浏览器安装；不会改用个人浏览器。") from error
        try:
            context = await browser.new_context(service_workers="block", accept_downloads=False, offline=True,
                                                java_script_enabled=java_script_enabled)
            blocked = []

            async def route_request(route):
                request = route.request
                if (request.url == ORIGIN + "/" and request.method == "GET" and request.is_navigation_request()
                        and request.frame == page.main_frame):
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                elif handler and request.url == ORIGIN + "/apply" and request.method == "POST":
                    await handler(route)
                else:
                    # 仅记录类型，不记录URL参数、简历或表单内容。
                    blocked.append(request.resource_type)
                    await route.abort("blockedbyclient")

            await context.route("**/*", route_request)
            await context.route_web_socket("**/*", lambda ws: ws.close())
            page = await context.new_page()
            page.set_default_timeout(5000)
            await page.goto(ORIGIN + "/", wait_until="load", timeout=5000)
            yield page, blocked
        finally:
            await browser.close()


async def render_snapshot(html, source, url, *, ready_selector="body", now=None):
    """渲染调用方已合法取得的HTML快照，再复用岗位解析器。不会访问来源URL。

    支持内联脚本生成的卡片；依赖外部JS/API的SPA会明确留下解析失败信息。
    来源抓取仍必须使用 PublicFetcher，不能借此绕过其 robots/DNS/请求限制。
    """
    safe_url(url)

    async def render():
        async with offline_page(html) as (page, blocked):
            await page.locator(ready_selector).first.wait_for(state="attached")
            rendered = await page.content()
            jobs, links, issues = parse_public_page(rendered, source, url, now)
            parsed = {"jobs": jobs, "links": links, "issues": issues}
            parsed["rendering"] = {
                "engine": "playwright-chromium", "network": "blocked",
                "source_url": url, "blocked_requests": len(blocked),
                "snapshot_sha256": sha256(html.encode()).hexdigest(),
                "rendered_sha256": sha256(rendered.encode()).hexdigest(),
            }
            if not parsed["jobs"]:
                parsed["issues"].append("来源快照可读但浏览器渲染后未解析到岗位；外部脚本/API不会自动访问。")
            return parsed

    return await asyncio.wait_for(render(), timeout=15)


async def fetch_rendered_source(fetcher, source, *, ready_selector="body", now=None):
    """可选只读入口：原PublicFetcher取快照，浏览器离线渲染，无新增网络权限。"""
    from job_agent.search import PublicFetcher

    if type(fetcher) is not PublicFetcher or source.get("kind") != "html":
        raise ValueError("浏览器来源只支持已有PublicFetcher和公开HTML配置。")
    url = safe_url(source["url"])
    html = await fetcher.fetch(url)
    return await render_snapshot(html, source, url, ready_selector=ready_selector, now=now)
