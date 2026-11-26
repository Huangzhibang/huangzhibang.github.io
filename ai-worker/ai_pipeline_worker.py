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
import time
import urllib.error
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


def one_round():
    rows, err = pending_tasks()
    if err:
        log("查询失败：%s" % err)
        return 0
    if not rows:
        return 0
    moved = 0
    for row in rows:
        tid = row.get("id")
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
