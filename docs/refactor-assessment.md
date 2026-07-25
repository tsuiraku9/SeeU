# 平台采集重构评估（2026-07-25）

## 已复现的问题

1. 当前运行实例的抖音账号使用 `v.douyin.com` 短链。容器 DNS 将其
   解析为 Clash/Mihomo Fake-IP `198.18.0.25` 和
   `fdfe:dcba:9876::10`，原有 SSRF 防护在发起 Provider 请求前直接
   返回 `422 Platform URL resolves to a non-public address`。
2. Bridge 的 staging 资源监控存在退出竞态：发现大小或磁盘余量越界
   后先停止 worker，`communicate()` 可能先收到 worker EOF 并返回，
   从而吞掉 `413/507`。回归测试此前为 34 通过、2 失败。
3. 抖音滑块会发布 `manual_verification_required`，但小红书扫码后的
   短信/安全二次验证只在上游日志中出现，Web UI 无法可靠进入人工验证
   状态。
4. Bridge 健康检查用裸 TCP 连接 websockify，每 30 秒制造一次
   `webSocketsHandshake: unknown connection error`，真实平台日志被噪声淹没。
5. 完整测试无法直接在任一生产镜像内运行：archive 镜像不包含
   `crawler` 源码，crawler 镜像不包含主服务依赖。通过只读挂载仓库的
   一次性测试容器可得到真实基线。
6. 小红书监控地址使用 `xhslink.cn` 时被旧白名单拒绝为
   `URL does not belong to xiaohongshu`。该短链实际以 302 跳转到
   `www.xiaohongshu.com/user/profile/...`，应在每一次跳转继续执行
   平台域名、DNS 和公网地址校验，而不是直接拒绝入口域名。
7. 固定上游的微博二维码登录在移动端 User-Agent 下找不到桌面二维码，
   随后调用 `sys.exit()` 并返回进程码 0，旧 Bridge 因而在二维码尚未
   出现时误报登录成功。
8. 发现 worker 曾把包含平台签名查询参数的创作者 URL 放入进程命令行，
   宿主机进程查看工具可能看到完整 URL；这违反了签名 URL 不进入日志或
   可观察进程参数的边界。
9. Bilibili 与小红书 discovery 同时运行时，两个 Chromium 进程树合计
   约 1.1 GiB、437 个 PID/线程并持续占用 CPU，两个本来可单独完成的任务
   均超过 840 秒。Bilibili 随后进入 HTML 回退，但新版空间页不再提供
   可识别投稿链接，最终两条路径同时失败。

## 上游与同类项目结论

