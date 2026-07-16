# 我会一直看着你

> The four platforms now use a locally built MediaCrawler sidecar pinned to
> commit `d280d22`. MediaCrawler is used under its NON-COMMERCIAL LEARNING
> LICENSE 1.1; this integration is for personal, non-commercial learning and
> research only.

## Login-state crawler sidecar

After `docker compose up -d --build`, open the authenticated account management
page. The platform-session area supports QR login, QR refresh, and logout. When
interactive verification is required, open the local noVNC window directly at
`http://127.0.0.1:7900/vnc.html?autoconnect=1&resize=scale` while the login worker
is active (replace `7900` when `NOVNC_PORT` is customized). `NOVNC_PORT` changes
only the host-published port; websockify remains on container port `7900`. This unauthenticated
endpoint must remain bound to the host loopback interface and is not proxied by
the Web UI; a remote administrator must use an SSH or VPN tunnel.
The bridge API and Chrome debug port are not published.

Browser profiles are isolated in `data/browser/mediacrawler/{platform}`. The
service never accepts platform-account passwords or Cookie uploads and never
exports Cookie values. Automated CAPTCHA solving, proxy pools, multi-account
matrices, DRM bypass, and access-control bypass remain prohibited. The pinned
provider may generate platform request signatures internally as part of its
discovery and staging implementation.

Before the pinned provider's JSONL storage can discard platform fields, the
worker captures the canonical identity, originality/pinned flags, content type,
exact media slots, and unsupported-media state in memory. After successful
execution it atomically emits those records as `bridge-contract.json` schema v1.
The bridge rejects missing or unknown contracts;
it does not guess completeness from filenames. Provider media first lands in
`data/provider-staging/{job_id}`. The main service validates completeness,
relative paths, expected/downloaded counts, file sizes, MIME types, and SHA-256, then uses
the existing sibling-temp-directory and atomic-rename archive flow. The Web UI
also accepts manifest-v1/v2 ZIP imports through the same validation boundary
and targets an explicit existing account or, by default, creates a new isolated
import account.

一个面向单管理员的自托管公开内容监控工具。系统定期检查小红书、抖音、微博和 Bilibili 公开账号，将监控开始后新发现且 Provider 能完整交付的文案、图片和视频归档到服务器文件系统，并通过认证 WebUI 汇总展示。

## 能力与边界

- 四个平台均以固定版本的 MediaCrawler Sidecar 作为主要发现与媒体暂存实现；Provider 内部可能执行平台请求签名并使用本地浏览器登录态。
- Bilibili 和微博在 Provider 不可用或执行失败时可回退到主服务内置的公开页面适配器；抖音和小红书不使用该回退路径。
- 首次轮询会归档最近一条历史内容，其余已发现内容只记录为基线；后续仅归档新发布内容。
- 允许单管理员人工扫码、短信或滑块验证并在本机保存隔离登录态；不托管密码、不上传或导出 Cookie、不自动破解验证码、不使用代理池，也不绕过访问控制或 DRM。
- 平台结构和反自动化策略会变化。受阻账号会在 WebUI 中标记为 `blocked` 或 `error`，不会自动采取规避措施。
- `data/archive` 是规范数据源：内容目录保存 `metadata.json`，`data/archive/_state/accounts` 原子保存 schema-v2 账号账本，包括账号主页、单账号轮询配置、基线、完整性状态、不截断的已完成水位、持久待重试引用或删除墓碑。SQLite 是运行索引；任务历史和 Web UI 会话状态仍只在 `app.db` 中。

## 快速启动

1. 生成带随机会话密钥的私有环境文件（不会覆盖已有 `.env`）：

   ```powershell
   .\scripts\init-env.ps1
   ```

   脚本只随机生成 `SESSION_SECRET`，并有意让 `WEBUI_LOGIN_TOKEN` 保持为空；应用会在每次启动时生成临时登录 Token，并以尽可能严格的 `0600` 权限原子写入 `data/state/webui-login-token.txt`，不会把 Token 值写入日志。脚本本身不会生成单独的明文凭据文件；在 Windows 上还会立即将 `.env` 的 ACL 限制为当前用户、SYSTEM 和 Administrators。旧版本遗留的管理员用户名、密码变量或 `data/state/initial-admin-password.txt` 不再用于 Web UI Token 登录，但在人工清理前仍应作为敏感数据保护。

   也可以使用跨平台入口；它同样从 `.env.example` 生成完整配置、原子拒绝覆盖已有 `.env`，并在 Windows 调用相同的 ACL 脚本、在 Unix 设置文件模式 `0600`：

   ```bash
   python scripts/bootstrap_env.py
   ```

   Windows 上可随时收紧已有 `.env` 和整个 `data` 目录（默认递归处理）：

   ```powershell
   .\scripts\protect-data.ps1
   ```

   如果文件曾由 Docker、Codex 沙箱或其他账号创建、脚本提示当前用户不是 owner，请从“以管理员身份运行”的 PowerShell 执行同一命令。脚本会拒绝把私有数据 ACL 绑定到 Codex 沙箱身份，并在修改第一项之前完成 owner 预检。

   为避免越过目录边界或只保护部分树，脚本在目标本身或任一后代是符号链接、junction 等 reparse point 时会拒绝执行；请先人工确认并移除该路径，不能用脚本静默跳过。

   非 Windows 环境也可以复制 `.env.example`，然后自行写入至少 32 位的随机会话密钥。`SESSION_SECRET` 为空时 Compose 会拒绝启动；`WEBUI_LOGIN_TOKEN` 可以留空。写入后应限制宿主机权限：

   ```bash
   chmod 600 .env
   chmod -R go-rwx data
   ```

