# 第三方依赖与许可证核对

核对日期：2026-10-10。依据本机实际安装发行包的 METADATA、License-Expression 与随包 LICENSE 文件，版本对应 requirements 文件；不是仅根据仓库描述推测许可。本次整理不新增或升级依赖，不复制第三方框架源码，也不把虚拟环境、浏览器二进制或字体上传到 Git。

| 依赖 | 固定版本 | 随包声明的许可证 | 用途 |
| --- | --- | --- | --- |
| openai-agents | 0.22.3 | MIT | Runner、Tool Calling、Session、受限子 Agent |
| openai | 3.23.0 | Apache-2.0 | 兼容 API 客户端；包内 vendor 另附许可 |
| pydantic | 2.13.5 | MIT | 结构化输出与数据校验 |
| pypdf | 6.19.0 | BSD-3-Clause | 本地 PDF 简历读取 |
| httpx2 | 2.13.1 | BSD-3-Clause | 受限 HTTP 与 MockTransport |
| playwright，可选 | 1.63.0 | Apache-2.0 | 隔离浏览器、表单输入与请求拦截 |
| pyee，可选 | 13.0.1 | MIT | Playwright 依赖 |
| greenlet，可选 | 3.5.6 | MIT AND PSF-2.0 | Playwright 依赖；部分文件遵循 PSF 许可 |

安装依赖时，各发行包自带版权与许可证，Playwright driver / 内联第三方代码另附通知；Chromium 下载和使用还受其第三方条款约束。上述宽松许可不等于可以删除第三方版权声明。未来若打包分发二进制、容器或复制源码，需要重新核对相应通知与分发义务。

项目自身仍按所有者决定暂不添加 LICENSE。公开 GitHub 作品集不自动授予任意复用权，也不改变依赖自己的许可证。公司招聘页的公开可读性不是其内容的再许可，更不授予自动申请、登录或绕过访问限制的权限。

未接入 browser-use、Stagehand、JobSpy、DrissionPage、Crawlee 或招聘网站扩展，因此没有为凑集成数量增加依赖或引入其权限链；历史评估是日期快照，见 [成熟方案评估](OPEN_SOURCE_EVALUATION.md)。

## 安全公告检查

本次通过 OSV 官方 `https://api.osv.dev/v1/querybatch` 查询隔离浏览器环境的 43 个已安装 PyPI 发行版本，未命中已收录公告。查询只发送包名和版本，不发送源码、简历、密钥或私人数据库。此结果不是不存在漏洞的保证，也不覆盖未知漏洞、操作系统或浏览器全部组件；实际部署仍应关注上游公告。
