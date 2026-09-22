# SPDX-License-Identifier: AGPL-3.0-or-later
"""Baidu_

.. _Baidu: https://www.baidu.com

Prism 本地改造版（挂载覆盖镜像内同名引擎，见 docker-compose.yml searxng-baidu 的
volume）：

- general 类目不再带 ``tn=json``——该参数 100% 触发 302 → wappass 验证码
  （2026-09 A/B 实测，独享干净 IP + 全浏览器头也一样）；去掉后 HTML 页 200 稳定。
- response() 对 general 改走 lxml HTML 解析（parse_general_html，模块级纯函数，
  不依赖 searx 包，可脱离 SearXNG 直接单测）。
- images/it 类目保留上游 JSON 接口不变（prism 只登记 general，此处仅为接口兼容）。
"""

import json
import time
from datetime import datetime
from urllib.parse import urlencode

from lxml import html as lxml_html
from searx.exceptions import SearxEngineAPIException, SearxEngineCaptchaException
from searx.utils import html_to_text

about = {
    "website": "https://www.baidu.com",
    "wikidata_id": "Q14772",
    "official_api_documentation": None,
    "use_official_api": False,
    "require_api_key": False,
    "results": "HTML",  # general 类目；images/it 仍为 JSON
    "language": "zh",
}

paging = True
categories = []
results_per_page = 10

baidu_category = 'general'

time_range_support = True
time_range_dict = {"day": 86400, "week": 604800, "month": 2592000, "year": 31536000}


def init(_):
    if baidu_category not in ('general', 'images', 'it'):
        raise SearxEngineAPIException(f"Unsupported category: {baidu_category}")


def request(query, params):
    page_num = params["pageno"]

    category_config = {
        'general': {
            'endpoint': 'https://www.baidu.com/s',
            'params': {
                "wd": query,
                "rn": results_per_page,
                "pn": (page_num - 1) * results_per_page,
                # ⚠️ 不要加 tn=json：100% 触发 wappass 验证码（本改造的根因）
            },
        },
        'images': {
            'endpoint': 'https://image.baidu.com/search/acjson',
            'params': {
                "word": query,
                "rn": results_per_page,
                "pn": (page_num - 1) * results_per_page,
                "tn": "resultjson_com",
            },
        },
        'it': {
            'endpoint': 'https://kaifa.baidu.com/rest/v1/search',
            'params': {
                "wd": query,
                "pageSize": results_per_page,
                "pageNum": page_num,
                "paramList": f"page_num={page_num},page_size={results_per_page}",
                "position": 0,
            },
        },
    }

    query_params = category_config[baidu_category]['params']
    query_url = category_config[baidu_category]['endpoint']

    if params.get("time_range") in time_range_dict:
        now = int(time.time())
        past = now - time_range_dict[params["time_range"]]

        if baidu_category == 'general':
            query_params["gpc"] = f"stf={past},{now}|stftype=1"

        if baidu_category == 'it':
            query_params["paramList"] += f",timestamp_range={past}-{now}"

    params["url"] = f"{query_url}?{urlencode(query_params)}"
    params["allow_redirects"] = False
    # 百度按 Accept 头分流：SearXNG 默认出站头不设 Accept（online.py 只设
    # Accept-Encoding/Cache-Control/DNT/Connection/User-Agent/Accept-Language），
    # 实际发出 Accept: */* → 一律 302 验证码；浏览器 Accept → 200 放行
    # （2026-09 同一独享 IP A/B 实测）。只补 Accept，不覆盖引擎已有的其他头
    # （settings.template.yml 里 baidu 引擎配了 Referer）
    params.setdefault("headers", {}).setdefault(
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8",
    )
    return params


def response(resp):
    # Detect Baidu Captcha, it will redirect to wappass.baidu.com
    # （302 时不读 body，先看 Location）
    if 'wappass.baidu.com/static/captcha' in resp.headers.get('Location', ''):
        raise SearxEngineCaptchaException()

    if baidu_category == 'general':
        return parse_general_html(resp.text)

    text = resp.text
    if baidu_category == 'images':
        # baidu's JSON encoder wrongly quotes / and ' characters by \\ and \'
        text = text.replace(r"\/", "/").replace(r"\'", "'")
    data = json.loads(text, strict=False)
    parsers = {'images': parse_images, 'it': parse_it}

    return parsers[baidu_category](data)