2. 按需在 `.env` 中配置固定 Web UI 登录 Token、监听端口或轮询参数：

   ```dotenv
   # 留空时由应用在每次启动时随机生成；生产环境推荐显式设置高熵 Token。
   WEBUI_LOGIN_TOKEN=
   APP_BIND_ADDRESS=127.0.0.1
   WEBUI_PORT=8080
   NOVNC_BIND_ADDRESS=127.0.0.1
   NOVNC_PORT=7900
   POLL_INTERVAL_MINUTES=60
   ```

   显式配置的 `WEBUI_LOGIN_TOKEN` 必须是至少 24 个可打印字符；推荐使用至少 32 字节随机数生成的高熵 Token。应用不会把显式配置或用户提交的 Token 写入日志；使用显式配置启动时，还会删除陈旧的自动 Token 文件，避免误用旧凭据。`WEBUI_PORT` 同时决定容器内 Uvicorn 的监听端口和宿主机回环发布端口。旧 `.env` 中的 `APP_PORT` 仅在没有 `WEBUI_PORT` 时作为兼容回退，已弃用，新配置不应继续使用。

3. 构建并启动。如果 `WEBUI_LOGIN_TOKEN` 留空，从受保护的状态文件读取本次启动自动生成的 Token：

   ```bash
   docker compose up -d --build
   cat data/state/webui-login-token.txt
   ```

   Windows PowerShell 使用 `Get-Content .\data\state\webui-login-token.txt`。服务日志只会报告 Token 文件路径，不会包含 Token 值。

4. 默认打开本机 `http://127.0.0.1:8080`，使用状态文件中读取或 `.env` 中配置的 Token 登录。自动生成的 Token 只在当前进程生命周期内有效；容器或应用重启后会原子替换该文件并生成新 Token，使旧 Web UI 会话失效。需要稳定登录凭据时，应在 `.env` 中显式配置强 `WEBUI_LOGIN_TOKEN`。添加账号时会立即建立基线并归档最近一条历史内容；后续新增内容将自动归档。远程管理请使用 SSH/VPN 隧道或同机 HTTPS 反向代理。Web UI 与 noVNC 均只允许发布到回环地址；`APP_BIND_ADDRESS` 或 `NOVNC_BIND_ADDRESS` 配置为非回环地址时，相应容器会拒绝启动。

> **公网安全警告：** 不要将 Web UI 或 noVNC 端口直接发布到公网或局域网。HTTP 会明文传输登录 Token，因此 `.env` 和 `data/state/webui-login-token.txt` 都必须按敏感凭据保护；Token 不应写入日志。noVNC 没有应用级认证。需要远程访问时，应通过 VPN/SSH 隧道，或由同机 Caddy、Nginx 等以域名和 HTTPS 反向代理 `127.0.0.1:<WEBUI_PORT>`，并设置 `COOKIE_SECURE=true`；noVNC 仍应只经受控隧道访问。

## 数据目录

```text
data/
├── archive/{platform}/{account}/{year}/{month}/{content_id}/
│   ├── content.md
│   ├── metadata.json
│   └── media/
├── archive/_state/accounts/{platform}/{account}.json  # schema-v2 ledger/tombstone
├── browser/mediacrawler/{platform}/
├── provider-staging/{job_id}/
├── provider-state/
├── state/app.db
└── state/webui-login-token.txt  # only while using an auto-generated token
```

Worker 会在上游 JSONL 序列化前从平台原始对象捕获规范 ID/URL、原创与置顶标志、内容类型、精确媒体槽位和不支持状态，并在 Provider 成功结束后原子写出 `bridge-contract.json` schema v1；Bridge 不接受缺失、未知或自相矛盾的 contract。Provider 媒体同时写入 `provider-staging`，并明确给出期望数量、实收数量和完整标记。主服务校验相对路径、唯一性、数量、大小、MIME、内容类型和 SHA-256 后，再将单条内容写入归档同级 `.tmp-*` 目录。任何期望媒体缺失都会拒绝发布，并以 `pending_refs` 持久留在 schema-v2 账号账本中，后续即使该引用滑出当前 500 条发现窗口也会继续重试；全部校验成功后才清除 pending 并原子移动到正式目录。磁盘剩余空间低于 `MIN_FREE_DISK_GB` 时下载自动暂停，已有归档不会被删除。

