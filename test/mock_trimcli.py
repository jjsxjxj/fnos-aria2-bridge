# -*- coding: utf-8 -*-
"""mock trim-cli: 模拟飞牛下载中心接口,用于本地测试 bridge.py

控制文件(与本文件同目录,可由测试在运行中改写):
  mock_ctl.json          {"adduri_fail_n": <前 N 次 addUris 报超时错>,
                          "query_recent": <task.query 是否带一个刚创建的任务>,
                          "adduri_id": <addUris 成功时返回的任务 id, 默认 999>}
  mock_adduri_count.txt  addUris 调用计数(测试前重置)
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

args = sys.argv[1:]
blob = " ".join(args)


def read_ctl():
    try:
        with open(os.path.join(HERE, "mock_ctl.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


if "--version" in args:
    print("trim-cli 1.0.0 (mock)")
    sys.exit(0)

if "login" in args:
    # 模拟登录成功(trim-cli 正常无输出或 succ)
    sys.exit(0)

ctl = read_ctl()

if "task.query" in blob:
    # 字段语义与真实 fnOS API 一致:
    #   dl_total_size = 已下载量;size 恒等于 total_size(总大小)
    tasks = [
        {"id": 123, "name": "ABC-123.mp4", "state": "downloading",
         "total_size": 1000000, "size": 1000000, "dl_total_size": 400000,
         "dl_speed": 12345, "up_speed": 100, "save_dir": "/vol6/1000/test",
         "uri": "magnet:?xt=urn:btih:AAA", "addition_time": 1700000000},
        {"id": 124, "name": "DEF-456.mp4", "state": "completed",
         "total_size": 2000000, "size": 2000000, "dl_total_size": 2000000,
         "dl_speed": 0, "up_speed": 0, "save_dir": "/vol6/1000/test",
         "uri": "magnet:?xt=urn:btih:BBB", "addition_time": 1700000001},
        {"id": 125, "name": "GHI-789.mp4", "state": "paused",
         "total_size": 3000000, "size": 3000000, "dl_total_size": 100000,
         "dl_speed": 0, "up_speed": 0, "save_dir": "/vol6/1000/test",
         "uri": "magnet:?xt=urn:btih:CCC", "addition_time": 1700000002},
    ]
    if ctl.get("query_recent"):
        tasks.append({"id": 777, "name": "NEW-TASK.mp4", "state": "downloading",
                      "total_size": 5000000, "size": 5000000, "dl_total_size": 0,
                      "dl_speed": 0, "up_speed": 0, "save_dir": "/vol6/1000/test",
                      "uri": "magnet:?xt=urn:btih:NEW",
                      "addition_time": int(time.time())})
    print(json.dumps({"tasks": tasks, "stats": {}}))
    sys.exit(0)

if "task.addUris" in blob:
    cnt_file = os.path.join(HERE, "mock_adduri_count.txt")
    n = 0
    try:
        with open(cnt_file, encoding="utf-8") as f:
            n = int(f.read().strip() or 0)
    except Exception:
        n = 0
    n += 1
    try:
        with open(cnt_file, "w", encoding="utf-8") as f:
            f.write(str(n))
    except Exception:
        pass
    if n <= int(ctl.get("adduri_fail_n", 0)):
        print("Error: failed to get download data from 127.0.0.1:5666: "
              "timed out waiting for websocket response", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"ids": [int(ctl.get("adduri_id", 999))]}))
    sys.exit(0)

if "getDefaultSaveDir" in blob:
    print('"/vol6/1000/矿盘/已完成/未整理"')
    sys.exit(0)

if any(c in args for c in ("pause", "resume", "rm", "retry")):
    # 与真实 trim-cli 一致: 控制类子命令不接受 --yes
    if "--yes" in args:
        print("error: unexpected argument '--yes' found", file=sys.stderr)
        sys.exit(2)
    print("{}")
    sys.exit(0)

print(json.dumps({"mock": "no-match", "args": args}))
sys.exit(1)
