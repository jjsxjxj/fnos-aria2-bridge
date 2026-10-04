#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aria2bridge — Aria2 JSON-RPC 协议桥接 飞牛「下载中心」

原理：
  1. 在本机(飞牛主机)监听 HTTP 端口(默认 6800)，伪装成标准 Aria2 JSON-RPC 服务；
  2. DBO(DB_Online) 等支持 Aria2 下载器的工具把任务推到这里；
  3. 本服务通过随包内置的 trim-cli 二进制连接飞牛 WebSocket 接口
     (appcgi.downloadcenter.*)，把任务转发到飞牛「下载中心」；
  4. tellActive/tellStatus 等查询映射回 Aria2 协议字段，供客户端显示进度。

仅使用 Python 标准库。运行于飞牛 fpk 应用环境(也兼容本地 python3 直接运行测试)。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
CFG = {}                 # config.json 内容
CFG_PATH = ""            # config.json 路径
VAR_DIR = ""             # 运行数据目录(日志/会话 HOME)
TRIM_BIN = ""            # trim-cli 可执行文件路径
TRIM_LOCK = threading.Lock()
RECENT = []              # 事件记录(内存,持久化到 VAR_DIR/recent.json,启动时加载)
DEFAULT_EVENT_DAYS = 30   # 事件保留天数(可用 config: event_retention_days 覆盖)
DEFAULT_EVENT_MAX = 1000  # 事件条数上限(可用 config: event_max_count 覆盖)
DEFAULT_SAVE_DIR_CACHE = {"v": "", "at": 0}

ARIA2_VERSION = "1.37.0"
ARIA2_FEATURES = ["bridged-to-fnos-downloadcenter"]


LOG_ROTATE_BYTES = 5 * 1024 * 1024  # bridge.log 超 5MB 滚动为 .1,只保留一代
LOG_TRIM_CHECK = {"at": 0}          # 日志按天清理的节流(每 6 小时检查一次)


