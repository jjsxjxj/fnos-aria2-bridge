# 下载中心接入Aria2（aria2bridge）

给飞牛 fnOS「下载中心」开一个**标准 Aria2 JSON-RPC 接口**的桥接应用（fpk 原生应用）。

任何支持 Aria2 协议的软件——DBO（DB_Online）、AriaNg、Motrix 等——都能把下载任务直接推进飞牛「下载中心」，进度、暂停、删除实时同步。

## 解决什么问题

飞牛 fnOS 的「下载中心」底层是 qBittorrent + aria2 引擎，但引擎 RPC 端口不对局域网开放，第三方下载管理软件无法直连。本应用在 NAS 上伪装一个标准 Aria2 JSON-RPC 服务（默认端口 `6800`），把收到的指令通过飞牛官方 WebSocket 接口（`appcgi.downloadcenter.*`）转发到下载中心：

```
Aria2 客户端 (DBO / AriaNg / …)
      │  Aria2 JSON-RPC (http://NAS:6800/jsonrpc, token 鉴权)
      ▼
┌─────────────────────────────┐     WebSocket 5666      ┌──────────────────┐
│  aria2bridge (fpk 应用)      │ ─────────────────────► │  fnOS 下载中心    │
│  纯 Python 标准库 + trim-cli │ ◄───────────────────── │  (qB + aria2)    │
└─────────────────────────────┘   task.query / addUris  └──────────────────┘
```

## 功能

- **标准 Aria2 JSON-RPC 协议**：`getVersion` / `getGlobalStat` / `addUri` / `tellActive` / `tellWaiting` / `tellStopped` / `tellStatus` / `pause` / `unpause` / `remove` / `forceRemove` / `removeDownloadResult` / `getOption` / `getGlobalOption` / `changeGlobalOption` / `system.multicall` / 批量请求 / GET 查询，全方法兼容
- **token 鉴权**（`token:secret`）
- **保存路径映射**（dir_map）：把客户端填写的路径前缀映射到飞牛 `/vol*` 卷路径
- **假失败容错**：飞牛接口偶发响应慢（实测约 9 秒）时，自动轮询任务列表确认任务是否已创建，避免「任务建了却报失败」
- **状态页**（桌面图标打开）：
  - 接入参数一键复制
  - 连通性自检（trim-cli 登录态 + 下载中心接口）
  - 最近事件：分页浏览、一键清空、30 天 / 1000 条自动清理
  - ⚙ 设置面板：RPC 密钥、默认保存目录、事件/日志保留策略、目录映射，保存即生效
  - 📜 全部日志：在线查看 + 完整下载，30 天按行自动清理 + 5MB 滚动
- 零 pip 依赖，纯 Python 标准库，随包内置 trim-cli

## 安装

1. 在飞牛应用中心安装 **「Python 3.12」开发工具**（运行依赖）
2. 下载 [aria2bridge.fpk](https://github.com/jjsxjxj/fnos-aria2-bridge/releases/latest)（或见[博客文章](https://813146.xyz/post/fnos-aria2-bridge-download-center)内的网盘渠道）
3. 飞牛应用中心 → 手动安装 → 选择 fpk，按向导填写：
   - 飞牛账号（**未开启两步验证**的账号）与密码
   - 服务端口（默认 `6800`）
   - RPC 密钥（Secret）
   - 默认保存目录

## 接入方法（通用）

在软件的「Aria2 / RPC 设置」里填：

| 项 | 值 |
|---|---|
| 主机 Host | 飞牛主机 IP |
| 端口 Port | `6800`（安装时设置的端口） |
| 密钥 Secret | 安装向导中设置的 Secret |
| 接口地址 | `http://<飞牛IP>:6800/jsonrpc` |

推送的下载任务会直接出现在飞牛「下载中心」，进度双向同步。

## 保存路径映射

很多客户端推送时会指定保存目录。飞牛只认 `/vol*` 卷路径，可在状态页 ⚙ 设置里配置 `dir_map` 把客户端路径前缀映射过去：

```json
[
  { "from": "/downloads/movies", "to": "/vol1/媒体/电影" },
  { "from": "/downloads", "to": "/vol1/Download" }
]
```

按从上到下匹配；未命中的走默认保存目录；客户端直接填 `/vol*` 路径则原样透传。

## 从源码构建

```bash
# 需要 fnpack（飞牛 fpk 打包工具）
fnpack build .
```

包内目录结构：

```
manifest        # fpk 元信息（appname/version/依赖声明等）
cmd/            # 生命周期脚本（start/stop/status/安装卸载回调）
config/         # 权限与资源声明
wizard/         # 安装/卸载/设置向导表单
app/            # 应用本体（bridge.py + 内置 trim-cli）
test/           # 集成测试（mock trim-cli，38 项）
```

运行测试：`python test/test_bridge.py`（自动启动 bridge + mock trim-cli，覆盖协议层全部方法与容错分支）。

## 已知限制

- 仅提供 **x86_64** 包
- 飞牛 `task.addUris` 不支持 **ed2k** 链接（http/https/ftp/sftp/magnet 可用）
- 状态页**无鉴权**，请勿把 6800 端口映射到公网
- 需使用**未开启两步验证**的飞牛账号（桥接需程序化登录）
- 飞牛账号密码仅保存在应用配置目录（`config.json`，权限 600），建议使用专用低权限账号
- 接口基于 fnOS 当前版本的 `appcgi.downloadcenter.*`，飞牛大版本更新后如有变化需适配

## 版本历史

| 版本 | 要点 |
|---|---|
| 1.0.18 | 运行日志 30 天按行自动清理（可配置） |
| 1.0.17 | 日志 5MB 滚动；修复日志重复写入 |
| 1.0.16 | 控制事件中文文案与徽章 |
| 1.0.15 | 修复设置/日志按钮点击无效 |
| 1.0.14 | 状态页 ⚙ 设置面板 + 📜 全部日志 |
| 1.0.13 | 修复暂停/删除等控制命令失败（trim-cli 参数） |
| 1.0.12 | 事件记录持久化 + 分页 + 手动清空 |
| 1.0.11 | 修复进度映射（completedLength 用 dl_total_size） |
| 1.0.10 | addUri 假失败容错（响应超时轮询确认） |
| 1.0.0–1.0.9 | 初版：Aria2 协议全方法桥接、状态页、自检、安装向导、图标与文案迭代 |

## 相关链接

- 博客文章：[飞牛下载中心接入 Aria2 —— 任意下载器直推方案](https://813146.xyz/post/fnos-aria2-bridge-download-center)
- 内置 [trim-cli](https://www.trimcss.com/) 用于与飞牛 WebSocket 接口通信

## 许可

[MIT](LICENSE) © 折腾日记
