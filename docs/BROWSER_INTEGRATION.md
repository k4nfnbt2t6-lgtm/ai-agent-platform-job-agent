# 可选Playwright浏览器集成

只安装一个成熟浏览器组件。核心依赖与生产 `.venv`、source配置、launchd配置不变；没有真实网站申请适配器。

## 独立环境安装

在项目目录执行，使用官方PyPI包与Playwright官方浏览器下载；不安装系统任务或浏览器扩展：

```bash
cd ~/ai-agent-lab
.venv/bin/python -m venv .agent-platform/browser-eval-venv
.agent-platform/browser-eval-venv/bin/python -m pip install --only-binary=:all: -r requirements.txt -r requirements-browser.txt
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.agent-platform/playwright-browsers"
.agent-platform/browser-eval-venv/bin/python -m playwright install chromium --only-shell
```

包/浏览器/测试输出均在被Git忽略的 `.agent-platform` 中；不复用个人浏览器、Cookie或登录状态。实际验证Python3.14.7、Playwright1.63.0、Chromium Headless Shell153.0.8010.12；其他平台需单独验收。浏览器启动显式启用Chromium sandbox，不添加关闭安全检查参数。

## 完整离线演示

```bash
.agent-platform/browser-eval-venv/bin/python -m job_agent.browser \
  --state-dir .agent-platform/browser-demo
```

仅内置人工2027届本科简历和人工广州Agent实习，不接受真实简历/网站参数：内联JS生成JD → 真实Chromium渲染 → 原JD解析/三个Skill/local评分 → 岗位库 → 独立local定制简历 → 浏览器表单 → 模拟回执 → 安全摘要。

同一命令再次执行应显示 `new_analyses: 0` 和 `already_recorded`。模拟提交不会把真实求职状态改成applied。

## 接口与限制

- `job_agent.browser.offline.render_snapshot`：输入已合法取得的公开HTML，保留来源URL和原始/渲染快照SHA256，复用原解析器；支持内联JS卡片/JSON-LD。
- `fetch_rendered_source`：可选只读入口，必须传原PublicFetcher及HTML来源配置；先执行原有域名/DNS/robots/响应/请求边界，再离线渲染。未将此入口默认接进每日任务。
- 外部脚本、图片、iframe、fetch和WebSocket被拦截，Service Worker禁用。不开放任意公网浏览器导航，因此完整外部脚本SPA不在本轮支持范围。无岗位会明确报告解析失败，不猜测。
- `OfflineBrowserATS`：固定内存模拟表单，以label/role定位，支持文本、单选下拉、人工确认勾选与内存文件上传。不能传任意本地文件路径，也没有真实网站URL/提交按钮配置。
- 未确认的开放问题或同意条款先进入review_required。回执与原材料指纹绑定；未知结果不重发，只查询原SQLite模拟回执。紧急停止/每日数量/重复保护复用原实现。
- JSON/MD摘要不保存表单答案、原文简历、异常正文；材料仍只存私有状态目录。默认关闭tracing/HAR/视频/截图，不产生额外隐私副本。
- 页面“只填写、不提交”也可能被JS自动保存或发送资料；真实网站填写同样需要个人信息外发授权，不能将其当作无需确认的安全操作。

## 测试

未安装浏览器时，基础项目仍能导入和运行；真实浏览器测试明确skip。完整验收必须启用：

```bash
JOB_AGENT_BROWSER_TESTS=1 .agent-platform/browser-eval-venv/bin/python -m unittest test_job_agent_browser -v
JOB_AGENT_BROWSER_TESTS=1 .agent-platform/browser-eval-venv/bin/python -m unittest discover -p 'test*.py'
```

使用前述同一个 `PLAYWRIGHT_BROWSERS_PATH`。若声明启用却缺包/浏览器，测试失败，不静默skip。网络全部是内存路由/MockTransport，只有安装依赖和阅读公开资料需要网络。

模拟成功证明浏览器与原业务流程兼容，不证明某个真实招聘网站允许自动投递或已成功收件。选型依据见 [成熟方案评估](OPEN_SOURCE_EVALUATION.md)。

## 实际应用集成补充

现有自动化入口也可串接mock LLM与浏览器：

```bash
.agent-platform/browser-eval-venv/bin/python -m job_agent.automation demo \
  --mode mock-llm --application-mode browser-mock \
  --state-dir .agent-platform/application-integration-demo
```

另有 `LeverSnapshotATS`：按官方文档的name/email/resume字段约定构建的**脱网快照适配原型**，不是已验收的真实Lever页面适配器。目标租户与岗位UUID绑定；检查唯一表单、必填字段、原action、原生表单校验、文件格式与填写值；支持已明确确认的select/checkbox。未知必填字段、验证码、登录、隐藏字段、控件变化、PDF专用上传均停止。第三方脚本完全禁用，原action只在核对后替换为固定内存路由；原始表单不会向企业POST。

人工fixture：`tests/fixtures/application-integration/lever-form.html`。它**不是**真实网页抓取快照。官方演示站申请页本轮只读访问失败，因此实际DOM定位、自定义问题和招聘方自动化许可仍未通过真实验收。不能把fixture通过当成Lever线上可用。

完整接口格式/浏览器模拟链路与故障测试：

```bash
JOB_AGENT_BROWSER_TESTS=1 .agent-platform/browser-eval-venv/bin/python -m unittest test_job_agent_integration -v
```

更多真实渠道与启用边界见 [实际应用集成](APPLICATION_INTEGRATION.md)。
