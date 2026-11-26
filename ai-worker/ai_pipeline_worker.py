# -*- coding: utf-8 -*-
"""
运动健康平台 · AI 流水线守护进程
=================================
作用：不依赖任何人打开网页，持续把云端待处理任务推进一步。
     审批 → 动作记录 → 评分 全部调用数据库函数 wb_ai_advance 完成，
     该函数以数据库属主身份执行，任何客户端都无法伪造分数。

为什么可以匿名调用：
     函数内部只做「单向前进 + 服务端重算分数」，不接受外部传入的分值，
     因此公开前端密钥即可，不需要把本人账号密码放在这台机器上。

用法：
     python ai_pipeline_worker.py          # 前台常驻（可看日志）
     pythonw ai_pipeline_worker.py         # 无窗口后台（开机自启用）
"""
import json
import os
import re
import time
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "ai_pipeline_worker.log")

CLOUD_ENDPOINT = os.environ.get("CLOUD_ENDPOINT", "https://sport-health-monitor.app.workbuddy.host").rstrip("/")
PUBLISHABLE_KEY = os.environ.get("PUBLISHABLE_KEY", "wbpk_eRW5YZcLuWnFOWcWtaQ0dz_jMDNlkkCMBBBQDtfXGhPvf6eq1s1DiU5")
REST = CLOUD_ENDPOINT + "/.cloud/database/rest"

POLL_SECONDS = int(os.environ.get("AI_WORKER_POLL", "20"))
MAX_BATCH = 20


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def call(method, path, payload=None):
    url = REST + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-wb-webapp-access-key", PUBLISHABLE_KEY)
    req.add_header("Origin", CLOUD_ENDPOINT)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return (json.loads(body) if body else None), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:
        return None, str(e)


def pending_tasks():
    q = ("/wf_tasks?select=id,status,name"
         "&status=in.(pending_approval,pending_record,pending_analysis)"
         "&order=created.asc&limit=%d" % MAX_BATCH)
    rows, err = call("GET", q)
    return (rows or []), err


def advance(task_id):
    return call("POST", "/rpc/wb_ai_advance", {"p_id": task_id})


# ---------------------------------------------------------------- 联网补充知识
SPORTS = [
    ("squat", "深蹲", ("深蹲", "squat")),
    ("deadlift", "硬拉", ("硬拉", "deadlift")),
    ("plank", "平板支撑", ("平板", "plank")),
    ("wallsit", "靠墙静蹲", ("靠墙", "静蹲", "wall")),
    ("shoulder", "肩外展", ("肩", "外展", "shoulder")),
    ("lunge", "弓步蹲", ("弓步", "lunge")),
    ("pushup", "俯卧撑", ("俯卧撑", "push")),
    ("run", "跑步", ("跑", "run")),
]


def guess_sport(name):
    low = (name or "").lower()
    for key, cn, kws in SPORTS:
        for kw in kws:
            if kw in low:
                return key, cn
    return "generic", "通用动作"


def _http(url, timeout=12, headers=None):
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; SportHealthBot/1.0)")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def search_wikipedia(cn):
    """中文维基：稳定的 JSON 接口，用来兜底。"""
    q = urllib.parse.quote(cn + " 动作")
    url = ("https://zh.wikipedia.org/w/api.php?action=query&list=search&format=json"
           "&srlimit=2&srsearch=" + q)
    data = json.loads(_http(url, timeout=10))
    out = []
    for item in (data.get("query", {}) or {}).get("search", []) or []:
        title = item.get("title", "")
        snippet = _clean(item.get("snippet", ""))
        if not title:
            continue
        out.append({
            "title": "%s · 维基百科：%s" % (cn, title),
            "snippet": snippet[:180] or ("%s 的基础动作说明与常见问题" % cn),
            "url": "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
        })
    return out


def search_ddg(query):
    """DuckDuckGo HTML 版，无需密钥。"""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    html = _http(url, timeout=12)
    out = []
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'[\s\S]{0,800}?<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)</a>',
            html):
        href, title, snippet = m.group(1), _clean(m.group(2)), _clean(m.group(3))
        if not href.startswith("http"):
            continue
        out.append({"title": title[:80], "snippet": snippet[:200], "url": href})
        if len(out) >= 3:
            break
    return out


def search_baike(cn):
    """百度百科：国内可直连，取页面描述作为一条资料。"""
    url = "https://baike.baidu.com/item/" + urllib.parse.quote(cn)
    html = _http(url, timeout=9)
    m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html)
    if not m:
        return []
    desc = _clean(m.group(1))
    if len(desc) < 10:
        return []
    return [{"title": "%s · 百度百科" % cn, "snippet": desc[:200], "url": url}]


