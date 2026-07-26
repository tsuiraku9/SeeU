# SeeU 外部 Provider HTTP 契约 v1

机器可读定义见 [`provider-openapi.yaml`](provider-openapi.yaml)；本文补充运行、
安全和完整性语义。两者有冲突时应视为契约缺陷并在实现前修正，不应自行猜测。

SeeU 不包含、构建或分发任何平台采集器。Provider 是由管理员单独选择、安装、
授权和运行的独立服务。MediaCrawler 只是可能的第三方实现之一，不是 SeeU
依赖，也不受 SeeU 的 MIT License 覆盖。

## 1. 连接与认证

- 在 SeeU 中配置 `PROVIDER_BASE_URL` 和 `PROVIDER_API_TOKEN` 才会启用 Provider。
- 所有请求携带 `Authorization: Bearer <PROVIDER_API_TOKEN>`。
- Token 至少包含 24 个可打印字符；推荐使用 32 字节随机值。
- Provider 应仅监听回环地址、私有容器网络或受保护的 VPN 地址。
- SeeU 不跟随 Provider 返回的 HTTP 重定向。
- Provider 不得在响应、错误或日志中返回 Cookie、浏览器存储、平台密码或完整的
  带签名媒体 URL。

若 SeeU 运行在 Docker 中、Provider 运行在同一宿主机，可以使用：

```dotenv
PROVIDER_BASE_URL=http://host.docker.internal:8090
PROVIDER_API_TOKEN=replace-with-the-same-random-token
```

## 2. 通用错误

所有失败响应使用以下结构：

```json
{
  "detail": {
    "code": "provider_execution_failed",
    "message": "可供管理员采取行动的简短说明",
    "phase": "discovery",
    "retryable": true
  }
}
```

- `401` / `403` 仅表示 Provider API Token 无效。
- 平台尚未登录使用 `409`，且 `code` 为 `login_required`。
- Provider 自身执行失败使用 `5xx`。
- `message` 不得包含 Cookie、Token 或完整的签名查询字符串。

## 3. 平台会话

### `GET /v1/sessions`

返回 Provider 支持的平台会话数组。Provider 可以只返回部分平台。

```json
[
  {
    "platform": "xiaohongshu",
    "status": "authenticated",
    "updated_at": "2026-07-26T12:00:00Z",
    "message": null,
    "manual_verification_url": ""
  }
]
```

允许的 `status`：

- `logged_out`
- `starting`
- `qr_ready`
- `authenticated`
- `expired`
- `manual_verification_required`
- `error`

### `POST /v1/sessions/{platform}/login`

启动交互式登录，返回对应平台的会话对象。可以直接包含二维码：

```json
{
  "platform": "xiaohongshu",
  "status": "qr_ready",
  "updated_at": "2026-07-26T12:00:00Z",
  "message": "请扫码",
  "manual_verification_url": "",
  "image_data_url": "data:image/png;base64,..."
}
```

`image_data_url` 只允许 PNG 或 JPEG，最大约 2 MiB。Provider 应当保存自己的
浏览器配置；SeeU 不接触该目录。

### `GET /v1/sessions/{platform}/qr`

返回当前会话和二维码，结构同上。

### `DELETE /v1/sessions/{platform}`

结束 Provider 中的平台会话并返回 `logged_out` 会话对象。

人工短信、滑块或设备确认由 Provider 自己处理。若需要浏览器界面，Provider
可以返回一个 `http(s)` 的 `manual_verification_url`；该地址的访问控制和安全
发布由 Provider 管理，SeeU 不提供 noVNC。

## 4. 发现创作者内容

### `POST /v1/creators/discover`

请求：

```json
{
  "platform": "bilibili",
  "profile_url": "https://space.bilibili.com/123",
  "limit": 500
}
```

响应必须按“最新优先”排列，最多返回请求的 `limit`：

```json
{
  "items": [
    {
      "remote_id": "BV1...",
      "source_url": "https://www.bilibili.com/video/BV1...",
      "published_at": "2026-07-26T10:00:00Z",
      "original": true,
      "pinned": false,
      "aliases": []
    }
  ],
  "truncated": false
}
```

要求：

- 只返回公开、可确认属于目标创作者且 `original` 为 `true` 的内容。
- `remote_id` 必须稳定；同一内容的历史标识放入 `aliases`。
- 最多 10 个 alias，每个最长 256 字符，不得在不同内容间冲突。
- 达到窗口上限或无法证明返回了完整窗口时，`truncated` 必须为 `true`。
- 页面被拦截、会话失效或输出结构未知时必须失败，不能返回“成功的空数组”。

## 5. 内容暂存

### `POST /v1/content/stage`

请求：

```json
{
  "platform": "bilibili",
  "content_id": "BV1...",
  "source_url": "https://www.bilibili.com/video/BV1..."
}
```

Provider 完成媒体下载和哈希计算后返回清单，但不返回宿主机路径：

```json
{
  "job_id": "job_01J4YQ0G6Q9B",
  "platform": "bilibili",
  "content_id": "BV1...",
  "source_url": "https://www.bilibili.com/video/BV1...",
  "title": "标题",
  "author": "作者",
  "text": "正文",
  "published_at": "2026-07-26T10:00:00Z",
  "content_type": "video",
  "expected_media_count": 1,
  "complete": true,
  "media": [
    {
      "file_id": "video_1",
      "kind": "video",
      "mime_type": "video/mp4",
      "size_bytes": 123456,
      "sha256": "64位小写十六进制SHA-256"
    }
  ]
}
```

约束：

- `job_id` 和 `file_id` 只能包含 ASCII 字母、数字、下划线和连字符，最长
  128 字符。
- `expected_media_count` 必须与 `media` 数量完全一致。
- 只有全部媒体已经准备好时才能返回 `complete: true`。
- 非纯文本内容不得返回零媒体。
- MIME 仅允许 `image/*`、`video/*`、`audio/*`。
- `kind` 必须为 `image`、`video` 或 `audio`，并与 MIME 主类型一致。
- `published_at` 必须是有效的时间戳或 ISO 8601 时间，SeeU 不会猜测发布时间。
- 不完整任务必须返回错误；SeeU 会保留引用并在以后重试。

### `GET /v1/staging/{job_id}/files/{file_id}`

返回单个媒体文件：

- `Content-Type` 必须与清单一致。
- 推荐发送 `Content-Length`；若发送，必须与 `size_bytes` 一致。
- 不得使用压缩 `Content-Encoding`。
- 不得重定向到平台 CDN。
- SeeU 会流式限制字节数、计算 SHA-256、检查文件魔数和 MIME 家族。

### `DELETE /v1/staging/{job_id}`

清理 Provider 自己的临时任务。此接口应当幂等。SeeU 无论归档成功或失败都会
尝试调用它。

## 6. 支持的平台值

- `xiaohongshu`
- `douyin`
- `weibo`
- `bilibili`

SeeU 自带的 Bilibili 和微博公开页面适配器只在外部 Provider 未配置、不可用
或执行失败时作为有限回退；小红书和抖音需要外部 Provider。

## 7. 许可证边界

Provider 的作者和部署者自行负责其依赖、平台条款及许可证。若选择
MediaCrawler，必须独立取得并遵守其非商业学习研究许可证。不要把 MediaCrawler
源码、修改版或预构建镜像提交到 SeeU 仓库或作为 SeeU 发布物分发。
