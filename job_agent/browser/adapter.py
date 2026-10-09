"""真实浏览器 + 内存招聘表单 + 原SQLite模拟收件平台；没有真实网站提交入口。"""

import asyncio
import json
from dataclasses import asdict

from job_agent.automation.adapters import ApplicationRejected, Field, MockATS, Receipt
from job_agent.automation.policy import digest
from job_agent.automation.store import GuardError
from .offline import offline_page


class OfflineBrowserATS:
    name = "offline-browser-ats"

    def __init__(self, store, *, require_availability=False, require_consent=False, behavior="success"):
        extras = []
        if require_availability:
            extras.append(Field("availability", kind="answer"))
        if require_consent:
            extras.append(Field("agree", kind="consent"))
        self.backend = MockATS(store, extra_fields=extras, behavior=behavior)
        self.last_audit = {}

    def fields(self, job):
        return self.backend.fields(job)

    async def lookup(self, attempt_id):
        return await self.backend.lookup(attempt_id)

    async def submit(self, packet, attempt_id):
        if packet.get("simulated") is not True or packet.get("adapter") != self.name:
            raise GuardError("离线浏览器只接受本适配器的模拟材料。")
        fields = packet["fields"]
        if "availability" in fields and fields["availability"] not in ("3个月", "6个月"):
            raise GuardError("可实习时长不在表单选项中，需人工确认。")
        if "agree" in fields and fields["agree"] != "同意":
            raise GuardError("未明确同意模拟申请条款，禁止勾选。")
        controls = '<label>姓名<input name="name" required></label><label>邮箱<input name="email" type="email" required></label>'
        controls += '<label>简历<input name="resume" type="file" required></label>'
        if "availability" in fields:
            controls += '<label for="availability">可实习时长</label><select id="availability" name="availability" required><option value="">请选择</option><option>3个月</option><option>6个月</option></select>'
        if "agree" in fields:
            controls += '<label><input name="agree" type="checkbox" required>同意模拟申请条款</label>'
        html = """<!doctype html><meta charset="utf-8"><title>仅本地模拟招聘表单</title>
<form>CONTROLS<button type="submit">模拟提交</button></form><pre id="result"></pre>
<script>
document.querySelector('form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  const fields = {name: form.elements.name.value, email: form.elements.email.value};
  if (form.elements.availability) fields.availability = form.elements.availability.value;
  if (form.elements.agree) fields.agree = form.elements.agree.checked ? '同意' : '';
  try {
    const resume = await form.elements.resume.files[0].text();
    const response = await fetch('/apply', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({fields, resume})});
    document.querySelector('#result').textContent = JSON.stringify(await response.json());
  } catch (_) { document.querySelector('#result').textContent = 'unknown'; }
});
</script>""".replace("CONTROLS", controls)
        requests = 0

        async def receive(route):
            nonlocal requests
            requests += 1
            try:
                self.backend.store.check_running()
                if requests != 1 or json.loads(route.request.post_data) != {"fields": fields, "resume": packet["resume"]}:
                    raise GuardError("模拟浏览器表单与已确认材料不一致。")
                receipt = await self.backend.submit(packet, attempt_id)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(asdict(receipt)))
            except ApplicationRejected:
                await route.fulfill(status=422, content_type="application/json", body='{"error":"rejected"}')
            except Exception:
                await route.abort("failed")

        async def run():
            async with offline_page(html, handler=receive) as (page, blocked):
                await page.get_by_label("姓名", exact=True).fill(fields["name"])
                await page.get_by_label("邮箱", exact=True).fill(fields["email"])
                # 上传内存中的已生成材料，不接受任意本地文件路径。
                await page.get_by_label("简历", exact=True).set_input_files({
                    "name": "tailored-resume.md", "mimeType": "text/markdown", "buffer": packet["resume"].encode()})
                if "availability" in fields:
                    await page.get_by_label("可实习时长", exact=True).select_option(label=fields["availability"])
                if "agree" in fields:
                    await page.get_by_label("同意模拟申请条款", exact=True).check()
                await page.get_by_role("button", name="模拟提交", exact=True).click()
                await page.wait_for_function("document.querySelector('#result').textContent.length > 0", timeout=5000)
                result = await page.locator("#result").inner_text()
                self.last_audit = {"engine": "playwright-chromium", "offline": True,
                                   "mock_submit_requests": requests, "blocked_requests": len(blocked),
                                   "packet_hash": digest(packet), "file_uploaded": True}
                if result == "unknown":
                    raise GuardError("模拟收件结果未知；只允许查询回执，不自动重发。")
                value = json.loads(result)
                if value.get("error") == "rejected":
                    raise ApplicationRejected("模拟平台明确拒收。")
                return Receipt(**value)

        return await asyncio.wait_for(run(), timeout=20)
