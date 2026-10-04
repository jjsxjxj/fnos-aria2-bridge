# -*- coding: utf-8 -*-
"""本地集成测试: bridge.py + mock trim-cli, 验证 aria2 JSON-RPC 协议层"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# 绕过沙箱代理,直连本机
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(k, None)
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
PORT = 16800
SECRET = "testsecret123"
BASE = "http://127.0.0.1:%d" % PORT

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (tag, name, detail if not cond else ""))
    if not cond:
        FAILS.append(name)


def rpc(method, params=None, token=SECRET, id=1):
    p = list(params or [])
    if token:
        p = ["token:" + token] + p
    body = json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": p}).encode()
    req = urllib.request.Request(BASE + "/jsonrpc", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    # mock trim-cli wrapper (.cmd for Windows) — 内容保持纯 ASCII,
    # 用 %~dp0 运行时展开自身目录,避免 .cmd 文件中文路径被 GBK 误解
    py = PY.replace("\\", "/") if os.name == "nt" else PY
    cmd = os.path.join(HERE, "mock_trimcli.cmd")
    if os.name == "nt":
        with open(cmd, "w", encoding="ascii") as f:
            f.write('@"%s" "%%~dp0mock_trimcli.py" %%*\n' % py)
    else:
        cmd = os.path.join(HERE, "mock_trimcli.sh")
        with open(cmd, "w") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (py, os.path.join(HERE, "mock_trimcli.py")))
    mock = cmd

    cfg = {
        "port": PORT, "secret": SECRET,
        "fnos_user": "admin", "fnos_password": "x",
        "fnos_host": "127.0.0.1", "fnos_port": 5666,
        "default_save_dir": "/vol6/1000/矿盘/已完成/未整理",
        "dir_map": [{"from": "/media", "to": "/vol6/1000/矿盘"}],
        "timeout_poll_interval": 0.3, "timeout_poll_attempts": 3,
    }
    cfg_path = os.path.join(HERE, "test_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    var_dir = os.path.join(HERE, "test_var")
    os.makedirs(var_dir, exist_ok=True)
    # 事件历史以 test_var/recent.json 持久化,测试前清掉避免上轮残留干扰断言
    for f in ("recent.json", "mock_ctl.json", "mock_adduri_count.txt"):
        try:
            os.remove(os.path.join(HERE, f) if f.startswith("mock") else os.path.join(var_dir, f))
        except OSError:
            pass
    # 预置一条超龄日志行,验证启动时按 log_retention_days(默认30天)清理
    with open(os.path.join(var_dir, "bridge.log"), "a", encoding="utf-8") as f:
        f.write("2026-08-01 00:00:00 OLD-STALE-LOGLINE\n")

    proc = subprocess.Popen([
        PY, os.path.join(ROOT, "app", "bridge.py"),
        "--config", cfg_path, "--var", var_dir, "--trim-cli", mock,
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    try:
        err = None
        for _ in range(30):
            try:
                urllib.request.urlopen(BASE + "/", timeout=2)
                err = None
                break
            except Exception as e:
                err = e
                time.sleep(0.3)
        else:
            print("bridge 未在超时内启动, 最后错误: %r" % err)
            proc.kill()
            proc.wait(10)
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            print(out[:3000])
            sys.exit(1)

        # 1. 状态页
        with urllib.request.urlopen(BASE + "/") as r:
            html = r.read().decode()
        check("status page", "下载中心接入Aria2" in html)

        # 2. 自检接口
        with urllib.request.urlopen(BASE + "/api/selftest") as r:
            st = json.loads(r.read().decode())
        check("selftest", st.get("task_query", "").startswith("ok"), str(st))

        # 3. getVersion
        r = rpc("aria2.getVersion")
        check("getVersion", r["result"]["version"] == "1.37.0", json.dumps(r))

        # 4. 错误 token
        r = rpc("aria2.getVersion", token="wrong")
        check("bad token rejected", "error" in r, json.dumps(r))

        # 5. addUri 磁力 + dir 映射 (/media/xx → /vol6/1000/矿盘/xx)
        r = rpc("aria2.addUri", [["magnet:?xt=urn:btih:TEST"], {"dir": "/media/ABC-123"}])
        gid = r.get("result", "")
        check("addUri returns gid", len(gid) == 16 and gid == format(999, "016x"), json.dumps(r))

        # 6. addUri 无 dir → default_save_dir
        r = rpc("aria2.addUri", [["https://example.com/a.iso"], {}])
        check("addUri default dir", "result" in r, json.dumps(r))

        # 7. addUri /vol 路径直通
        r = rpc("aria2.addUri", [["https://example.com/b.iso"], {"dir": "/vol2/docker/x"}])
        check("addUri vol path", "result" in r, json.dumps(r))

        # 7a. addUri 假失败恢复: 首次 addUris 报 websocket 超时,
        #     但轮询发现任务实际已创建(id=777) → 应返回 777 的 gid 而非报错
        with open(os.path.join(HERE, "mock_ctl.json"), "w", encoding="utf-8") as f:
            json.dump({"adduri_fail_n": 1, "query_recent": True}, f)
        with open(os.path.join(HERE, "mock_adduri_count.txt"), "w", encoding="utf-8") as f:
            f.write("0")
        r = rpc("aria2.addUri", [["magnet:?xt=urn:btih:FALSEFAIL"], {}])
        gid = r.get("result", "")
        check("addUri timeout recovered (task exists)",
              gid == format(777, "016x"), json.dumps(r))

        # 7b. addUri 真超时重试: 首次报超时, 轮询无新任务 → 重试成功(id=888)
        with open(os.path.join(HERE, "mock_ctl.json"), "w", encoding="utf-8") as f:
            json.dump({"adduri_fail_n": 1, "query_recent": False, "adduri_id": 888}, f)
        with open(os.path.join(HERE, "mock_adduri_count.txt"), "w", encoding="utf-8") as f:
            f.write("0")
        r = rpc("aria2.addUri", [["magnet:?xt=urn:btih:RETRY"], {}])
        gid = r.get("result", "")
        check("addUri timeout retried", gid == format(888, "016x"), json.dumps(r))

        # 恢复 mock 默认行为
        with open(os.path.join(HERE, "mock_ctl.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        with open(os.path.join(HERE, "mock_adduri_count.txt"), "w", encoding="utf-8") as f:
            f.write("0")

        # 8. tellActive: mock 中 id=123 downloading
        r = rpc("aria2.tellActive")
        gids = [t["gid"] for t in r["result"]]
        check("tellActive", format(123, "016x") in gids and len(r["result"]) == 1, json.dumps(r.get("result"))[:200])
        t = r["result"][0]
        check("tellActive fields", t["status"] == "active" and t["totalLength"] == "1000000"
              and t["completedLength"] == "400000" and t["downloadSpeed"] == "12345", json.dumps(t))

        # 9. tellStopped: id=124 completed
        r = rpc("aria2.tellStopped")
        gids = [t["gid"] for t in r["result"]]
        check("tellStopped", format(124, "016x") in gids, json.dumps(gids))

        # 10. tellWaiting: id=125 paused
        r = rpc("aria2.tellWaiting")
        gids = [t["gid"] for t in r["result"]]
        check("tellWaiting", format(125, "016x") in gids, json.dumps(gids))

        # 11. tellStatus
        r = rpc("aria2.tellStatus", [format(124, "016x")])
        check("tellStatus", r["result"]["status"] == "complete", json.dumps(r))

        # 12. tellStatus 不存在
        r = rpc("aria2.tellStatus", ["ffffffffffffffff"])
        check("tellStatus missing", "error" in r, json.dumps(r))

        # 13. getGlobalStat
        r = rpc("aria2.getGlobalStat")
        check("getGlobalStat", r["result"]["numActive"] == "1" and r["result"]["numStopped"] == "1"
              and r["result"]["numWaiting"] == "1", json.dumps(r))

        # 14. pause / unpause / remove
        r = rpc("aria2.pause", [format(123, "016x")])
        check("pause", r.get("result") == format(123, "016x"), json.dumps(r))
        r = rpc("aria2.forceRemove", [format(123, "016x")])
        check("remove", "result" in r, json.dumps(r))
        r = rpc("aria2.removeDownloadResult", [format(123, "016x")])
        check("removeDownloadResult", r.get("result") == "OK", json.dumps(r))

        # 15. getGlobalOption / changeGlobalOption
        r = rpc("aria2.getGlobalOption")
        check("getGlobalOption", "dir" in r.get("result", {}), json.dumps(r))
        r = rpc("aria2.changeGlobalOption", [{"max-concurrent-downloads": "3"}])
        check("changeGlobalOption", r.get("result") == "OK", json.dumps(r))

        # 16. system.multicall (aria2 格式: params = [[call, call]])
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "system.multicall",
                           "params": [[{"methodName": "aria2.getVersion", "params": ["token:" + SECRET]},
                                       {"methodName": "aria2.tellActive", "params": ["token:" + SECRET]}]]}).encode()
        req = urllib.request.Request(BASE + "/jsonrpc", data=body)
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read().decode())
        ok = isinstance(r.get("result"), list) and len(r["result"]) == 2
        check("multicall", ok, json.dumps(r)[:300])

        # 17. 批量请求(数组)
        body = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "aria2.getVersion", "params": ["token:" + SECRET]},
            {"jsonrpc": "2.0", "id": 2, "method": "aria2.getGlobalStat", "params": ["token:" + SECRET]},
        ]).encode()
        req = urllib.request.Request(BASE + "/jsonrpc", data=body)
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read().decode())
        check("batch", isinstance(r, list) and len(r) == 2, json.dumps(r)[:200])

        # 18. GET /jsonrpc (aria2 兼容)
        q = urllib.parse.urlencode({"method": "aria2.getVersion",
                                    "params": json.dumps(["token:" + SECRET]), "id": "3"})
        with urllib.request.urlopen(BASE + "/jsonrpc?" + q, timeout=15) as resp:
            r = json.loads(resp.read().decode())
        check("GET jsonrpc", r.get("result", {}).get("version") == "1.37.0", json.dumps(r))

        # 19. 事件持久化: 前面多次 addUri 应已写入 recent.json 且页面可见(含分页控件)
        rj = os.path.join(var_dir, "recent.json")
        ok = os.path.exists(rj) and "btih:TEST" in open(rj, encoding="utf-8").read()
        check("events persisted to recent.json", ok)
        with urllib.request.urlopen(BASE + "/") as r2:
            html = r2.read().decode()
        check("events shown with pager", "btih:TEST" in html and "pg-prev" in html
              and "共 " in html, html[-500:])
        check("interactive buttons", "openSettings" in html and "openLogs" in html
              and "mask-settings" in html and "mask-logs" in html)

        # 20. 重启后事件仍在(启动时从 recent.json 加载)
        proc.terminate(); proc.wait(10)
        proc = subprocess.Popen([
            PY, os.path.join(ROOT, "app", "bridge.py"),
            "--config", cfg_path, "--var", var_dir, "--trim-cli", mock,
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(30):
            try:
                urllib.request.urlopen(BASE + "/", timeout=2); break
            except Exception:
                time.sleep(0.3)
        with urllib.request.urlopen(BASE + "/") as r2:
            html = r2.read().decode()
        check("events survive restart", "btih:TEST" in html, html[-500:])

        # 21. 手动清空事件
        req = urllib.request.Request(BASE + "/api/events/clear", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read().decode())
        check("clear api", r.get("ok") is True, json.dumps(r))
        with urllib.request.urlopen(BASE + "/") as r2:
            html = r2.read().decode()
        ok = "暂无事件记录" in html and "btih:TEST" not in html
        check("events cleared on page", ok, html[-500:])
        ok = os.path.exists(rj) and json.load(open(rj, encoding="utf-8")) == []
        check("recent.json emptied", ok)

        # 22. 设置接口: 读取配置
        with urllib.request.urlopen(BASE + "/api/config") as r2:
            cfg_view = json.loads(r2.read().decode())
        ok = all(k in cfg_view for k in ("secret", "default_save_dir",
                                         "event_retention_days", "event_max_count", "dir_map"))
        check("config view", ok, json.dumps(cfg_view))

        # 23. 设置接口: 保存配置并即时生效+落盘
        new_body = dict(cfg_view)
        new_body["event_retention_days"] = 7
        new_body["event_max_count"] = 500
        new_body["secret"] = SECRET  # 保持不变,避免影响其他 rpc
        req = urllib.request.Request(BASE + "/api/config", data=json.dumps(new_body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read().decode())
        check("config save", r.get("ok") is True, json.dumps(r))
        with urllib.request.urlopen(BASE + "/api/config") as r2:
            cfg2 = json.loads(r2.read().decode())
        check("config applied", cfg2["event_retention_days"] == 7 and cfg2["event_max_count"] == 500,
              json.dumps(cfg2))
        ok = json.load(open(cfg_path, encoding="utf-8"))["event_retention_days"] == 7
        check("config persisted to file", ok)

        # 24. 非法 dir_map → 400
        bad = dict(cfg2)
        bad["dir_map"] = "not-a-list"
        req = urllib.request.Request(BASE + "/api/config", data=json.dumps(bad).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=15)
            check("config invalid dir_map rejected", False)
        except urllib.error.HTTPError as e:
            check("config invalid dir_map rejected", e.code == 400, str(e))

        # 25. 日志接口: 尾部查看 + 完整下载 + 超龄行已清理
        with urllib.request.urlopen(BASE + "/api/logs?lines=5000") as r2:
            txt = r2.read().decode()
        check("logs tail", "aria2bridge" in txt, txt[:200])
        check("stale log line trimmed", "OLD-STALE-LOGLINE" not in txt)
        with urllib.request.urlopen(BASE + "/api/logs/download") as r2:
            txt = r2.read().decode()
        check("logs download", "aria2bridge" in txt, txt[:200])

    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()

    print()
    if FAILS:
        print("FAILED: %d — %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