- 固定的 MediaCrawler `d280d22` 发布于 2026-05-25。上游在
  2026-07-24 的
  [`17f6612`](https://github.com/NanmiCoder/MediaCrawler/commit/17f66121e0fcc40fc23958b995bec873d422667d)
  升级 `xhshow` 至 0.2.0，并将小红书 GET 签名从私有方法 monkey-patch
  改为公开 API；本项目应做窄范围、可审计的兼容回移，而不是解除固定
  commit。
- MediaCrawler 的
  [小红书登录问题 #203](https://github.com/NanmiCoder/MediaCrawler/issues/203)
  和
  [扫码后无登录信息 #852](https://github.com/NanmiCoder/MediaCrawler/issues/852)
  表明有界面浏览器、持久 profile 和人工验证仍是必要路径，不能把扫码
  成功等同于 API 会话可用。
- [ShunL12324/xhs-mcp](https://github.com/ShunL12324/xhs-mcp)
  把登录、状态检查、短信验证建模为独立状态机；可复用的是状态边界，
  不是自动化验证码。
- [xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)
  将首次人工登录和长期无头服务分离，并持久化浏览器会话。当前项目应
  保持每平台独立 profile，并让登录 worker 与采集 worker 共享同一份
  profile，而不接收 Cookie 上传。
- [OpenCLI](https://github.com/jackwener/opencli) 将平台适配、下载能力和
  稳定退出码分离。Bridge 后续应采用结构化错误码，避免主服务依赖英文
  错误字符串判断登录过期或人工验证。
- [yt-dlp 的 Bilibili extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)
  已实现空间投稿列表的 WBI 参数、分页以及 412/401/-352 拒绝诊断。项目
  已经依赖 yt-dlp 下载 B 站媒体，因此有限回退应复用这一维护中的列表
  实现，而不是复制一套易失效的签名算法或继续依赖空间 HTML。

## 已实施的重构

### 第一阶段：运行基础稳定性

- Fake-IP 兼容必须显式启用，只允许标准 Fake-IP 网段；其他私网地址继续
  阻断。
- 修复 staging 资源监控竞态，不完整结果永不晋升为归档。
- 小红书二次验证只发布状态和 noVNC 指引，验证码仍由管理员在平台页面
  人工输入。
- 健康检查验证 X11 socket、x11vnc/websockify 进程和 noVNC 静态入口，
  不再建立会被误记为 WebSocket 握手失败的 VNC 探测连接。
- 回移 xhshow 0.2.0 公共签名 API，同时保持 MediaCrawler 源码基线
  `d280d22`。
- `xhslink.com` 与 `xhslink.cn` 入口均经过逐跳重验证后规范化为小红书
  创作者主页。
- 微博登录 worker 强制使用桌面 User-Agent，并把上游零退出码
  `SystemExit` 转换为结构化失败；只有真实二维码就绪或持久会话通过检查
  才能发布成功状态。

### 第二阶段：拆分 Bridge 单体

- 将 `crawler/worker.py` 拆为 `contracts`、`sessions`、`network_policy`
  和四个平台兼容模块。
- 每个平台模块声明其固定上游符号、规范 ID、原创判断、媒体槽位和支持
  类型；启动时做兼容性自检，缺少符号时拒绝健康状态。
- Bridge 返回结构化 `error_code`、`phase`、`retryable` 和截断后的安全
  diagnostics，主服务不再匹配 `login/cookie/verify` 字符串。
- 已落地 `network_policy.py`、`session_state.py`、
  `contract_validation.py`、`worker_protocol.py` 和
  `upstream_compatibility.py`。健康检查会对四个平台的固定上游符号做
  fail-closed 自检。
- Bridge 通过权限为 `0600` 的原子 `bridge-request.json` 传递任务参数；
  worker 读取后立即删除。真实容器验证中，worker 命令行不再出现 URL、
  查询串、`--value` 或 `--limit`。
- 主服务会同时保存 MediaCrawler 主链路与 Bilibili/微博有限回退链路的
  脱敏诊断，避免后一个异常覆盖真正的首发故障。
- 调度采集默认由 `CRAWLER_POLL_CONCURRENCY=1` 全局串行，队列等待发生在
  发出 Provider HTTP 请求之前；这避免多个浏览器互相争抢资源并耗尽各自
  的 840 秒执行预算。高资源服务器仍可显式提高并发，但需要重新做真实
  平台烟雾测试。
- crawler 重启时会无条件删除已失去调用者的 `discover-*` 目录；可能仍被
  主服务复制的内容 staging 目录继续按 24 小时 TTL 处理，避免重启清理与
  原子归档发生竞态。
- 主服务的 multipart、Playwright 和通用 `tempfile` 不再直接污染
  `provider-staging` 根目录，而是统一进入专用 `.runtime-tmp`；入口脚本
  启动时只清空这一明确的临时子目录，不触碰显式 import/stage 作业。
- Bilibili 有限回退在空间 HTML 失效时复用项目已有 yt-dlp 的 WBI 空间
  列表实现，最多只取 20 条规范 BV ID；候选仍逐条通过公开详情 API 校验
  创作者归属和 `copyright == 1`，不把 yt-dlp 列表本身当作原创性证明。
- 固定上游 Bilibili creator 会为窗口内每条投稿查询详情，并在持有并发
  信号量期间休眠 2 秒；单并发处理 210 条已经超过 9 分钟，500 条窗口
  无法在 840 秒内完成。并发 2 的实测速度仍使 500 条窗口贴近超时边界，
  因此仅该 discovery 路径默认使用有界并发 3（可配置
  1–4），媒体暂存及其他平台仍为 1。

### 第三阶段：真实平台验证

- 为四个平台各保存一份脱敏的 discovery/detail contract 夹具，覆盖
  图文、视频、置顶、转载、空媒体和多段媒体。
- 增加 opt-in 烟雾测试，只使用管理员已人工登录的本地 profile，不在 CI
  中保存登录态。
- 验证矩阵至少包含：首次基线、第二次无新增、单条新增、下载中断后重试、
  500 条窗口饱和、会话过期和服务器重启恢复。

截至 2026-07-25，四个平台的真实账号均为 `healthy`、连续失败计数为 0，
且完整性状态为 `complete`：

- 小红书 `xhslink.cn` 真实账号首次发现 310 条，原子归档最新 1 条笔记及
  2 个媒体文件；独立第二次轮询再次返回 310 条、0 新增、无待重试引用，
  未重复归档且未检测到窗口缺口。
- 抖音真实账号已完成首次基线归档；后续轮询返回 11 条、0 新增，既有
  1 条归档及 1 个媒体文件保持完整。
- Bilibili 在全局串行、详情并发 3 下约 6 分 40 秒发现 405 条原创投稿，
  原子归档最新 1 条视频及 1 个媒体文件。
- 微博管理员扫码后真实发现 178 条；后续页被平台拒绝时保留已验证的
  最新窗口并明确标记窗口截断，首次基线原子归档 1 条及 18 个媒体文件。

四个平台的全部归档均重新验证了身份、MIME、大小和 SHA-256，且不存在
`.tmp-*` 目录。紧接 500 条窗口后实测 Bilibili 的 yt-dlp 回退被平台
限流拒绝，因此它只作为有限备援并保留明确错误，不能替代健康主链路。

## 不采用的方案

- 不自动处理短信、滑块或 CAPTCHA。
- 不上传/导入 Cookie，不接收平台密码。
- 不用代理池或多账号矩阵降低平台风控。
- 不以“下载到几个文件”推断完整性，不弱化 contract/哈希/原子晋升边界。
