"""城市依据只来自岗位字段/JD；来源网站、公司地址不代表工作地点。"""

import re

CITY_ORDER = ("广州", "上海", "深圳")
CITY_POINTS = {"广州": 100, "上海": 45, "深圳": 20}
CITY_NAMES = {"广州": r"广州|guangzhou", "上海": r"上海|shanghai", "深圳": r"深圳|shenzhen"}
LOCATION_LABEL = r"工作地点|工作城市|岗位地点|办公地点|工作地址|招聘城市|工作所在地|job location|location|城市"


def city_fit(job):
    evidence = []
    lines = job.jd.splitlines()
    for index, line in enumerate(lines):
        label = re.sub(r"^[\s#*【\[（(]*|[】\]）)*]", "", line).strip()
        label = re.sub(r"^(?:[一二三四五六七八九十]+|\d+)[、.)]\s*", "", label)
        match = re.match(r"(?:" + LOCATION_LABEL + r")\s*[:：]?\s*(.*)$", label, re.I)
        if match:
            value = match[1].strip()
            if not value and index + 1 < len(lines):
                value = lines[index + 1].strip()
            if value:
                evidence.append(value)
        elif re.search(r"(?:工作|办公|到岗)(?:地点)?(?:为|在|位于)\s*(?:广州|上海|深圳)|(?:广州|上海|深圳)(?:市)?(?:办公|工作|到岗)", line):
            evidence.append(line.strip())
    # 有明确 JD 工作地点时优先采用正文；不从公司/高校名称、企业简介推断。
    values = evidence or [job.city]
    cities = [city for city, pattern in CITY_NAMES.items()
              if any(re.search(pattern, value, re.I) and not re.search(r"(?:不含|不在|不接受|除外)\s*(?:" + pattern + ")", value, re.I) for value in values)]
    preferred = next((city for city in CITY_ORDER if city in cities), None)
    return {"city": preferred or " / ".join(values), "cities": cities,
            "priority": CITY_ORDER.index(preferred) + 1 if preferred else 4,
            "points": CITY_POINTS.get(preferred, 0), "evidence": values,
            "label": {"广州": "第一优先", "上海": "广州不足时的第二候选", "深圳": "广州/上海不足时的第三候选"}.get(preferred, "非核心城市/地点待确认"),
            "reason": ("依据 JD 工作地点" if evidence else "依据岗位城市字段") + "；不使用来源网站所在地。" + ("多城市可选，需确认实际团队地点。" if len(cities) > 1 else "")}


def select_cities(candidates, enough=2, guangzhou_only=False, limit=8, eligible=None, same_job=None):
    """先本地过滤与去重再判断不足；额度以有效岗位数量计算，不按匹配分。"""
    if not 1 <= enough <= 20:
        raise ValueError("城市扩展阈值需要 1-20 个有效候选。")
    groups = {city: [] for city in CITY_ORDER}
    excluded = []
    for job in candidates:
        fit = city_fit(job)
        if eligible and not eligible(job):
            excluded.append(job)
            continue
        if fit["city"] not in groups:
            excluded.append(job)
            continue
        if same_job and any(same_job(job, old) for old in groups[fit["city"]]):
            continue
        groups[fit["city"]].append(job)
    selected = list(groups["广州"][:limit])
    stages = ["广州"]
    if not guangzhou_only:
        for city in CITY_ORDER[1:]:
            if len(selected) >= min(enough, limit):
                break
            stages.append(city)
            selected.extend(groups[city][:min(enough, limit) - len(selected)])
    selected_ids = {id(job) for job in selected}
    deferred = [job for jobs in groups.values() for job in jobs if id(job) not in selected_ids]
    return {"selected": selected, "deferred": deferred, "excluded": excluded,
            "policy": {"order": list(CITY_ORDER), "enough": min(enough, limit), "guangzhou_only": guangzhou_only,
                       "valid_counts": {city: len(jobs) for city, jobs in groups.items()}, "stages": stages,
                       "reason": "先累计广州有效候选；不足补上海，再不足补深圳。有效指方向/JD/类型/届别/截止条件通过且去重，不代表匹配分或录用概率。"}}


def selected_with_aliases(selection, candidates, limit, same_job):
    """阈值只计规范岗位，剩余额度内保留跨来源别名供原记忆层合并。"""
    result = list(selection["selected"][:limit])
    for job in candidates:
        if len(result) >= limit:
            break
        if any(same_job(job, selected) for selected in selection["selected"]) and not any(job.url == old.url for old in result):
            result.append(job)
    return result