Compose 还通过 `ARCHIVE_MEMORY_LIMIT`（默认 `2g`）和 `CRAWLER_MEMORY_LIMIT`（默认 `3g`）限制容器内存。固定 Provider 的部分平台媒体客户端仍会先在内存中接收单个文件；若异常大文件触发内存上限，容器会重启，主服务可能对 Bilibili、微博进入有限 fallback，或将小红书、抖音及 fallback 失败的引用保留为待重试。不完整的 Sidecar 暂存结果不会形成“完整”归档。可按宿主机容量调整上限，但不应取消限制。

## 备份与恢复

复制整个 `data/archive` 后，可用以下命令重建内容索引和监控连续性：

```bash
docker compose exec archive python -m app.cli rebuild-index
```

不使用容器时，从仓库根目录运行：

```bash
python -m backend.app.cli rebuild-index
```

该命令会重新校验归档文件的身份、路径、数量、大小和 SHA-256，并从 `_state/accounts` 的 schema-v2 账本恢复账号主页 URL、启用状态、轮询间隔、首次基线、完整性标记、全部终态已见 ID 和待重试引用；删除墓碑会阻止旧监控配置被恢复。若该 slug 下仍有规范归档，重建内容索引时可以创建一个禁用的 `recovered://` 占位账号来归属内容，但它不会恢复为启用的监控账号。该命令不会恢复历史任务记录或 Web UI 会话状态；需要保留这些运行信息时，仍应在服务停止或使用一致性 SQLite 备份机制的情况下同时备份 `data/state/app.db`。平台登录态与 Web UI 登录态是两类不同数据：恢复 MediaCrawler 的平台登录凭据需要备份 `data/browser/mediacrawler`；当前 Bridge 还以 `data/provider-state/{platform}.json` 中的 `authenticated` 状态作为准入门，因此若未同时恢复 `data/provider-state`，恢复后必须重新发起登录以重建状态。继续使用一个既有 Web UI 登录会话必须同时保留显式配置且相同的 `WEBUI_LOGIN_TOKEN`、原来的 `SESSION_SECRET`（通常都在 `.env` 中）、`app.db` 中的会话状态，以及客户端浏览器中尚未过期的 Cookie；如果 Token 留空，应用重启时生成的新 Token 会使旧会话失效，备份自动 Token 文件也不会改变这一点。服务端备份也无法重建已丢失的客户端 Cookie。`.env`、`data/state/webui-login-token.txt`、`app.db`、`data/browser` 和平台会话状态都含敏感信息，只应加密备份并限制宿主机权限；`provider-staging` 是临时运行数据，`provider-state` 不是规范内容源，但当前实现恢复平台会话准入时需要它，除非接受重新登录。

## 本地开发

后端（从 `backend` 目录运行）：

```bash
cd backend
python -m pip install -r ../requirements.txt
python -m playwright install chromium
python -m uvicorn app.main:app --reload
python -m pytest tests
```

前端（从仓库根目录运行）：

```bash
pnpm --dir frontend install
pnpm --dir frontend dev
pnpm --dir frontend test
pnpm --dir frontend build
```

Vite 开发服务器会将 `/api` 代理到 `http://localhost:8000`。

## 运行机制

- 全局调度器每分钟扫描到期账号，每个账号默认 60 分钟轮询并加入 0–5 分钟随机偏移。
- 单平台同一时间只运行一个账号采集，媒体下载默认最多并发 2 个。
- 失败采用指数退避，最长延迟 24 小时；一个内容下载失败不会阻断同账号其他新增内容。
- Sidecar 每次最多发现最新 500 条公开原创内容，所有终态已见 ID 都写入独立观察表和归档恢复账本，不再以 100 条截断。失败或媒体不完整的引用以 `pending_refs` 单独持久化，成功归档前不会被当成已完成；若已饱和的发现窗口与既有水位完全不重叠，账号会保留 `gap_detected` 状态，避免把可能漏采误报为完整。
- Bilibili、微博的有限回退适配器仍最多返回 20 条；调度器会并行处理到期账号，同时保持同平台一次只运行一个采集任务。

## 平台适配说明

- Bilibili：MediaCrawler 为主要 Provider；固定 commit 的健康主链路当前只发现创作者投稿视频，并仅把详情字段 `copyright == 1` 的投稿视为原创。Provider 不可用或执行失败时，内置适配器会用公开详情 API 再次校验作者与原创标记，并只归档验证通过的视频、使用 yt-dlp 处理媒体；转载、作者不符以及无法验证原创性的动态和专栏不会归档。系统不会把两条发现结果静默合并，因此动态和专栏仍是已知覆盖缺口。
- 微博：MediaCrawler 为主要 Provider；有限回退适配器使用移动端公开数据响应或公开页面，并主动排除转发微博。
- 抖音：通过 MediaCrawler 的平台登录态发现公开视频和图文并暂存媒体，不使用内置适配器回退。
- 小红书：通过 MediaCrawler 的平台登录态发现公开笔记并暂存媒体，不使用内置适配器回退。

主要采集链路依赖固定 commit 的 MediaCrawler、隔离浏览器登录态和 Docker 内部 Bridge；Bilibili、微博回退路径仍依赖公开页面。平台改版或 Provider 输出变化后可能需要更新桥接规范化逻辑、固定样本或回退适配器。请先运行“测试”操作检查主页是否能识别最新内容。