def web_lookup(cn):
    """依次尝试多个来源，任何一路成功即可。"""
    attempts = [
        ("baike", lambda: search_baike(cn)),
        ("wikipedia", lambda: search_wikipedia(cn)),
        ("duckduckgo", lambda: search_ddg("%s 动作要领 常见错误" % cn)),
        ("duckduckgo2", lambda: search_ddg("%s 运动分析 关节角度" % cn)),
        ("duckduckgo3", lambda: search_ddg("%s 教学视频 动作示范" % cn)),
    ]
    errors = []
    for name, fn in attempts:
        try:
            rows = fn()
            if rows:
                log("联网检索 %s：%s 命中 %d 条" % (cn, name, len(rows)))
                return rows, errors
        except Exception as e:
            errors.append("%s: %s" % (name, e))
            log("联网检索 %s 的 %s 失败：%s" % (cn, name, e))
    return [], errors


def kb_exists(entry_id):
    rows, err = call("GET", "/role_kb?select=id&id=eq." + urllib.parse.quote(entry_id))
    return bool(rows), err


def kb_insert(row):
    return call("POST", "/role_kb", row)


def enrich_pending(task_id, name):
    """审核前把该运动的网络资料补进审核员 / 分析员的知识库。"""
    sport, cn = guess_sport(name)
    today = datetime.now().strftime("%Y%m%d")
    marker = "webmark-%s-%s" % (sport, today)
    found, _ = kb_exists(marker)
    if found:
        return 0
    rows, errors = web_lookup(cn)
    now = datetime.now().isoformat()
    added = 0
    for r in rows:
        h = hashlib.md5(r["url"].encode("utf-8")).hexdigest()[:8]
        for role, kind in (("approver", "reference"), ("analyst", "cue")):
            eid = "web-%s-%s-%s" % (role[0], sport, h)
            exists, _ = kb_exists(eid)
            if exists:
                continue
            body = r["snippet"] or r["title"]
            if role == "analyst":
                body = "%s：%s" % (cn, body)
            res, err = kb_insert({
                "id": eid, "role": role, "kind": kind,
                "title": r["title"][:110], "body": body[:400],
                "src": r["url"], "tags": [sport, "web"],
                "weight": 12 if role == "approver" else 7, "uses": 0,
                "created": now, "updated_at": now,
            })
            if not err:
                added += 1
    # 当天标记，避免同一运动被反复联网
    kb_insert({"id": marker, "role": "approver", "kind": "marker",
               "title": "%s 联网检索记录" % cn,
               "body": "命中 %d 条；失败 %s" % (len(rows), ("；".join(errors) or "无")),
               "src": "", "tags": [sport, "web"], "weight": 0, "uses": 0,
               "created": now, "updated_at": now})
    if errors and not rows:
        # 把问题本身记进分析员错误库，下次可查
        kb_insert({"id": "err-web-%s-%s" % (sport, today), "role": "analyst", "kind": "error",
                   "title": "联网检索失败：%s" % cn, "body": "；".join(errors)[:700],
                   "src": "", "tags": [sport, "error"], "weight": 0, "uses": 0,
                   "created": now, "updated_at": now})
    return added


def one_round():
    rows, err = pending_tasks()
    if err:
        log("查询失败：%s" % err)
        return 0
    if not rows:
        return 0
    moved = 0
    enriched = {}
    for row in rows:
        tid = row.get("id")
        # 审核前联网补充该运动的知识（同一运动当天只查一次）
        if row.get("status") == "pending_approval":
            sport, _cn = guess_sport(row.get("name"))
            if sport not in enriched:
                try:
                    enriched[sport] = enrich_pending(tid, row.get("name"))
                    if enriched[sport]:
                        log("「%s」知识库补充 %d 条" % (_cn, enriched[sport]))
                except Exception as e:
                    enriched[sport] = 0
                    log("知识库补充异常：%s" % e)
        for _ in range(3):
            res, e = advance(tid)
            if e:
                log("推进 %s 失败：%s" % (tid, e))
                break
            step = (res or {}).get("step")
            if not step or step == "none":
                break
            moved += 1
            log("推进 %s → %s（%s）" % (tid, (res or {}).get("status"), step))
            if (res or {}).get("status") in ("done", "rejected"):
                break
    return moved


def main():
    if os.environ.get("AI_WORKER_ONCE") == "1":   # GitHub Actions 等定时任务：跑一轮即退出
        moved = one_round()
        log("单轮执行完成，推进 %s 步" % moved)
        return
    log("AI 流水线守护启动：轮询间隔 %ss，云端 %s" % (POLL_SECONDS, CLOUD_ENDPOINT))
    while True:
        try:
            one_round()
        except KeyboardInterrupt:
            log("手动停止")
            return
        except Exception as e:
            log("异常：%s" % e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
