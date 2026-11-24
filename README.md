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
- **token 鉴权**（每个 RPC 调用必须携带 `token:secret`，包含批量和 multicall 内部调用）
- **管理页鉴权**：HTTP Basic，用户名 `aria2bridge`，密码为 RPC Secret；状态、设置、自检、事件和日志接口统一保护
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
2. 从 [Releases](https://github.com/jjsxjxj/fnos-aria2-bridge/releases/latest) 下载对应架构的包（或见[博客文章](https://813146.xyz/post/fnos-aria2-bridge-download-center)内的网盘渠道）：
   - x86_64 设备：`aria2bridge_<版本>_x86.fpk`
   - ARM 设备：`aria2bridge_<版本>_arm64.fpk`
3. 飞牛应用中心 → 手动安装 → 选择 fpk，按向导填写：
   - 飞牛账号（**未开启两步验证**的账号）与密码
   - 服务端口（默认 `6800`）
   - RPC 密钥（Secret，设置独立的非空随机密钥，不要复用飞牛账号密码）
   - 默认保存目录

## 管理页登录与升级

打开桌面图标或 `http://<飞牛IP>:6800/` 时，浏览器会要求登录：用户名为
`aria2bridge`，密码为安装时填写的 **RPC Secret**，不是飞牛账号密码。
设置、自检、日志下载继续使用同一登录。修改 Secret 后，浏览器需要使用新密钥重新登录，
所有 RPC 客户端也要同步更新。

升级旧安装时，请先通过飞牛应用设置配置一个独立的非空随机 Secret，并更换旧安装向导
曾预填的默认密钥。空密钥配置不再允许匿名访问：管理接口返回 503，RPC 返回认证错误；
通过飞牛应用设置补填 Secret 后重启桥接即可恢复。已有自定义非空密钥无需变更。

管理接口不接受跨源浏览器请求，且不再提供通配 CORS。RPC 客户端仍在每个调用的首个
参数发送 `token:secret`，不需要 HTTP Basic；`system.multicall` 的每个内部调用分别带 token。

HTTP Basic 与 HTTP RPC 都不会加密传输。仅在受控网络使用，建议通过 HTTPS 反向代理
保护传输并限制来源地址；不要直接映射公网端口，也不要在 URL 中放置管理密码。

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

打 ARM 包：把 `manifest` 里的 `platform` 改成 `arm` 再 `fnpack build`。`cmd/main` 会按宿主机
架构自动挑 trim-cli（原生 aarch64 > 系统自带 > PATH > 随包 x86）。若希望在没有原生 trim-cli 的
ARM 设备上也能跑，需额外把 **aarch64 版** `qemu-x86_64-static` 放进 `app/bin/`，并把 x86 sysroot
（`ld-linux-x86-64.so.2`、`libc.so.6` 等）放进 `app/x64root/`；缺这两个目录时会直接回退到系统
trim-cli，找不到则启动时在日志里给出提示。

包内目录结构：

```
manifest        # fpk 元信息（appname/version/依赖声明等）
cmd/            # 生命周期脚本（start/stop/status/安装卸载回调）
config/         # 权限与资源声明
wizard/         # 安装/卸载/设置向导表单
app/            # 应用本体（bridge.py + 内置 trim-cli）
test/           # 集成测试（mock trim-cli，38 项）
```

运行测试：`python test/test_bridge.py`（自动启动 bridge + mock trim-cli，覆盖协议层全部方法与容错分支）及 `python test/test_auth.py`（隔离的本机认证、管理权限和跨源回归测试，无需 NAS）。

## 已知限制

- 提供 **x86_64** 与 **ARM64** 两个包，按飞牛设备架构选择
- ARM 包优先使用飞牛系统自带的 `/usr/trim/bin/trim-cli`（aarch64 原生）；若系统未提供，则用随包的 qemu-user 静态解释器运行 x86 版 trim-cli，此时会有明显性能损耗。也可自行把 aarch64 版 trim-cli 放到应用目录并命名为 `trim-cli-linux-arm64`
- 飞牛 `task.addUris` 不支持 **ed2k** 链接（http/https/ftp/sftp/magnet 可用）
- 状态页及管理接口需要认证；仍请勿把 6800 端口直接映射到公网
- 需使用**未开启两步验证**的飞牛账号（桥接需程序化登录）
- 飞牛账号密码仅保存在应用配置目录（`config.json`，权限 600），建议使用专用低权限账号
- 接口基于 fnOS 当前版本的 `appcgi.downloadcenter.*`，飞牛大版本更新后如有变化需适配

## 版本历史

| 版本 | 要点 |
|---|---|
| 1.0.19 | **安全修复**：RPC 每次调用强制校验 token、管理接口改用 HTTP Basic、跨源管理请求拒绝、空 Secret 失败关闭、向导不再预填公开密钥；**新增 ARM64 包** |
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