# ---------------------------------------------------------------------------
# general 类目 HTML 解析（不依赖 searx 包，纯函数便于单测）
# ---------------------------------------------------------------------------

# 结果容器：有机结果 / 百科·地图等 op 卡片 / 广告都带 result+c-container class，
# 广告靠 _is_ad 过滤（h3 class 含 ec_title，或容器内有"广告"角标 span.ec-tuiguang）
_CONTAINER_XPATH = "//div[contains(@class,'result') and contains(@class,'c-container')]"

# 摘要候选 xpath，按优先级取第一个非空：c-abstract 是老版式；新版式（class 带
# new-pmd）摘要在 span.summary-text_*；百科/地图卡片摘要在 c-span-last
_ABSTRACT_XPATHS = (
    ".//*[contains(@class,'c-abstract')]",
    ".//*[contains(@class,'summary-text')]",
    ".//*[contains(@class,'content-right')]",
    ".//*[contains(@class,'c-span-last')]",
    ".//*[contains(@class,'abstract')]",
)


def _norm_text(node) -> str:
    return " ".join(node.text_content().split())


def _is_ad(container) -> bool:
    """广告容器判定：h3 class 含 ec_title，或容器内带"广告"角标
    （span.ec-tuiguang，文本恰为"广告"）。用精确文本匹配而非 contains——
    有机结果摘要里可能出现"广告"一词，不误杀。"""
    h3 = container.xpath(".//h3")
    if h3 and 'ec_title' in (h3[0].get('class') or ''):
        return True
    return bool(container.xpath(
        ".//*[contains(@class,'tuiguang') and normalize-space(text())='广告']"
    ))


def parse_general_html(text: str) -> list[dict]:
    """解析 baidu PC 版 HTML 结果页。JS 渲染壳页（有机结果不在 HTML 里）
    自然返回空列表，不算错误。"""
    results = []
    doc = lxml_html.fromstring(text)
    for container in doc.xpath(_CONTAINER_XPATH):
        if _is_ad(container):
            continue
        link = container.xpath(".//h3//a")
        if not link:
            continue
        link = link[0]
        url = link.get("href") or ""
        title = _norm_text(link)
        if not title or not url.startswith("http"):
            continue
        content = ""
        for xpath in _ABSTRACT_XPATHS:
            nodes = container.xpath(xpath)
            if nodes:
                content = _norm_text(nodes[0])
                if content:
                    break
        results.append({"title": title, "url": url, "content": content})
    return results


def parse_images(data):
    results = []
    if "data" in data:
        for item in data["data"]:
            if not item:
                # the last item in the JSON list is empty, the JSON string ends with "}, {}]"
                continue
            replace_url = item.get("replaceUrl", [{}])[0]
            width = item.get("width")
            height = item.get("height")
            img_date = item.get("bdImgnewsDate")
            publishedDate = None
            if img_date:
                publishedDate = datetime.strptime(img_date, "%Y-%m-%d %H:%M")
            results.append(
                {
                    "template": "images.html",
                    "url": replace_url.get("FromURL"),
                    "thumbnail_src": item.get("thumbURL"),
                    "img_src": replace_url.get("ObjURL"),
                    "title": html_to_text(item.get("fromPageTitle")),
                    "source": item.get("fromURLHost"),
                    "resolution": f"{width} x {height}",
                    "img_format": item.get("type"),
                    "filesize": item.get("filesize"),
                    "publishedDate": publishedDate,
                }
            )
    return results


def parse_it(data):
    results = []
    if not data.get("data", {}).get("documents", {}).get("data"):
        raise SearxEngineAPIException("Invalid response")

    for entry in data["data"]["documents"]["data"]:
        results.append(
            {
                'title': entry["techDocDigest"]["title"],
                'url': entry["techDocDigest"]["url"],
                'content': entry["techDocDigest"]["summary"],
            }
        )
    return results
