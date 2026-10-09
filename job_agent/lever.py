"""Lever 官方公开 Postings GET 格式；不包含申请 POST 或雇主 API Key。"""

import json
import re
from urllib.parse import urlsplit

from .search import JobCandidate, plain_html, recruitment_type, safe_url, utc_now


def posting_url(value, tenant, region="global"):
    url = safe_url(value)
    parts = urlsplit(url)
    host = "jobs.eu.lever.co" if region == "eu" else "jobs.lever.co"
    if parts.hostname != host or parts.query or not re.fullmatch(
        r"/" + re.escape(tenant) + r"/[0-9a-fA-F-]{36}(?:/apply)?", parts.path
    ):
        raise ValueError("Lever 岗位链接与已核验的租户/区域不一致。")
    return url


def parse_lever(body, source, now=None):
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise ValueError("Lever 公开接口应返回岗位数组。")
    result, issues = [], []
    for row in rows:
        try:
            tenant, region = source["tenant"], source.get("region", "global")
            url = posting_url(row["hostedUrl"], tenant, region)
            apply_url = posting_url(row["applyUrl"], tenant, region)
            if apply_url != url.rstrip("/") + "/apply" or not url.endswith("/" + row["id"]):
                raise ValueError("岗位与申请链接不是同一职位。")
            categories = row.get("categories", {})
            # lists 的标题必须保留，避免把优先条件改写成全部必须。
            text = [row.get("descriptionPlain") or plain_html(row.get("description", ""))]
            if row.get("descriptionBodyPlain"):
                text = [row.get("openingPlain", ""), row["descriptionBodyPlain"]]
            text += [(item["text"] + "\n" + plain_html(item["content"])) for item in row.get("lists", [])]
            text += [row.get("additionalPlain") or plain_html(row.get("additional", ""))]
            jd = "\n".join(part for part in text if part and part.strip())
            location = categories.get("location") or "未知"
            result.append(JobCandidate(source["company"], row["text"], location,
                recruitment_type(row["text"], jd, categories.get("commitment", "")),
                jd, source["name"], url, now or utc_now(),
                source_url=source["url"], detail_url=url,
                parsing_evidence={"method": "lever_public_get", "posting_id": row["id"],
                    "location": location, "commitment": categories.get("commitment"),
                    "apply_url": apply_url, "all_locations": categories.get("allLocations", [])}))
        except (KeyError, TypeError, ValueError, AttributeError):
            issues.append("Lever 岗位缺少正文、地点或有效关联链接，跳过并保留解析失败。")
    return result, issues
