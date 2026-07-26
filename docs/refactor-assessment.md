# 外部 Provider 重构评估（2026-07-26）

## 决策

SeeU 不再内置、构建、固定或修改 MediaCrawler。平台采集被定义为独立的可选
Provider，并通过 [`provider-http-contract.md`](provider-http-contract.md)
与 SeeU 通信。

## 原因

1. MediaCrawler 的 NON-COMMERCIAL LEARNING LICENSE 1.1 明确授予使用、
   复制、修改和合并权，但没有明确授予公开发布、分发或再许可权。
2. 旧仓库中的
   `crawler/upstream_overrides/media_platform/xhs/playwright_sign.py`
   与上游修复具有相同的核心实现；保留署名不能补充缺失的分发授权。
3. 旧 Bridge 虽使用 HTTP，但依赖双方共享 `data/provider-staging`，并不是真正
   的进程、容器或主机边界。
4. 平台登录、浏览器 profile、人工验证和资源消耗应由 Provider 自己管理，
   SeeU 只需要稳定、可验证的数据交换契约。

## 已实施

- 删除仓库内的 `crawler/`、MediaCrawler override、上游许可证副本和专属 worker
  测试。
- 默认 Compose 只构建和运行 SeeU，不下载任何第三方采集器。
- 用 `PROVIDER_BASE_URL` 和 `PROVIDER_API_TOKEN` 配置可选外部 Provider。
- Provider 不再挂载 SeeU 数据目录；媒体通过认证 HTTP 文件端点传输。
- SeeU 将媒体流式写入随机临时目录，并验证数量、大小、Content-Type、
  SHA-256、媒体魔数和累计上限。
- Provider 只返回不透明任务/文件 ID，不返回宿主机路径、Cookie、浏览器存储
  或完整签名媒体 URL。
- 会话、二维码和人工验证地址继续通过契约提供；人工验证界面由 Provider
  自己保护和发布。
- 未配置 Provider 时 SeeU 仍能启动、浏览、导入和恢复归档；Bilibili、微博
  可以尝试有限公开页面回退，小红书和抖音明确要求外部 Provider。

## 仍需部署者完成

- 选择并独立安装具有合适授权的 Provider。
- 如果使用 MediaCrawler，自行确认其许可证、版本、构建和非商业限制。
- 为 Provider 实现 SeeU Provider HTTP v1 适配层。
- 只在回环、私有容器网络或可信 VPN 暴露 Provider，并使用强 Bearer Token。
- 独立备份 Provider 的浏览器 profile 和平台会话。

## 不采用

- 不把 MediaCrawler 作为 Git 子模块或构建时下载项。
- 不发布包含 MediaCrawler 的预构建 SeeU 镜像。
- 不通过改名、删注释或添加 NOTICE 将衍生文件宣称为 MIT。
- 不让 Provider 读取或写入 SeeU 的 `data/`。
- 不自动处理短信、滑块或 CAPTCHA。
- 不上传/导入 Cookie，不接收平台密码。
- 不使用代理池、多账号矩阵、DRM 或访问控制绕过。
