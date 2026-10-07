"""網頁內建推播（Web Push）：把通知送到「加入主畫面」並按過「開啟通知」的手機。

訂閱流程：手機在監控網頁按「開啟通知」→ 網頁把訂閱資料送到 ntfy 中繼頻道
→ 每次檢查時由 collect_subscriptions() 收下來存進 subscriptions.json。

需要套件 pywebpush，以及環境變數 VAPID_PRIVATE_KEY_FILE（私鑰檔路徑，存在 GitHub Secrets）。
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

BASE_DIR = Path(__file__).resolve().parent
SUBS_PATH = BASE_DIR / "subscriptions.json"
RELAY_SERVER = "https://ntfy.sh"
# 推播伺服器要求的聯絡資訊；用網站網域，不放信箱
VAPID_CLAIMS = {"sub": "https://samplepleple.github.io"}
MAX_SUBS = 50
# 只接受各大瀏覽器官方的推播伺服器，避免有人塞入奇怪的網址
PUSH_HOSTS = ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com",
              "notify.windows.com")


def load_subs():
    if SUBS_PATH.exists():
        return json.loads(SUBS_PATH.read_text(encoding="utf-8"))
    return {"relay_since": "12h", "subscriptions": []}


def save_subs(data):
    SUBS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _valid(sub):
    try:
        host = urlsplit(sub["endpoint"]).hostname or ""
        return (sub["endpoint"].startswith("https://")
                and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)
                and isinstance(sub["keys"]["p256dh"], str) and isinstance(sub["keys"]["auth"], str))
    except (KeyError, TypeError):
        return False


def collect_subscriptions(relay_topic, log):
    """從中繼頻道收下新的訂閱，回傳新加入的訂閱清單。"""
    data = load_subs()
    url = f"{RELAY_SERVER}/{relay_topic}/json?poll=1&since={data['relay_since']}"
    with urlopen(url, timeout=30) as resp:
        lines = resp.read().decode("utf-8").splitlines()

    known = {s["endpoint"] for s in data["subscriptions"]}
    added = []
    for line in lines:
        msg = json.loads(line)
        if msg.get("event") != "message":
            continue
        data["relay_since"] = msg["id"]
        try:
            sub = json.loads(msg.get("message", ""))
        except json.JSONDecodeError:
            continue
        if not _valid(sub) or sub["endpoint"] in known or len(data["subscriptions"]) >= MAX_SUBS:
            continue
        sub = {"endpoint": sub["endpoint"], "keys": {k: sub["keys"][k] for k in ("p256dh", "auth")}}
        data["subscriptions"].append(sub)
        known.add(sub["endpoint"])
        added.append(sub)
    save_subs(data)
    if added:
        log(f"新增 {len(added)} 支手機的通知訂閱（共 {len(data['subscriptions'])} 支）")
    return added


def send(title, body, url=None, only=None):
    """推播給所有訂閱（或只推給 only 裡的訂閱）；失效的訂閱會自動移除。回傳成功數量。"""
    from pywebpush import WebPushException, webpush

    key_file = os.environ.get("VAPID_PRIVATE_KEY_FILE")
    if not key_file:
        raise RuntimeError("沒有設定 VAPID_PRIVATE_KEY_FILE，無法推播")
    data = load_subs()
    targets = only if only is not None else data["subscriptions"]
    payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
    sent, gone = 0, set()
    for sub in targets:
        try:
            webpush(sub, payload, vapid_private_key=key_file, vapid_claims=dict(VAPID_CLAIMS), ttl=86400)
            sent += 1
        except WebPushException as ex:
            if ex.response is not None and ex.response.status_code in (404, 410):
                gone.add(sub["endpoint"])  # 使用者關閉通知或刪掉主畫面圖示
            else:
                print(f"推播失敗（{sub['endpoint'][:40]}…）：{ex}", flush=True)
    if gone:
        data["subscriptions"] = [s for s in data["subscriptions"] if s["endpoint"] not in gone]
        save_subs(data)
    return sent
