"""只读取静态HTML的岗位卡片，不执行脚本或访问页面里的申请入口。"""

import re
from hashlib import sha256
from urllib.parse import urljoin, urlsplit
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in {'script', 'style', 'noscript', 'nav', 'footer', 'aside'}:
            return ''
        content = ''.join(child.text() if isinstance(child, Node) else child for child in self.children)
        return '\n' + content + '\n' if self.tag in {'div', 'p', 'li', 'section', 'summary', 'h1', 'h2', 'h3', 'h4', 'br'} else content


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('document')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            if len(self.stack) >= 100:
                raise ValueError('来源可读但解析失败：HTML嵌套过深。')
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean(node):
    return '\n'.join(line.strip() for line in node.text().splitlines() if line.strip())


def classes(node, choices):
    return bool(set(node.attrs.get('class', '').split()) & set(choices))


def is_card(node):
    return classes(node, ('role-card', 'job-card', 'position-card', 'vacancy-card', 'job-item', 'position-item', 'recum')) or node.tag == 'details' and classes(node, ('job', 'role', 'position'))


def static_cards(body, source, url=None):
    tree = Tree()
    tree.feed(body)
    nodes = list(tree.root.walk())
    parents = {id(child): node for node in nodes for child in node.children if isinstance(child, Node)}

    def ancestors(node):
        while id(node) in parents:
            node = parents[id(node)]
            yield node

    # 只有当前页的选中栏目才可证明校招；导航里出现“校园招聘”不够。
    campus = next((clean(n) for n in nodes if n.tag == 'a' and clean(n) == '校园招聘'
                   and urlsplit(urljoin(url or source.get('url', ''), n.attrs.get('href', '')))[:4] == urlsplit(url or source.get('url', ''))[:4]
                   and any(classes(p, ('on', 'active', 'current')) for p in ancestors(n))), '')
    company = source.get('company')
    company_evidence = '来源配置company' if company else ''
    if not company:
        for node in nodes:
            if node.tag == 'meta' and node.attrs.get('property') == 'og:site_name':
                company = node.attrs.get('content'); company_evidence = 'og:site_name: ' + (company or '')
                break
    if not company:
        title = next((clean(n) for n in nodes if n.tag == 'title'), '')
        found = re.fullmatch(r'(?:加入我们|招聘|Careers)\s*[|｜—-]\s*(.+)', title, re.I)
        if found:
            company = found[1]; company_evidence = '页面title: ' + title
    cards, links, issues = [], [], []
    for index, card in enumerate(n for n in nodes if is_card(n) and not any(is_card(p) for p in ancestors(n))):
        descendants = list(card.walk())
        heading = next((n for n in descendants if classes(n, ('role-title', 'job-title', 'position-title', 'job-t'))), None)
        if heading is None and classes(card, ('recum',)):
            heading = next((n for n in descendants if n.tag == 'span' and classes(n, ('cm',))), None)
        if heading is None:
            heading = next((n for n in descendants if n.tag in {'h2', 'h3'}), None)
        title = clean(heading) if heading else card.attrs.get('data-title', '')
        title_link = next((n.attrs['href'] for n in descendants if n.tag == 'a' and n.attrs.get('href') and
                           (classes(n, ('job-link', 'detail-link', 'position-link', 'role-title', 'job-title', 'position-title'))
                            or title and clean(n) == title)), None)
        if title_link:
            links.append((title_link, title))
        local_company = next((clean(n) for n in descendants if classes(n, ('company-name', 'job-company'))), None) or company
        location = next((clean(n) for n in descendants if classes(n, ('job-location', 'role-location', 'position-location', 'role-sub'))), '')
        location = card.attrs.get('data-city') or card.attrs.get('data-location') or location
        raw_text = clean(card)
        text = raw_text
        # 为现有 JD 分类器提供明确章节名，同时保留原栏目名称及原文摘要校验值。
        for original, label in (('你将负责', '岗位职责'), ('我们期待你', '任职要求'), ('工作职责', '岗位职责')):
            text = re.sub(r'(?m)^' + original + r'[:：]?$', original + '\n' + label + '：', text)
        explicit = re.search(r'(?:工作地点|工作城市|城市)(?:[:：]|均为|为)\s*([^\n]+)', text)
        if explicit:
            city = explicit[1].strip(); location_evidence = explicit[0]
        else:
            cities = re.findall(r'广州|上海|深圳|北京|杭州|成都|南京|武汉|西安|苏州|重庆', location)
            city = ' / '.join(dict.fromkeys(cities)) or '未知'; location_evidence = location
            # 必须是所在招聘 section 明写“工作地点均为…”，不能使用网站/公司地址。
            if city == '未知':
                section = next((p for p in ancestors(card) if p.tag == 'section'), None)
                declarations = [clean(n) for n in section.walk() if n.tag == 'p'
                                and not any(is_card(p) for p in ancestors(n))
                                and re.match(r'^工作地点均为[^。\n]+[。]?', clean(n))] if section else []
                if len(declarations) == 1:
                    location_evidence = re.match(r'^工作地点均为([^。，,\n]+)', declarations[0])[0]
                    city = re.match(r'^工作地点均为([^。，,\n]+)', declarations[0])[1]
        if not title:
            issues.append('来源可读但解析失败：岗位卡片缺少明确职位标题。')
            continue
        if not re.search(r'岗位职责|任职要求|职位描述|岗位要求', text):
            if not title_link:
                issues.append('来源可读但解析失败：岗位卡片没有JD或公开详情链接。')
            continue
        if not local_company:
            issues.append('来源可读但解析失败：岗位卡片缺少明确公司名称。')
            continue
        if campus:
            text = '招聘类型：校招\n' + text
        closed = re.search(r'(?:岗位状态|招聘状态|职位状态)[:：]?\s*(?:已关闭|已下线|已结束|已过期|停止招聘)|(?:该|本|当前)(?:岗位|职位)(?:已关闭|已下线|已结束|已过期|停止招聘)', raw_text)
        cards.append({'company': local_company, 'title': title, 'city': city, 'jd': text,
                      'declared': location + ' ' + campus, 'detail_link': title_link,
                      'evidence': {'method': 'html_card', 'card': card.attrs.get('id') or str(index+1),
                                   'title': title, 'company': company_evidence or local_company,
                                   'location': location_evidence, 'recruitment_context': campus,
                                   'raw_jd_sha256': sha256(raw_text.encode()).hexdigest(),
                                   'availability': 'closed' if closed else 'not_confirmed',
                                   'availability_evidence': closed[0] if closed else '公开招聘卡片；投递前仍需复核是否在招。'}})
    return cards, links, issues
