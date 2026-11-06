#!/usr/bin/env python3
"""
kindle-pdocs-cleanup — 清理 Send-to-Kindle 邮件推送的个人文档(PDoc)，仅保留最近 N 个。

原理：
  1. GET  https://www.amazon.com/hz/mycd/digital-console/contentlist/pdocsAll/dateDsc/
     从页面 HTML 提取 csrfToken（需已登录的 Amazon cookies）。
  2. POST /hz/mycd/digital-console/ajax  activity=GetContentOwnershipData
     contentType=KindlePDoc, contentCategoryReference=pdocs, 按获取日期降序。
  3. 保留最新 KEEP 个，其余逐个（或整批）删除：
     activity=DeleteContent, activityInput={"asinDetails":{"<ASIN>":{"category":"KindlePDoc"}}}

用法：
  1. 浏览器登录 amazon.com 后打开 Content & Devices 页面，F12 → Network →
     复制任意请求的 Cookie 请求头整串，填入同目录 config.json 的 "cookies"。
  2. python3 cleanup.py            # 干跑：只列出将删除的文档
     python3 cleanup.py --apply    # 真正删除
     python3 cleanup.py --keep 3 --apply

退出码：0 成功 / 1 配置错误 / 2 cookies 失效 / 3 有删除失败 / 4 接口响应异常
零第三方依赖（仅 Python 标准库），可在 OpenClaw 定时任务里直接跑。
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 当前 MYCD 控制台用的 AJAX 端点；legacy 端点作为删除的兜底
AJAX_PATH = "/hz/mycd/digital-console/ajax"
LEGACY_AJAX_PATH = "/hz/mycd/ajax"
PDOCS_PAGE = "/hz/mycd/digital-console/contentlist/pdocsAll/dateDsc/"

CSRF_PATTERNS = [
    # JS 变量赋值风格：csrfToken = "..." / csrfToken":"..."（MYCD ajax 用这个）
    re.compile(r'csrfToken\s*=\s*"([^"]+)"'),
    re.compile(r'"csrfToken"\s*:\s*"([^"]+)"'),
    re.compile(r"""csrfToken["']?\s*[:=]\s*["']([^"']+)["']"""),
    re.compile(r"""name=["']csrfToken["']\s+value=["']([^"']+)["']"""),
]


