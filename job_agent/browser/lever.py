"""Lever 字段约定的脱网快照适配原型；不能连接或提交真实招聘网站。"""

import asyncio
import json
import re
from dataclasses import asdict
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from agent_platform.workspace import contains_credentials
from job_agent.automation.adapters import ApplicationRejected, MockATS, Receipt
from job_agent.automation.policy import digest
from job_agent.automation.store import GuardError
from job_agent.lever import posting_url
from .offline import ORIGIN, offline_page


class FormSnapshot(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.forms, self.controls, self.buttons, self.restricted = [], [], 0, False
        self.in_form = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if any(word in str(attrs).lower() for word in ("captcha", "recaptcha", "hcaptcha", "turnstile")):
            self.restricted = True
        if any(key in attrs for key in ("formaction", "formmethod", "formenctype")):
            self.restricted = True
        if tag == "input" and attrs.get("type", "").lower() == "password":
            self.restricted = True
        if tag == "form":
            self.forms.append(attrs)
            self.in_form = True
        elif self.in_form and tag in ("input", "select", "textarea"):
            self.controls.append((tag, attrs))
        elif self.in_form and tag == "button" and attrs.get("type", "submit") == "submit":
            self.buttons += 1

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False

    def handle_data(self, data):
        if re.search(r"请输入验证码|人机验证|登录后|verify you are human", data, re.I):
            self.restricted = True


class LeverSnapshotATS:
    """读取操作者取得的公开表单快照；提交仅进入原有SQLite MockATS。

    非在线申请适配器。真实 Lever 自定义问题、反机器人、动态脚本和PDF限制
    尚须逐公司验证。快照脚本禁用；原action核对后才改成本机内存路由。
    """
    name = "lever-snapshot-mock"

    def __init__(self, store, *, html, target, extra_fields=(), behavior="success"):
        if not isinstance(html, str) or len(html.encode()) > 1_000_000:
            raise GuardError("表单快照须为小于1MB的HTML。")
        if contains_credentials(html):
            raise GuardError("快照疑似包含凭据，拒绝加载。")
        parts = urlsplit(target)
        tenant = parts.path.strip("/").split("/")[0]
        self.target = posting_url(target, tenant, "eu" if parts.hostname == "jobs.eu.lever.co" else "global")
        if self.target.endswith("/apply"):
            raise GuardError("请绑定岗位详情链接，而不是其他申请目标。")
        self.html, self.last_audit = html, {}
        self.backend = MockATS(store, extra_fields=extra_fields, behavior=behavior)
        self._validate()

    def _validate(self):
        parsed = FormSnapshot(self.html)
        if parsed.restricted:
            raise GuardError("遇到身份验证或验证码；暂停此渠道，不能绕过。")
        if len(parsed.forms) != 1 or parsed.buttons != 1:
            raise GuardError("表单结构变化：须人工重新核验唯一申请表单与提交按钮。")
        form = parsed.forms[0]
        if (urljoin(self.target, form.get("action", "")) != self.target + "/apply"
            or form.get("method", "").lower() != "post"
            or form.get("enctype", "").lower() != "multipart/form-data"):
            raise GuardError("表单目标/上传方式与岗位不一致，停止填写。")
        known = {f.name: f for f in self.backend.fields(None)}
        names = [attrs.get("name") for _, attrs in parsed.controls]
        if len(names) != len(set(names)) or any(not name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,80}", name) for name in names):
            raise GuardError("表单字段缺失、重名或结构尚不支持，需人工适配。")
        for required in ("name", "email", "resume"):
            if required not in names:
                raise GuardError("必填字段缺失，页面结构需重新核验。")
        for tag, attrs in parsed.controls:
            name = attrs["name"]
            kind = attrs.get("type", "text").lower()
            if ("required" in attrs and name not in known) or kind in ("hidden", "password") or "disabled" in attrs:
                raise GuardError("存在未知必填/隐藏/禁用字段，不能猜测申请信息。")
            if kind == "checkbox" and (name not in known or known[name].kind != "consent"):
                raise GuardError("同意条款必须单独由用户确认。")
            if name != "resume" and (kind not in ("text", "email", "tel", "checkbox") or tag not in ("input", "select", "textarea")):
                raise GuardError("尚未适配该控件类型，需人工核验。")
            if name == "resume" and (kind != "file" or attrs.get("accept", "") not in ("", ".md", "text/markdown")):
                raise GuardError("此快照不接受当前Markdown材料；不得伪装成PDF上传。")
        if any(f.required and f.name not in names for f in known.values()):
            raise GuardError("已确认的问题字段在页面中缺失，需重新检查。")
        return parsed

    def fields(self, job):
        if (job.get("detail_url") or job.get("source_url")) != self.target:
            raise GuardError("表单快照属于其他岗位。")
        return self.backend.fields(job)

    async def lookup(self, attempt_id):
        return await self.backend.lookup(attempt_id)

    async def submit(self, packet, attempt_id):
        if (packet.get("simulated") is not True or packet.get("adapter") != self.name
            or packet.get("target") != self.target):
            raise GuardError("只接受本适配器、同一岗位的模拟材料。")
        self._validate()
        fields = packet["fields"]
        requests = 0

        async def receive(route):
            nonlocal requests
            requests += 1
            try:
                self.backend.store.check_running()
                content_type = route.request.headers.get("content-type", "")
                body = route.request.post_data_buffer or b""
                if not content_type.startswith("multipart/form-data;") or len(body) > 1_000_000 or requests != 1:
                    raise GuardError("上传格式或请求次数不符合已核验材料。")
                message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + content_type + "\r\n\r\n").encode() + body)
                received = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name in received:
                        raise GuardError("重复申请字段。")
                    received[name] = part.get_payload(decode=True).decode("utf-8")
                if received != {**fields, "resume": packet["resume"]}:
                    raise GuardError("浏览器填写结果与已确认信息不一致。")
                receipt = await self.backend.submit(packet, attempt_id)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(asdict(receipt)))
            except ApplicationRejected:
                await route.fulfill(status=422, content_type="application/json", body='{"error":"rejected"}')
            except Exception:
                await route.fulfill(status=503, content_type="application/json", body='{"error":"unknown"}')

        async def run():
            # 绝不执行第三方表单脚本。浏览器仍保持sandbox、离线、HTTP/WS拦截。
            async with offline_page(self.html, handler=receive, java_script_enabled=False) as (page, blocked):
                controls = page.locator("input,select,textarea")
                for index in range(await controls.count()):
                    control = controls.nth(index)
                    name = await control.get_attribute("name")
                    if name == "resume":
                        await control.set_input_files({"name": "tailored-resume.md", "mimeType": "text/markdown", "buffer": packet["resume"].encode()})
                    elif name in fields:
                        kind = await control.get_attribute("type")
                        if kind == "checkbox":
                            if fields[name] != "同意":
                                raise GuardError("未明确确认条款，不能勾选。")
                            # checkbox原value可能为yes，发送前保留已确认语义。
                            await control.evaluate("element => element.value = '同意'")
                            await control.check()
                        elif await control.evaluate("element => element.tagName") == "SELECT":
                            await control.select_option(label=fields[name])
                            value = await control.input_value()
                            if value != fields[name]:
                                raise GuardError("选项标签与提交值不同，须人工明确选项映射。")
                        else:
                            await control.fill(fields[name])
                        if kind != "checkbox" and await control.input_value() != fields[name]:
                            raise GuardError("填写后字段与确认内容不一致。")
                    else:
                        # 未确认的可选字段不提交，避免默认值泄露或误答。
                        await control.evaluate("element => element.disabled = true")
                form = page.locator("form")
                if not await form.evaluate("element => element.checkValidity()"):
                    raise GuardError("浏览器原生表单校验未通过。")
                self.backend.store.check_running()
                await form.evaluate("(element, target) => element.action = target", ORIGIN + "/apply")
                self.last_audit = {"engine": "playwright-chromium", "offline": True,
                    "snapshot_sha256": digest(self.html), "packet_hash": digest(packet),
                    "target_verified": True, "pre_submit_validated": True, "external_submissions": 0}
                async with page.expect_navigation(wait_until="load"):
                    await page.locator('button[type="submit"],button:not([type])').click()
                value = json.loads(await page.locator("body").inner_text())
                self.last_audit.update(mock_submit_requests=requests, blocked_requests=len(blocked))
                if value.get("error") == "rejected":
                    raise ApplicationRejected("模拟平台明确拒收。")
                if value.get("error"):
                    raise GuardError("模拟收件结果未知，只允许查询回执，不能自动重发。")
                return Receipt(**value)

        return await asyncio.wait_for(run(), timeout=20)
