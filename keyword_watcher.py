#!/usr/bin/env python3
"""網站關鍵字監控：當指定網頁出現「新的」關鍵字內容時，推播通知到手機（ntfy）。

只用 Python 標準函式庫，不需安裝任何套件。
平常由 GitHub Actions 每 5 分鐘自動執行，結果寫進 docs/data.json 給網頁顯示。

推播頻道名稱寫在 config.json 的 ntfy_topic（也可用環境變數 NTFY_TOPIC 覆蓋）。
手機用 Safari 打開 https://ntfy.sh/app 加入主畫面、訂閱同一個頻道就會收到通知，不用安裝 App。

用法：
    python3 keyword_watcher.py --test-notify   # 推播一則測試通知
    python3 keyword_watcher.py --once          # 檢查一次就結束（GitHub Actions 用這個）
    python3 keyword_watcher.py                 # 持續執行，每隔 interval_minutes 檢查一次
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
DATA_PATH = BASE_DIR / "docs" / "data.json"   # 給網頁顯示用
CONTEXT_CHARS = 60      # 關鍵字前後各擷取多少字當作上下文
MAX_KNOWN = 2000        # 每個網站最多記住幾筆已通知過的內容
MAX_ITEMS = 100         # 網頁上最多顯示幾筆
MAX_PUSHES = 5          # 一次新內容太多時，超過這個數量就合併成一則通知


class TextExtractor(HTMLParser):
    """把 HTML 轉成純文字，略過 script/style，並保留連結網址。"""

    SKIP_TAGS = {"script", "style", "noscript", "head"}
    BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
                  "article", "section", "td", "a", "span", "title"}

    def __init__(self):
        super().__init__()
        self.lines = []          # [(text, link)]
        self._buf = []
        self._skip = 0
        self._link = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip += 1
        if tag in self.BLOCK_TAGS:
            self._flush()
        if tag == "a":
            self._link = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in self.BLOCK_TAGS:
            self._flush()
        if tag == "a":
            self._link = None

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text:
            self.lines.append((text, self._link))
        self._buf = []

    def close(self):
        super().close()
        self._flush()


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    if urlsplit(url).hostname == "www.ptt.cc":
        headers["Cookie"] = "over18=1"  # 略過 PTT 部分看板的「是否已滿 18 歲」頁面
    with urlopen(Request(url, headers=headers), timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def find_matches(html, keywords, base_url):
    """回傳 [{keyword, snippet, link}]，每筆是一段含關鍵字的文字。"""
    parser = TextExtractor()
    parser.feed(html)
    parser.close()

    matches, seen = [], set()
    for text, link in parser.lines:
        for kw in keywords:
            for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
                start = max(0, m.start() - CONTEXT_CHARS)
                snippet = text[start:m.end() + CONTEXT_CHARS]
                if snippet in seen:
                    continue
                seen.add(snippet)
                if link and not link.startswith(("http://", "https://")):
                    link = urljoin(base_url, link)
                matches.append({"keyword": kw, "snippet": snippet, "link": link})
    return matches


def match_id(url, match):
    # 有連結就用連結辨識（文章標題旁的推文數等小變動不會被當成新內容）
    key = match["link"] or match["snippet"]
    return hashlib.sha1(f"{url}|{match['keyword']}|{key}".encode()).hexdigest()


NTFY_SERVER = "https://ntfy.sh"


def notify(topic, title, message, click=None):
    """透過 ntfy 推播到手機。"""
    payload = {"topic": topic, "title": title, "message": message, "tags": ["bell"]}
    if click:
        payload["click"] = click
    req = Request(NTFY_SERVER, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30):
        pass


def send_notifications(topic, new_items):
    if len(new_items) > MAX_PUSHES:
        kws = "、".join(sorted({m["keyword"] for _, m in new_items}))
        notify(topic, f"發現 {len(new_items)} 筆新內容：{kws}",
               "\n".join(f"・{m['snippet']}" for _, m in new_items[:10]))
        return
    for url, m in new_items:
        notify(topic, f"新內容：{m['keyword']}", m["snippet"], m["link"] or url)


def check_once(cfg, state):
    """檢查所有網站一次，推播並回傳新出現的 [(url, match)]。"""
    new_items = []
    updated = {}
    for site in cfg["sites"]:
        url, keywords = site["url"], site["keywords"]
        try:
            html = fetch(url)
        except Exception as ex:
            log(f"抓取失敗 {url}: {ex}")
            continue

        matches = find_matches(html, keywords, url)
        known = state.get(url, [])
        first_run = url not in state
        current = [match_id(url, m) for m in matches]
        known_set = set(known)
        fresh = [m for m, mid in zip(matches, current) if mid not in known_set]
        # 新的放前面，舊紀錄保留到上限為止
        updated[url] = list(dict.fromkeys(current + known))[:MAX_KNOWN]

        if first_run and not cfg.get("notify_on_first_run", False):
            log(f"{url}：首次執行，記錄現有 {len(matches)} 筆作為基準，不通知")
            continue
        log(f"{url}：共 {len(matches)} 筆符合，其中新出現 {len(fresh)} 筆")
        new_items += [(url, m) for m in fresh]

    if new_items:
        send_notifications(cfg["ntfy_topic"], new_items)
        log(f"已推播通知（{len(new_items)} 筆）")

    # 推播成功才記錄為「已通知」，失敗的下次會再推
    state.update(updated)
    write_json(STATE_PATH, state)
    update_page_data(cfg, new_items)
    return new_items


def update_page_data(cfg, new_items):
    """更新網頁資料。沒有新內容時一天只寫一次，避免每 5 分鐘產生一個 commit。"""
    data = load_json(DATA_PATH, {"items": []})
    now = datetime.now(timezone.utc)
    sites = [{"url": s["url"], "keywords": s["keywords"]} for s in cfg["sites"]]
    unchanged = data.get("sites") == sites and data.get("ntfy_topic") == cfg["ntfy_topic"]
    if not new_items and unchanged and data.get("checked_at", "")[:10] == now.date().isoformat():
        return
    found_at = now.isoformat(timespec="seconds")
    items = [{"found_at": found_at, "source": url, **m} for url, m in new_items]
    data.update(checked_at=found_at, ntfy_topic=cfg["ntfy_topic"], sites=sites,
                items=(items + data["items"])[:MAX_ITEMS])
    write_json(DATA_PATH, data)


def main():
    ap = argparse.ArgumentParser(description="網站關鍵字監控並推播到手機")
    ap.add_argument("--once", action="store_true", help="只檢查一次")
    ap.add_argument("--test-notify", action="store_true", help="推播一則測試通知")
    args = ap.parse_args()

    if not CONFIG_PATH.exists():
        sys.exit(f"找不到設定檔 {CONFIG_PATH}")
    cfg = load_json(CONFIG_PATH, {})
    cfg["ntfy_topic"] = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    if not cfg["ntfy_topic"]:
        sys.exit("config.json 沒有設定 ntfy_topic，無法推播")

    if args.test_notify:
        notify(cfg["ntfy_topic"], "關鍵字監控測試", "如果你看到這則通知，代表手機推播設定正確 🎉")
        log("測試通知已送出")
        return
    state = load_json(STATE_PATH, {})
    if args.once:
        check_once(cfg, state)
        return

    interval = cfg.get("interval_minutes", 5) * 60
    log(f"開始監控，每 {interval // 60} 分鐘檢查一次（Ctrl+C 停止）")
    while True:
        try:
            check_once(cfg, state)
        except Exception as ex:
            log(f"發生錯誤：{ex}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