class KindleClient:
    def __init__(self, domain: str, cookies: str):
        self.origin = f"https://www.amazon.{domain}"
        self.cookies = cookies
        self.csrf: str | None = None

    # ---- 低层请求 -------------------------------------------------------
    def _request(self, path: str, form: dict | None = None):
        url = self.origin + path
        headers = {
            "User-Agent": UA,
            "Cookie": self.cookies,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = self.origin
            headers["Referer"] = self.origin + PDOCS_PAGE
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        return urllib.request.urlopen(req, timeout=60)

    def _post_ajax(self, path: str, form: dict):
        """POST 并解析 JSON；检测登录重定向。"""
        resp = self._request(path, form)
        final_url = resp.geturl()
        if "/ap/signin" in final_url:
            print("ERROR: cookies 已失效（Amazon 把请求重定向到了登录页）", file=sys.stderr)
            sys.exit(2)
        body = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            snippet = body[:200].replace("\n", " ")
            print(f"ERROR: 响应不是 JSON: {snippet}", file=sys.stderr)
            sys.exit(4)

    # ---- 高层操作 -------------------------------------------------------
    def fetch_csrf(self) -> str:
        resp = self._request(PDOCS_PAGE)
        if "/ap/signin" in resp.geturl():
            print("ERROR: cookies 已失效（被重定向到登录页）", file=sys.stderr)
            sys.exit(2)
        html = resp.read().decode("utf-8", "replace")
        # 页面上有两种 csrfToken："2@" 开头的是商店前端(aapi)的，对 mycd ajax 无效；
        # MYCD 接口要的是页面里 `csrfToken = "..."`（JS 变量赋值）风格的经典 token。
        candidates = []
        for pattern in CSRF_PATTERNS:
            candidates += pattern.findall(html)
        preferred = [t for t in candidates if not t.startswith("2@")]
        token = preferred[0] if preferred else (candidates[0] if candidates else None)
        if token:
            self.csrf = token
            return token
        print("ERROR: 页面里没找到 csrfToken，页面结构可能变了", file=sys.stderr)
        sys.exit(4)

    def list_pdocs(self) -> list:
        """返回按获取时间降序的个人文档列表 [{asin,title,acquiredTime}, ...]"""
        if not self.csrf:
            self.fetch_csrf()
        activity_input = {
            "contentType": "KindlePDoc",
            "contentCategoryReference": "pdocs",
            "itemStatusList": ["Active"],
            "showSharedContent": True,
            "fetchCriteria": {
                "sortOrder": "DESCENDING",
                "sortIndex": "DATE",
                "startIndex": 0,
                "batchSize": 200,
                "totalContentCount": -1,
            },
            "surfaceType": "LargeDesktop",
        }
        items: list = []
        start = 0
        while True:
            activity_input["fetchCriteria"]["startIndex"] = start
            data = self._post_ajax(AJAX_PATH, {
                "clientId": "MYCD_WebService",
                "activity": "GetContentOwnershipData",
                "activityInput": json.dumps(activity_input),
                "csrfToken": self.csrf,
            })
            page = data.get("items") or data.get("GetContentOwnershipData", {}).get("items")
            if data.get("success") is False or not isinstance(page, list):
                print(f"ERROR: 列表接口返回异常: {json.dumps(data)[:300]}", file=sys.stderr)
                sys.exit(4)
            items += page
            if len(page) < 200:
                break
            start += 200
            time.sleep(1 + random.random())
        return items

    def delete_pdocs(self, asins: list[str]) -> dict[str, bool]:
        """删除个人文档，返回 {asin: 是否成功}。"""
        if not self.csrf:
            self.fetch_csrf()
        asin_details = {asin: {"category": "KindlePDoc"} for asin in asins}
        activity_input = json.dumps({"asinDetails": asin_details})

        # 优先走新版 digital-console 端点
        data = self._post_ajax(AJAX_PATH, {
            "clientId": "MYCD_WebService",
            "activity": "DeleteContent",
            "activityInput": activity_input,
            "csrfToken": self.csrf,
        })
        result = data.get("DeleteContent", {}).get("resultMap")

        # 兜底：旧版端点（data 参数风格）
        if result is None:
            time.sleep(1 + random.random())
            data = self._post_ajax(LEGACY_AJAX_PATH, {
                "data": json.dumps({"param": {"DeleteContent": {"asinDetails": asin_details}}}),
                "csrfToken": self.csrf,
            })
            result = data.get("DeleteContent", {}).get("resultMap")

        if result is None:
            print(f"ERROR: 删除接口响应无法解析: {json.dumps(data)[:300]}", file=sys.stderr)
            sys.exit(4)
        return {asin: bool(ok) for asin, ok in result.items()}


def _load_config(raw: str) -> tuple[dict, bool]:
    """解析配置；JSON 非法时降级为逐字段正则提取（cookies 字段跳过，见 cookies.txt）。"""
    try:
        return json.loads(raw), True
    except json.JSONDecodeError:
        cfg = {}
        for key, pattern in [
            ("domain", r'"domain"\s*:\s*"([^"]*)"'),
            ("keep", r'"keep"\s*:\s*(\d+)'),
            ("deleteBatch", r'"deleteBatch"\s*:\s*(true|false)'),
        ]:
            m = re.search(pattern, raw)
            if m:
                cfg[key] = m.group(1)
        return cfg, False


def main():
    parser = argparse.ArgumentParser(description="清理 Kindle 个人文档，仅保留最近 N 个")
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认只干跑列出）")
    parser.add_argument("--keep", type=int, default=3, help="保留最近的文档数（默认 3）")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="config.json 路径")
    parser.add_argument("--single-asin", help="删除指定 ASIN（调试用，忽略 --keep）")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"ERROR: 找不到配置文件 {cfg_path}", file=sys.stderr)
        sys.exit(1)
    raw_cfg = cfg_path.read_text()
    cfg, cfg_ok = _load_config(raw_cfg)
    if not cfg_ok:
        print(
            f"WARNING: {cfg_path.name} 不是合法 JSON（多半是 cookies 里有未转义的引号），\n"
            "  已降级为正则提取字段；建议改用 cookies.txt 存 Cookie（纯文本，无转义问题）",
            file=sys.stderr,
        )
    # cookies 优先级：cookies.txt > config["cookies"]（纯文本文件不会踩 JSON 转义的坑）
    cookies = (cfg_path.parent / "cookies.txt").read_text().strip() if (cfg_path.parent / "cookies.txt").exists() else str(cfg.get("cookies", "")).strip()
    if not cookies:
        print(
            "ERROR: 没有找到 cookies。请把浏览器复制的整串 Cookie 粘到\n"
            "  同目录 cookies.txt 文件里（推荐），或 config.json 的 \"cookies\" 字段\n"
            "（粘进 JSON 时 Cookie 里的双引号必须转义成 \\\"，否则解析会失败）",
            file=sys.stderr,
        )
        sys.exit(1)
    domain = cfg.get("domain", "com")  # com / cn / de / co.uk / co.jp ...
    # CLI 参数优先于配置文件
    keep = args.keep if args.keep != 3 else int(cfg.get("keep", 3))
    delete_batch = bool(cfg.get("deleteBatch", False))  # True=一次请求删一批；False=逐个删更稳妥

    client = KindleClient(domain, cookies)

    if args.single_asin:
        to_delete, titles = [args.single_asin], {args.single_asin: "(手动指定)"}
    else:
        print("拉取个人文档列表 ...")
        items = client.list_pdocs()
        print(f"共 {len(items)} 个个人文档，保留最新 {keep} 个")
        titles = {}
        for it in items:
            titles[it.get("asin", "")] = it.get("title", "(无标题)")
        to_delete = [it.get("asin") for it in items[keep:] if it.get("asin")]

    if not to_delete:
        print("没有需要删除的文档，完成。")
        return

    print(f"\n将删除 {len(to_delete)} 个：")
    for asin in to_delete:
        print(f"  - {titles.get(asin, '?')}  [{asin}]")

    if not args.apply:
        print("\n[dry-run] 未执行删除。确认无误后加 --apply 运行。")
        return

    failed = []
    if delete_batch:
        result = client.delete_pdocs(to_delete)
        for asin, ok in result.items():
            print(f"  删除 {titles.get(asin, asin)}: {'OK' if ok else 'FAILED'}")
            if not ok:
                failed.append(asin)
    else:
        for i, asin in enumerate(to_delete):
            if i > 0:
                time.sleep(2 + random.random() * 3)  # 2~5s 随机间隔，避免触发风控
            result = client.delete_pdocs([asin])
            ok = result.get(asin, False)
            print(f"  删除 {titles.get(asin, asin)}: {'OK' if ok else 'FAILED'}")
            if not ok:
                failed.append(asin)

    if failed:
        print(f"\nWARNING: {len(failed)} 个删除失败: {failed}", file=sys.stderr)
        sys.exit(3)
    print("\n全部删除成功，完成。")


if __name__ == "__main__":
    main()