def trim_log_by_age():
    """按保留天数(默认 30 天,config: log_retention_days)清理运行日志。

    bridge.log 按行首时间戳过滤;滚动归档 .1 按文件修改时间超龄即删。
    """
    try:
        days = max(int(CFG.get("log_retention_days", 30)), 1)
    except Exception:  # noqa: BLE001
        days = 30
    cutoff = time.time() - days * 86400
    if not VAR_DIR:
        return
    p = os.path.join(VAR_DIR, "bridge.log")
    try:
        p1 = p + ".1"
        if os.path.exists(p1) and os.path.getmtime(p1) < cutoff:
            os.remove(p1)
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        keep = []
        for ln in lines:
            try:
                ts = int(time.mktime(time.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")))
            except Exception:  # noqa: BLE001
                ts = None  # 无时间戳的行(如堆栈续行)保留
            if ts is None or ts >= cutoff:
                keep.append(ln)
        if len(keep) != len(lines):
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(keep)
    except Exception:  # noqa: BLE001
        pass


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    try:
        print(line, flush=True)
        if VAR_DIR:
            os.makedirs(VAR_DIR, exist_ok=True)
            now = time.time()
            if now - LOG_TRIM_CHECK["at"] > 6 * 3600:  # 每 6 小时按天清理一次
                LOG_TRIM_CHECK["at"] = now
                trim_log_by_age()
            p = os.path.join(VAR_DIR, "bridge.log")
            try:
                if os.path.exists(p) and os.path.getsize(p) > LOG_ROTATE_BYTES:
                    os.replace(p, p + ".1")  # 滚动,旧日志保留一代
            except Exception:
                pass
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def _event_ts(e):
    """取事件时间戳;老格式无 ts 字段时从 time 字符串解析"""
    try:
        ts = int(e.get("ts") or 0)
    except Exception:  # noqa: BLE001
        ts = 0
    if not ts and e.get("time"):
        try:
            ts = int(time.mktime(time.strptime(e["time"], "%Y-%m-%d %H:%M:%S")))
        except Exception:  # noqa: BLE001
            ts = 0
    return ts


def event_conf():
    """事件保留策略:(天数, 条数上限),可用 config.json 覆盖"""
    try:
        days = int(CFG.get("event_retention_days", DEFAULT_EVENT_DAYS))
    except Exception:  # noqa: BLE001
        days = DEFAULT_EVENT_DAYS
    try:
        mx = int(CFG.get("event_max_count", DEFAULT_EVENT_MAX))
    except Exception:  # noqa: BLE001
        mx = DEFAULT_EVENT_MAX
    return max(days, 1), max(mx, 10)


def prune_recent():
    days, mx = event_conf()
    cutoff = time.time() - days * 86400
    RECENT[:] = [e for e in RECENT if _event_ts(e) >= cutoff][:mx]


def save_recent():
    try:
        if VAR_DIR:
            with open(os.path.join(VAR_DIR, "recent.json"), "w", encoding="utf-8") as f:
                json.dump(RECENT, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def event(kind, detail):
    """记录一条推送/控制事件;持久化保存,按保留天数/条数自动清理"""
    RECENT.insert(0, {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "ts": int(time.time()), "kind": kind, "detail": detail})
    prune_recent()
    save_recent()


def clear_recent():
    """手动清空全部事件记录(状态页「清空记录」按钮)"""
    RECENT[:] = []
    save_recent()


def apply_config(updates):
    """合并状态页提交的配置项 → 内存 CFG + config.json,并即时生效。

    只允许改有限字段;fnos 账号/密码/端口等不在此暴露。
    """
    if "secret" in updates:
        CFG["secret"] = str(updates["secret"] or "")
    if "default_save_dir" in updates:
        v = str(updates["default_save_dir"] or "").strip()
        if v:
            CFG["default_save_dir"] = v
        else:
            CFG.pop("default_save_dir", None)
        DEFAULT_SAVE_DIR_CACHE.update(v="", at=0)
    if "dir_map" in updates:
        dm = updates["dir_map"]
        if not isinstance(dm, list):
            raise ValueError("dir_map 必须是数组")
        clean = []
        for m in dm:
            if isinstance(m, dict) and str(m.get("from", "")).strip() and str(m.get("to", "")).strip():
                clean.append({"from": str(m["from"]).strip(), "to": str(m["to"]).strip()})
        CFG["dir_map"] = clean
    if "event_retention_days" in updates:
        try:
            CFG["event_retention_days"] = max(int(updates["event_retention_days"] or 30), 1)
        except Exception:  # noqa: BLE001
            pass
    if "event_max_count" in updates:
        try:
            CFG["event_max_count"] = max(int(updates["event_max_count"] or 1000), 10)
        except Exception:  # noqa: BLE001
            pass
    if "log_retention_days" in updates:
        try:
            CFG["log_retention_days"] = max(int(updates["log_retention_days"] or 30), 1)
        except Exception:  # noqa: BLE001
            pass
    if CFG_PATH:
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=1)
        try:
            os.chmod(tmp, 0o600)  # 配置含飞牛密码,保持仅所有者可读
        except Exception:  # noqa: BLE001
            pass
        os.replace(tmp, CFG_PATH)
    prune_recent()
    save_recent()


def load_recent():
    try:
        p = os.path.join(VAR_DIR, "recent.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                v = json.load(f)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# trim-cli 封装
# ---------------------------------------------------------------------------
def trim_base_args():
    host = CFG.get("fnos_host", "127.0.0.1")
    port = str(CFG.get("fnos_port", 5666))
    return [TRIM_BIN, "--host", host, "--port", port, "--scheme", "ws"]


def trim_run(args, timeout=30):
    """执行 trim-cli 子进程,返回 (rc, stdout, stderr)"""
    env = dict(os.environ)
    home = os.path.join(VAR_DIR, "home")
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["TRIM_CLI_SESSION_STORAGE"] = "file"
    try:
        p = subprocess.run(
            [str(a) for a in args],
            capture_output=True, timeout=timeout, env=env,
            text=True, encoding="utf-8", errors="replace",
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "trim-cli timeout"
    except Exception as e:  # noqa: BLE001
        import traceback
        log("trim_run 异常: %r args=%r" % (e, args))
        log(traceback.format_exc()[-800:])
        return 125, "", str(e)


def looks_like_auth_failure(out, err):
    blob = (out + " " + err)
    if "135168" in blob:
        return True
    for kw in ("登录态失效", "token", "login", "not login", "unauthor", "session"):
        if kw in blob.lower() or kw in blob:
            # 粗匹配,避免误判 login 成功输出
            if "succ" not in blob[:200]:
                return True
    return False


def trim_login():
    user = CFG.get("fnos_user", "")
    pwd = CFG.get("fnos_password", "")
    if not user or not pwd:
        return False, "未配置飞牛账号/密码"
    args = trim_base_args() + ["login", "-u", user, "-p", pwd]
    rc, out, err = trim_run(args, timeout=30)
    if rc == 0 and "fail" not in (out + err).lower()[:400]:
        log("trim-cli login ok (user=%s)" % user)
        return True, ""
    msg = (err or out or "").strip().splitlines()
    msg = msg[-1] if msg else "unknown"
    log("trim-cli login FAILED: %s" % msg)
    return False, msg


def trim_call(args, retry_auth=True, timeout=30):
    """带会话自愈的 trim-cli 调用;返回 (ok, stdout, stderr)"""
    with TRIM_LOCK:
        rc, out, err = trim_run(args, timeout=timeout)
        if rc != 0 and retry_auth and looks_like_auth_failure(out, err):
            log("检测到会话失效,尝试重新登录…")
            ok, msg = trim_login()
            if ok:
                rc, out, err = trim_run(args, timeout=timeout)
            else:
                return False, out, "relogin failed: " + msg
        if rc != 0:
            return False, out, err or ("exit %d" % rc)
        return True, out, err


def trim_json(args, timeout=30):
    ok, out, err = trim_call(args, timeout=timeout)
    if not ok:
        raise RuntimeError((err or out).strip()[:300])
    try:
        return json.loads(out)
    except Exception:
        raise RuntimeError("trim-cli 输出非 JSON: " + out[:200])


# ---------------------------------------------------------------------------
# 下载中心接口
# ---------------------------------------------------------------------------
def dc_query_tasks():
    data = trim_json(trim_base_args() + [
        "download", "request", "appcgi.downloadcenter.task.query",
        "--json", json.dumps({"init_flag": True, "state_filter": 65535}),
        "--yes",
    ])
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return tasks if isinstance(tasks, list) else []


def dc_default_save_dir(force=False):
    now = time.time()
    if not force and DEFAULT_SAVE_DIR_CACHE["v"] and now - DEFAULT_SAVE_DIR_CACHE["at"] < 600:
        return DEFAULT_SAVE_DIR_CACHE["v"]
    if CFG.get("default_save_dir"):
        DEFAULT_SAVE_DIR_CACHE.update(v=CFG["default_save_dir"], at=now)
        return CFG["default_save_dir"]
    try:
        out = trim_json(trim_base_args() + [
            "download", "request", "appcgi.downloadcenter.config.getDefaultSaveDir",
            "--json", "{}", "--yes",
        ])
        v = out if isinstance(out, str) else json.dumps(out)
        v = v.strip().strip('"')
        if v.startswith("/vol"):
            DEFAULT_SAVE_DIR_CACHE.update(v=v, at=now)
            return v
    except Exception as e:  # noqa: BLE001
        log("getDefaultSaveDir 失败: %s" % e)
    return "/vol1/Download"


def dc_add_uri(uri, save_dir):
    payload = {"uris": [uri], "save_dir": save_dir}
    out = trim_json(trim_base_args() + [
        "download", "request", "appcgi.downloadcenter.task.addUris",
        "--json", json.dumps(payload), "--yes",
    ])
    ids = None
    if isinstance(out, dict):
        ids = out.get("ids")
    elif isinstance(out, list):
        ids = out
    if not ids:
        raise RuntimeError("下载中心返回中无任务 id: " + json.dumps(out, ensure_ascii=False)[:200])
    return int(ids[0])


def dc_control(cmd, task_id):
    # 注意: trim-cli 的 pause/resume/retry/rm 均不接受 --yes(实测报
    # "unexpected argument '--yes'"),执行也无交互确认,故不加任何附加参数
    ok, out, err = trim_call(trim_base_args() + ["download", cmd, str(task_id)])
    return ok, (err or out).strip()[:200]


# ---------------------------------------------------------------------------
# 路径映射: Aria2 options.dir → 飞牛 /vol{n}/...
# ---------------------------------------------------------------------------
def map_dir(d):
    if d:
        d = str(d).strip()
        for m in CFG.get("dir_map", []):
            frm = str(m.get("from", ""))
            to = str(m.get("to", ""))
            if frm and (d == frm or d.startswith(frm.rstrip("/") + "/")):
                return d.replace(frm, to, 1) if d != frm else to
        if re.match(r"^/vol\d+/", d):
            return d
    return dc_default_save_dir()


# ---------------------------------------------------------------------------
# fnOS 任务 → Aria2 状态
# ---------------------------------------------------------------------------
def gid_of(task):
    try:
        return format(int(task.get("id")), "016x")
    except Exception:  # noqa: BLE001
        return str(task.get("id"))


def gid_to_id(gid):
    try:
        return int(str(gid), 16)
    except Exception:  # noqa: BLE001
        return str(gid)


_STATE_MAP = {
    "downloading": "active", "active": "active", "started": "active",
    "seeding": "complete",
    "completed": "complete", "complete": "complete", "finished": "complete", "done": "complete",
    "paused": "paused", "stopped": "paused",
    "waiting": "waiting", "queued": "waiting", "idle": "waiting",
    "error": "error", "failed": "error",
    "removed": "removed",
}


def aria_state(task):
    s = task.get("state")
    if isinstance(s, str):
        v = s.lower()
        if v in _STATE_MAP:
            return _STATE_MAP[v]
        if "download" in v or "active" in v or "start" in v:
            return "active"
        if "complet" in v or "finish" in v or "seed" in v or "done" in v:
            return "complete"
        if "paus" in v or "stop" in v:
            return "paused"
        if "wait" in v or "queue" in v or "idle" in v:
            return "waiting"
        if "error" in v or "fail" in v:
            return "error"
    elif isinstance(s, int):
        if s == 1:
            return "active"
        if s == 2:
            return "paused"
        if s == 3:
            return "complete"
        if s == 4:
            return "error"
        if s == 0:
            return "waiting"
    if s not in (None, ""):
        log("warn: 未识别 state=%r task=%s,按 active 处理" % (s, task.get("name")))
    return "active"


def to_aria_task(task):
    total = int(task.get("total_size") or 0)
    # 实测 fnOS 接口:已下载量在 dl_total_size;size 恒等于 total_size(总大小),
    # 不能用作进度,否则所有任务永远显示 100%
    done = int(task.get("dl_total_size") or 0)
    if not done:
        size = int(task.get("size") or 0)
        if 0 < size < total:  # 仅当 size < total 时才可能是已下载量(兼容假设)
            done = size
    if done > total > 0:
        done = total
    st = aria_state(task)
    return {
        "gid": gid_of(task),
        "status": st,
        "totalLength": str(total),
        "completedLength": str(done),
        "downloadSpeed": str(int(task.get("dl_speed") or 0)),
        "uploadSpeed": str(int(task.get("up_speed") or 0)),
        "dir": task.get("save_dir") or "",
        "connections": "1",
        "files": [{
            "path": (task.get("save_dir") or "").rstrip("/") + "/" + (task.get("name") or ""),
            "length": str(total),
            "completedLength": str(done),
            "uris": ([{"uri": str(task["uri"])}] if task.get("uri") else []),
        }],
        "errorCode": str(task.get("error_code") or "0"),
        "errorMessage": str(task.get("error_message") or "") or ("" if st != "error" else "下载任务出错"),
    }


# ---------------------------------------------------------------------------
# Aria2 JSON-RPC 方法实现
# ---------------------------------------------------------------------------
def check_token(params):
    """去掉并校验 token:xxx 参数;失败抛异常"""
    if params and isinstance(params[0], str) and params[0].startswith("token:"):
        tok = params[0][6:]
        params = params[1:]
        secret = CFG.get("secret", "")
        if secret and tok != secret:
            raise RpcError(1, "Token 验证失败")
    return params


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def m_getVersion(params):
    return {"version": ARIA2_VERSION, "enabledFeatures": ARIA2_FEATURES}


def m_getGlobalStat(params):
    tasks = dc_query_tasks()
    c = {}
    for t in tasks:
        s = aria_state(t)
        c[s] = c.get(s, 0) + 1
    return {
        "downloadSpeed": "0", "uploadSpeed": "0",
        "numActive": str(c.get("active", 0)),
        "numWaiting": str(c.get("waiting", 0) + c.get("paused", 0)),
        "numStopped": str(c.get("complete", 0) + c.get("error", 0) + c.get("removed", 0)),
        "numActiveTotal": str(c.get("active", 0)),
    }


def dc_find_recent_task(t0):
    """在任务列表里找 t0 前后新建的任务(响应超时后的"假失败"排查用)。

    飞牛 addUris 偶发响应慢(实测 9 秒):请求已送达、任务已创建,但 trim-cli
    先一步报 timed out。此时不能贸然重试,否则会建重复任务;先按 addition_time
    找刚建出来的任务,找到即视为成功。
    """
    try:
        tasks = dc_query_tasks()
    except Exception:  # noqa: BLE001
        return None
    best = None
    for t in tasks:
        try:
            at = int(t.get("addition_time") or 0)
        except Exception:  # noqa: BLE001
            continue
        if at >= t0 - 5 and (best is None or at > int(best.get("addition_time") or 0)):
            best = t
    return best


def m_addUri(params):
    uris = params[0] if params else None
    options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
    if not isinstance(uris, list) or not uris:
        raise RpcError(1, "uris 为空")
    uri = str(uris[0])
    save_dir = map_dir(options.get("dir"))
    event("push", "%s → %s" % (uri[:100], save_dir))
    t0 = int(time.time())
    try:
        tid = dc_add_uri(uri, save_dir)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "timed out" not in msg.lower() and "timeout" not in msg.lower():
            event("push-failed", msg[:200])
            raise RpcError(1, "转发到飞牛下载中心失败: %s" % e)
        # 响应超时:任务可能已创建(假失败),轮询确认(实测飞牛建任务最慢约 9 秒)
        log("addUris 响应超时,轮询确认任务是否已创建…")
        try:
            iv = max(float(CFG.get("timeout_poll_interval", 4)), 0.2)
        except Exception:  # noqa: BLE001
            iv = 4
        try:
            n_poll = int(CFG.get("timeout_poll_attempts", 3))
        except Exception:  # noqa: BLE001
            n_poll = 3
        for _ in range(n_poll):
            time.sleep(iv)
            t = dc_find_recent_task(t0)
            if t is not None:
                event("push-ok", "响应超时但任务已创建 id=%s(%s)" % (t.get("id"), t.get("name", "?")))
                return gid_of(t)
        # 轮询窗口(约 12 秒)内未出现新任务 → 判定首次请求确实失败,重试一次
        log("轮询未见新任务,重试 addUris 一次")
        try:
            tid = dc_add_uri(uri, save_dir)
        except Exception as e2:  # noqa: BLE001
            event("push-failed", str(e2)[:200])
            raise RpcError(1, "转发到飞牛下载中心失败: %s" % e2)
    event("push-ok", "任务 id=%s" % tid)
    return gid_of({"id": tid})


def _tell(filter_states):
    tasks = dc_query_tasks()
    return [to_aria_task(t) for t in tasks if aria_state(t) in filter_states]


def m_tellActive(params):
    return _tell({"active"})


def m_tellWaiting(params):
    return _tell({"waiting", "paused"})


def m_tellStopped(params):
    return _tell({"complete", "error", "removed"})


def m_tellStatus(params):
    gid = params[0] if params else ""
    tid = gid_to_id(gid)
    for t in dc_query_tasks():
        if str(t.get("id")) == str(tid):
            return to_aria_task(t)
    raise RpcError(1, "GID#%s is not found" % gid)


def _control(gid, cmd, label):
    tid = gid_to_id(gid)
    ok, msg = dc_control(cmd, tid)
    if not ok:
        log("%s 失败: gid=%s → %s" % (label, gid, msg))
        raise RpcError(1, "%s 失败: %s" % (label, msg))
    if not msg or msg.strip() in ("{}", '""'):  # trim-cli 成功时返回空 JSON
        msg = "成功"
    event(cmd, "gid=%s → %s" % (gid, msg))
    return gid


def m_pause(p):
    return _control(p[0], "pause", "暂停")


def m_unpause(p):
    return _control(p[0], "resume", "恢复")


def m_remove(p):
    return _control(p[0], "rm", "删除")


def m_removeDownloadResult(p):
    return "OK"


def m_getGlobalOption(p):
    return {
        "max-concurrent-downloads": "5", "max-overall-download-limit": "0",
        "max-overall-upload-limit": "0", "dir": dc_default_save_dir(),
        "listen-port": "6881-6999", "seed-ratio": "1.0",
    }


def m_changeGlobalOption(p):
    return "OK"


def m_changeOption(p):
    return "OK"


def m_getOption(p):
    return {"dir": dc_default_save_dir()}


METHODS = {
    "aria2.getVersion": m_getVersion,
    "aria2.getGlobalStat": m_getGlobalStat,
    "aria2.addUri": m_addUri,
    "aria2.tellActive": m_tellActive,
    "aria2.tellWaiting": m_tellWaiting,
    "aria2.tellStopped": m_tellStopped,
    "aria2.tellStatus": m_tellStatus,
    "aria2.pause": m_pause,
    "aria2.unpause": m_unpause,
    "aria2.forcePause": m_pause,
    "aria2.forceUnpause": m_unpause,
    "aria2.remove": m_remove,
    "aria2.forceRemove": m_remove,
    "aria2.removeDownloadResult": m_removeDownloadResult,
    "aria2.getGlobalOption": m_getGlobalOption,
    "aria2.changeGlobalOption": m_changeGlobalOption,
    "aria2.getOption": m_getOption,
    "aria2.changeOption": m_changeOption,
}


def dispatch(request):
    method = request.get("method", "")
    rid = request.get("id")
    if method == "system.multicall":
        result = []
        try:
            for group in request.get("params") or []:
                calls = group if isinstance(group, list) else [group]
                for call in calls:
                    if not isinstance(call, dict):
                        result.append({"code": 1, "message": "bad call entry"})
                        continue
                    try:
                        r = handle_call(call.get("methodName", ""), call.get("params") or [])
                        result.append([r])
                    except RpcError as e:
                        result.append({"code": e.code, "message": e.message})
        except Exception as e:  # noqa: BLE001
            result.append({"code": 1, "message": "multicall error: %s" % e})
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    try:
        result = handle_call(method, request.get("params") or [])
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except RpcError as e:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}}
    except Exception as e:  # noqa: BLE001
        log("rpc error: %s %s → %s" % (method, request.get("params"), e))
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": 1, "message": str(e)}}


def handle_call(method, params):
    if method not in METHODS:
        raise RpcError(1, "未实现的方法: %s" % method)
    params = check_token(list(params or []))
    return METHODS[method](params)


# ---------------------------------------------------------------------------
# 状态页
# ---------------------------------------------------------------------------
STATUS_HTML = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>下载中心接入Aria2 · 状态</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","Microsoft YaHei",-apple-system,sans-serif;
  background:linear-gradient(160deg,#eef2ff 0%,#f5f7fb 45%,#eefaf4 100%);
  min-height:100vh;color:#1e293b;padding:28px 16px}
.wrap{max-width:860px;margin:0 auto}
.hero{display:flex;align-items:center;gap:12px;margin:2px 4px 18px}
.dot{width:12px;height:12px;border-radius:50%;background:#22c55e;
  box-shadow:0 0 0 4px rgba(34,197,94,.15),0 0 12px rgba(34,197,94,.6);
  animation:pulse 2.4s ease-in-out infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 4px rgba(34,197,94,.15)}50%{box-shadow:0 0 0 7px rgba(34,197,94,.06)}}
.hero h1{font-size:19px;font-weight:700;letter-spacing:.5px}
.hero .sub{font-size:12.5px;color:#64748b;margin-top:3px}
.card{background:rgba(255,255,255,.82);backdrop-filter:blur(8px);
  border:1px solid rgba(226,232,240,.9);border-radius:16px;
  padding:20px 22px;margin:16px 0;box-shadow:0 4px 18px rgba(15,23,42,.05)}
.card h2{font-size:14px;font-weight:700;color:#334155;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.card h2::before{content:"";width:4px;height:14px;border-radius:2px;
  background:linear-gradient(180deg,#6366f1,#2563eb)}
.prow{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:9px 4px;border-bottom:1px dashed #eef2f7}
.prow:last-of-type{border-bottom:0}
.plabel{font-size:13px;color:#64748b;white-space:nowrap}
.pval{display:flex;align-items:center;gap:8px;min-width:0}
.pval code{background:#f1f5f9;border:1px solid #e2e8f0;color:#4338ca;
  padding:4px 10px;border-radius:8px;font-size:12.5px;font-family:Consolas,Menlo,monospace;
  max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.copy{border:1px solid #e2e8f0;background:#fff;color:#64748b;font-size:11.5px;
  border-radius:7px;padding:3px 9px;cursor:pointer;transition:.15s;flex:none}
.copy:hover{border-color:#818cf8;color:#4f46e5;background:#eef2ff}
.copy:disabled{opacity:.35;cursor:default}
.copy:disabled:hover{border-color:#e2e8f0;color:#64748b;background:#fff}
.copy.danger{color:#dc2626;border-color:#fecaca}
.copy.danger:hover{border-color:#f87171;color:#b91c1c;background:#fef2f2}
.pager{display:flex;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap}
.pager .flex{flex:1}
.cnt{font-size:12px;color:#94a3b8}
.note{font-size:12.5px;color:#94a3b8;margin-top:12px;line-height:1.7}
.btn{background:linear-gradient(135deg,#6366f1,#3b82f6);color:#fff;border:0;
  border-radius:10px;padding:9px 18px;font-size:13.5px;font-weight:600;cursor:pointer;
  box-shadow:0 4px 12px rgba(79,70,229,.28);transition:.15s}
.btn:hover{filter:brightness(1.07)}
.btn:active{transform:translateY(1px)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
#out{font-size:12.5px;margin-top:14px;background:#0f172a;color:#a5f3fc;
  padding:14px 16px;border-radius:12px;min-height:20px;line-height:1.8;
  font-family:Consolas,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
#out.ok{color:#86efac}
#out.bad{color:#fca5a5}
#out a{color:#818cf8;text-decoration:none;margin-left:4px}
#out a:hover{text-decoration:underline}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:8px 6px;border-bottom:1px solid #f1f5f9;text-align:left;vertical-align:top}
th{color:#94a3b8;font-weight:600;font-size:12px;white-space:nowrap}
tr:last-child td{border-bottom:0}
.badge{display:inline-block;font-size:11px;padding:2px 9px;border-radius:999px;font-weight:600}
.b-push{background:#e0e7ff;color:#4338ca}
.b-push-ok{background:#dcfce7;color:#15803d}
.b-push-failed{background:#fee2e2;color:#b91c1c}
.b-ctl{background:#f1f5f9;color:#475569}
.time{color:#94a3b8;font-size:12px;white-space:nowrap}
.empty{color:#94a3b8;font-size:12.5px;padding:10px 0}
.cnt{font-size:12px;color:#94a3b8;margin-top:8px}
.hbtns{margin-left:auto;display:flex;gap:8px}
.mini{border:1px solid #e2e8f0;background:#fff;color:#475569;font-size:12px;
  border-radius:9px;padding:6px 13px;cursor:pointer;transition:.15s;box-shadow:0 2px 8px rgba(15,23,42,.05)}
.mini:hover{border-color:#818cf8;color:#4f46e5;background:#eef2ff}
.mask{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;
  align-items:flex-start;justify-content:center;z-index:50;padding:40px 14px;overflow:auto}
.mask.on{display:flex}
.modal{background:#fff;border-radius:16px;padding:22px 24px;max-width:600px;width:100%;
  box-shadow:0 18px 50px rgba(15,23,42,.25)}
.modal h3{font-size:15px;color:#1e293b;margin-bottom:14px;display:flex;align-items:center}
.modal h3 span{flex:1}
.modal .x{border:0;background:none;font-size:18px;color:#94a3b8;cursor:pointer;line-height:1}
.modal .x:hover{color:#475569}
.frow{display:flex;align-items:center;gap:10px;padding:8px 0}
.frow label{width:120px;font-size:12.5px;color:#64748b;flex:none}
.inp,.ta{flex:1;border:1px solid #e2e8f0;border-radius:9px;padding:8px 10px;
  font-size:12.5px;color:#1e293b;font-family:inherit;min-width:0}
.inp:focus,.ta:focus{outline:none;border-color:#818cf8;box-shadow:0 0 0 3px rgba(129,140,248,.15)}
.ta{font-family:Consolas,Menlo,monospace;min-height:84px;resize:vertical}
.fhint{font-size:11.5px;color:#94a3b8;margin:2px 0 2px 130px}
#logbox{background:#0f172a;color:#a5f3fc;border-radius:12px;padding:14px 16px;
  font-family:Consolas,Menlo,monospace;font-size:11.5px;line-height:1.7;
  max-height:52vh;overflow:auto;white-space:pre-wrap;word-break:break-all;margin-top:12px}
</style></head><body><div class="wrap">
<div class="hero">
  <div class="dot"></div>
  <div><h1>下载中心接入Aria2</h1>
  <div class="sub">任何支持 Aria2 协议的软件均可接入 · 任务直达飞牛「下载中心」</div></div>
  <div class="hbtns">
    <button class="mini" onclick="openSettings()">⚙ 设置</button>
    <button class="mini" onclick="openLogs()">📜 全部日志</button>
  </div>
</div>

<div class="card">
  <h2>通用接入参数</h2>
  <div class="prow"><span class="plabel">主机 Host</span>
    <span class="pval"><code id="v-host"></code><button class="copy" onclick="cp(this)">复制</button></span></div>
  <div class="prow"><span class="plabel">端口 Port</span>
    <span class="pval"><code id="v-port">__PORT__</code><button class="copy" onclick="cp(this)">复制</button></span></div>
  <div class="prow"><span class="plabel">密钥 Secret</span>
    <span class="pval"><code id="v-secret"></code><button class="copy" onclick="cp(this)">复制</button></span></div>
  <div class="prow"><span class="plabel">RPC 地址</span>
    <span class="pval"><code id="v-url"></code><button class="copy" onclick="cp(this)">复制</button></span></div>
  <p class="note">把以上四项填进对应软件的「Aria2 / RPC 设置」即可，推送的下载任务会直接出现在飞牛「下载中心」，进度双向同步。</p>
</div>

<div class="card">
  <h2>连通性自检</h2>
  <div class="row">
    <button class="btn" onclick="selfTest()">自检</button>
    <span class="note" style="margin:0">检查 trim-cli 登录态与下载中心任务接口</span>
  </div>
  <div id="out">点击「自检」查看结果</div>
</div>

<div class="card">
  <h2>最近事件<span style="flex:1"></span>
    <button class="copy danger" onclick="clearEvents()">清空记录</button></h2>
  <table><thead><tr><th style="width:150px">时间</th><th style="width:90px">类型</th><th>详情</th></tr></thead>
  <tbody id="events"></tbody></table>
  <div class="pager">
    <span class="cnt" id="cnt"></span><span class="flex"></span>
    <button class="copy" id="pg-prev" onclick="pageTurn(-1)">上一页</button>
    <span class="cnt" id="pg-info"></span>
    <button class="copy" id="pg-next" onclick="pageTurn(1)">下一页</button>
  </div>
</div>

<script>
const SECRET = __SECRET_HTML__;
document.getElementById('v-host').textContent = location.hostname;
document.getElementById('v-secret').textContent = SECRET || '(未设置)';
document.getElementById('v-url').textContent = 'http://' + location.hostname + ':__PORT__/jsonrpc';
document.querySelectorAll('.pval code, .pval .copy').forEach(function(el){
  if (el.classList.contains('copy')) { el.dataset.v = el.previousElementSibling.textContent; }
});
function cp(btn){
  const t = btn.dataset.v || '';
  const done = function(){ btn.textContent = '已复制'; setTimeout(function(){ btn.textContent = '复制'; }, 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done).catch(function(){ fallback(); });
  } else { fallback(); }
  function fallback(){
    const i = document.createElement('textarea'); i.value = t;
    document.body.appendChild(i); i.select();
    try { document.execCommand('copy'); done(); } catch(e){}
    i.remove();
  }
}
const BADGE = { 'push':['b-push','添加任务'], 'push-ok':['b-push-ok','成功'],
  'push-failed':['b-push-failed','失败'], 'pause':['b-ctl','暂停'],
  'resume':['b-ctl','恢复'], 'rm':['b-ctl','删除'], 'retry':['b-ctl','重试'] };
var EV = __EVENTS__;            // 全部事件(服务端已按保留天数/条数清理)
var PAGESIZE = 10, curPage = 0;
var tb = document.getElementById('events');
function pageCount(){ return Math.max(1, Math.ceil(EV.length / PAGESIZE)); }
function renderEvents(){
  var n = pageCount();
  if (curPage >= n) curPage = n - 1;
  if (curPage < 0) curPage = 0;
  var s = curPage * PAGESIZE, e = Math.min(EV.length, s + PAGESIZE);
  if (!EV.length) tb.innerHTML = '<tr><td colspan="3" class="empty">暂无事件记录</td></tr>';
  else {
    var html = '';
    for (var i = s; i < e; i++) {
      var b = BADGE[EV[i].kind] || ['b-ctl', EV[i].kind];
      var d = String(EV[i].detail).replace(/</g,'&lt;');
      html += '<tr><td class="time">'+EV[i].time+'</td><td><span class="badge '+b[0]+'">'+b[1]+'</span></td><td>'+d+'</td></tr>';
    }
    tb.innerHTML = html;
  }
  document.getElementById('cnt').textContent = '共 ' + EV.length + ' 条推送 / 控制记录';
  var prev = document.getElementById('pg-prev'), next = document.getElementById('pg-next');
  var info = document.getElementById('pg-info');
  if (EV.length <= PAGESIZE) { info.textContent=''; prev.style.display='none'; next.style.display='none'; }
  else {
    info.textContent = (curPage + 1) + ' / ' + n + ' 页';
    prev.style.display = ''; next.style.display = '';
    prev.disabled = curPage <= 0; next.disabled = curPage >= n - 1;
  }
}
function pageTurn(d){ curPage += d; renderEvents(); }
function clearEvents(){
  if (!confirm('确定清空全部事件记录？此操作不可恢复。')) return;
  fetch('/api/events/clear', {method:'POST'}).then(function(){
    EV = []; curPage = 0; renderEvents();
  });
}
renderEvents();
let lastResult = null, expanded = false;
function selfTest(){
  const out = document.getElementById('out');
  out.className = '';
  out.textContent = '检测中…';
  fetch('/api/selftest').then(function(r){return r.json()}).then(function(j){
    lastResult = j; expanded = false; renderOut();
  }).catch(function(e){ lastResult = null; expanded = false;
    out.textContent = '请求失败: ' + e; });
}
function renderOut(){
  const out = document.getElementById('out');
  if (!lastResult) { out.textContent = '点击「自检」查看结果'; return; }
  const okLogin = lastResult.login === 'ok';
  const okQuery = String(lastResult.task_query || '').indexOf('ok') === 0;
  const ok = okLogin && okQuery;
  const sum = (ok ? '✓ 自检通过：登录态正常，下载中心接口连通'
                  : '✗ 自检存在异常：登录=' + lastResult.login + '，任务接口=' + lastResult.task_query);
  if (!expanded) {
    out.className = ok ? 'ok' : 'bad';
    out.innerHTML = escapeHtml(sum) + '　<a href="javascript:void(0)" onclick="expanded=true;renderOut()">展开详情</a>';
  } else {
    out.className = '';
    out.innerHTML = '<a href="javascript:void(0)" onclick="expanded=false;renderOut()">收起</a>\\n' +
      escapeHtml(JSON.stringify(lastResult, null, 2));
  }
}
function escapeHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }

/* ---------- 设置弹窗 ---------- */
/* 注意: 弹窗 DOM 在脚本之后,必须点击时再查找,脚本解析时拿到的是 null */
function openSettings(){ document.getElementById('mask-settings').classList.add('on'); loadConfig(); }
function closeSettings(){ document.getElementById('mask-settings').classList.remove('on'); }
function loadConfig(){
  fetch('/api/config').then(function(r){return r.json()}).then(function(j){
    document.getElementById('s-secret').value = j.secret || '';
    document.getElementById('s-savedir').value = j.default_save_dir || '';
    document.getElementById('s-days').value = j.event_retention_days;
    document.getElementById('s-maxn').value = j.event_max_count;
    document.getElementById('s-logdays').value = j.log_retention_days;
    document.getElementById('s-dirmap').value = JSON.stringify(j.dir_map || [], null, 1);
  }).catch(function(e){ alert('读取配置失败: ' + e); });
}
function saveConfig(){
  var dirmap;
  try { dirmap = JSON.parse(document.getElementById('s-dirmap').value || '[]'); }
  catch(e){ alert('目录映射不是合法 JSON: ' + e.message); return; }
  if (!Array.isArray(dirmap)) { alert('目录映射必须是数组'); return; }
  var body = {
    secret: document.getElementById('s-secret').value.trim(),
    default_save_dir: document.getElementById('s-savedir').value.trim(),
    event_retention_days: parseInt(document.getElementById('s-days').value, 10) || 30,
    event_max_count: parseInt(document.getElementById('s-maxn').value, 10) || 1000,
    log_retention_days: parseInt(document.getElementById('s-logdays').value, 10) || 30,
    dir_map: dirmap,
  };
  var st = document.getElementById('s-status');
  st.textContent = '保存中…';
  fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)}).then(function(r){ return r.json(); }).then(function(j){
    if (j.ok) { st.textContent = '✓ 已保存并生效'; setTimeout(function(){ location.reload(); }, 700); }
    else st.textContent = '✗ ' + (j.error || '保存失败');
  }).catch(function(e){ st.textContent = '✗ 请求失败: ' + e; });
}

/* ---------- 日志弹窗 ---------- */
function openLogs(){ document.getElementById('mask-logs').classList.add('on'); loadLogs(); }
function closeLogs(){ document.getElementById('mask-logs').classList.remove('on'); }
function loadLogs(){
  var box = document.getElementById('logbox');
  box.textContent = '加载中…';
  fetch('/api/logs?lines=800').then(function(r){ return r.text(); }).then(function(t){
    box.textContent = t || '(日志为空)';
    box.scrollTop = box.scrollHeight;
  }).catch(function(e){ box.textContent = '加载失败: ' + e; });
}
</script>

<div class="mask" id="mask-settings">
  <div class="modal">
    <h3>设置<span><button class="x" onclick="closeSettings()">✕</button></span></h3>
    <div class="frow"><label>RPC 密钥</label>
      <input class="inp" id="s-secret" placeholder="留空表示不校验 token"></div>
    <div class="frow"><label>默认保存目录</label>
      <input class="inp" id="s-savedir" placeholder="/vol1/Download"></div>
    <div class="frow"><label>事件保留天数</label>
      <input class="inp" id="s-days" type="number" min="1" style="max-width:110px"></div>
    <div class="frow"><label>事件条数上限</label>
      <input class="inp" id="s-maxn" type="number" min="10" style="max-width:110px"></div>
    <div class="frow"><label>日志保留天数</label>
      <input class="inp" id="s-logdays" type="number" min="1" style="max-width:110px"></div>
    <div class="fhint">运行日志（全部日志）超过该天数的行会自动清理。</div>
    <div class="frow" style="align-items:flex-start"><label style="padding-top:8px">目录映射</label>
      <textarea class="ta" id="s-dirmap" placeholder='[{"from": "/downloads", "to": "/vol1/Download"}]'></textarea></div>
    <div class="fhint">把推送方填的保存路径前缀映射到飞牛卷路径，按从上到下匹配；留空 [] 则仅用默认保存目录。</div>
    <div class="fhint" style="color:#f59e0b">修改 RPC 密钥后，需在推送软件里同步更新。</div>
    <div class="row" style="margin-top:14px;justify-content:flex-end">
      <span class="cnt" id="s-status" style="margin-right:auto"></span>
      <button class="mini" onclick="closeSettings()">取消</button>
      <button class="btn" style="padding:7px 16px;font-size:12.5px" onclick="saveConfig()">保存</button>
    </div>
  </div>
</div>

<div class="mask" id="mask-logs">
  <div class="modal">
    <h3>运行日志（bridge.log 末尾 800 行）<span><button class="x" onclick="closeLogs()">✕</button></span></h3>
    <div id="logbox">加载中…</div>
    <div class="row" style="margin-top:14px;justify-content:flex-end">
      <a class="mini" style="text-decoration:none;display:inline-block;margin-right:auto"
         href="/api/logs/download" download="bridge.log">⬇ 下载完整日志</a>
      <button class="mini" onclick="loadLogs()">刷新</button>
      <button class="mini" onclick="closeLogs()">关闭</button>
    </div>
  </div>
</div>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "aria2bridge/1.0"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, self.status_page(), "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/selftest"):
            self._send(200, json.dumps(self.selftest(), ensure_ascii=False))
            return
        if self.path.startswith("/api/config"):
            self._send(200, json.dumps(self.view_config(), ensure_ascii=False))
            return
        if self.path.startswith("/api/logs/download"):
            body = self.log_text().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", "attachment; filename=bridge.log")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/logs"):
            lines = 800
            for kv in self.path.split("?", 1)[-1].split("&"):
                if kv.startswith("lines="):
                    try:
                        lines = min(int(kv[6:]), 5000)
                    except Exception:  # noqa: BLE001
                        pass
            self._send(200, self.log_text(lines), "text/plain; charset=utf-8")
            return
        if self.path.startswith("/jsonrpc"):
            import urllib.parse
            q = urllib.parse.urlparse(self.path).query
            kv = urllib.parse.parse_qs(q)
            if "params" in kv:
                try:
                    params = json.loads(kv["params"][0])
                    req = {"jsonrpc": "2.0", "id": kv.get("id", [1])[0],
                           "method": kv.get("method", [""])[0], "params": params}
                    self._send(200, json.dumps(dispatch(req), ensure_ascii=False))
                    return
                except Exception as e:  # noqa: BLE001
                    self._send(400, json.dumps({"error": {"code": 1, "message": str(e)}}))
                    return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.startswith("/api/events/clear"):
            clear_recent()
            self._send(200, json.dumps({"ok": True}))
            return
        if self.path.startswith("/api/config"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception as e:  # noqa: BLE001
                self._send(400, json.dumps({"ok": False, "error": "bad request: %s" % e}))
                return
            try:
                apply_config(body if isinstance(body, dict) else {})
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:  # noqa: BLE001
                self._send(400, json.dumps({"ok": False, "error": str(e)}))
            return
        if not self.path.startswith("/jsonrpc"):
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self._send(400, json.dumps({"error": {"code": 1, "message": "bad request: %s" % e}}))
            return
        if isinstance(req, list):
            resp = [dispatch(r) for r in req]
        else:
            resp = dispatch(req)
        self._send(200, json.dumps(resp, ensure_ascii=False))

    # ----- 状态页/自检 -----
    def view_config(self):
        days, mx = event_conf()
        try:
            log_days = max(int(CFG.get("log_retention_days", 30)), 1)
        except Exception:  # noqa: BLE001
            log_days = 30
        return {
            "secret": CFG.get("secret", ""),
            "default_save_dir": CFG.get("default_save_dir", ""),
            "event_retention_days": days,
            "event_max_count": mx,
            "log_retention_days": log_days,
            "dir_map": CFG.get("dir_map", []),
        }

    def log_text(self, max_lines=None):
        p = os.path.join(VAR_DIR, "bridge.log") if VAR_DIR else ""
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:  # noqa: BLE001
            return "(暂无日志)"
        if max_lines:
            content = "\n".join(content.splitlines()[-max_lines:])
        return content

    def status_page(self):
        secret = CFG.get("secret", "")
        shown = secret if secret else "（未设置 secret,任意 token 可用）"
        html = (STATUS_HTML
                .replace("__PORT__", str(CFG.get("port", 6800)))
                .replace("__SECRET_HTML__", json.dumps(shown, ensure_ascii=False))
                .replace("__EVENTS__", json.dumps(RECENT, ensure_ascii=False)))
        return html

    def selftest(self):
        out = {}
        ok, msg = trim_login()
        out["login"] = "ok" if ok else ("FAIL: " + msg)
        try:
            tasks = dc_query_tasks()
            out["task_query"] = "ok, 当前任务数: %d" % len(tasks)
        except Exception as ex:  # noqa: BLE001
            out["task_query"] = "FAIL: %s" % ex
        try:
            out["default_save_dir"] = dc_default_save_dir(force=True)
        except Exception as ex:  # noqa: BLE001
            out["default_save_dir"] = "FAIL: %s" % ex
        return out


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    global CFG, CFG_PATH, VAR_DIR, TRIM_BIN, RECENT
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="config.json 路径")
    ap.add_argument("--var", default="", help="运行数据目录(日志/会话)")
    ap.add_argument("--trim-cli", default="", help="trim-cli 可执行文件路径")
    args = ap.parse_args()

    CFG_PATH = os.path.abspath(args.config)
    with open(CFG_PATH, encoding="utf-8") as f:
        CFG = json.load(f)

    VAR_DIR = os.path.abspath(args.var or os.path.dirname(CFG_PATH))
    os.makedirs(VAR_DIR, exist_ok=True)
    RECENT = load_recent()
    prune_recent()      # 启动即按保留天数/条数清理历史事件
    save_recent()
    trim_log_by_age()   # 启动即清理超过保留天数的运行日志
    LOG_TRIM_CHECK["at"] = time.time()

    here = os.path.dirname(os.path.abspath(__file__))
    TRIM_BIN = args.trim_cli or os.path.join(here, "trim-cli-linux-x64")
    if not os.path.exists(TRIM_BIN):
        # 本地测试兜底: 同目录或 PATH
        for cand in (os.path.join(here, "trim-cli"), "trim-cli"):
            if os.path.exists(cand) or _which(cand):
                TRIM_BIN = cand
                break
    if os.path.exists(TRIM_BIN):
        try:
            os.chmod(TRIM_BIN, 0o755)
        except Exception:
            pass

    port = int(CFG.get("port", 6800))
    log("aria2bridge 启动: port=%s config=%s trim=%s" % (port, CFG_PATH, TRIM_BIN))

    # 启动时尝试登录,失败不阻塞(状态页可见原因)
    threading.Thread(target=trim_login, daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log("listening on 0.0.0.0:%d" % port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


if __name__ == "__main__":
    main()
