# Platform Notes — 平台踩坑记录

各平台/格式的实测经验。随项目更新，欢迎贡献。

## YouTube

- 风控错误特征：`Sign in to confirm you're not a bot`、HTTP 403/429、`sorry/index`、`PO Token required`。
- 应对：`bgutil-ytdlp-pot-provider` 本地 provider + `mweb` client；失败后 `impersonate=chrome`、`web_safari` client 重试。
- PO Token 只解决客户端校验，不解决 IP 级 429 / CAPTCHA / 登录墙。
- 播放列表默认不整单下载，需要显式 `--playlist`；批量多视频用 `--url-file` + `-j`。

## Bilibili

- yt-dlp 可解析多数公开视频，默认最高 1080p。
- 大会员/高码率内容需要账号凭据，本工具不代取。
- 若 yt-dlp 失败且检测到 `bilibili.com`，自动链路会尝试 Lux 下载器（需另行安装并加入 PATH，或设置 `LUX_BIN` 环境变量）。
- 短链 `b23.tv/xxx` 会先展开再解析。

## 抖音 / 快手

- 错误特征：`Fresh cookies are needed`、`ERROR: unable to extract data`。
- 应对：impersonation 重试 → 静态扫描 `playAddr/play_addr/downloadAddr` 等字段 → `/video/tos/` extensionless CDN 识别。
- 短链 `v.douyin.com/xxx` 先展开再解析。
- 无水印直链通常需要登录态，无凭据时以能拿到的公开流为准。

## X / Twitter

- 多数媒体需要登录会话；无凭据时可能只能拿到 poster 或低清流。
- 若页面包含可直接访问的媒体 URL（如 `*.twimg.com` 视频），静态发现仍可能命中。

## 通用网页视频（静态发现）

- `resolve_static` 扫描 `<video>`、`<source>`、`<meta>`、`<script>` 中 JSON 媒体字段。
- 常见媒体模式：`video src`、`mp4/m3u8/mpd` URL、`playAddr/playUrl/play_addr`、`*.mp4?xxx` 签名 URL。
- SPA 页面 JS 动态注入的媒体，静态发现可能失败——如实报告，附 `inspect-report.md`。

## HLS / DASH

- `master.m3u8` 多清晰度：`formats` 会列出变体，用 `--quality 720p --quality-mode exact` 精确选择。
- AES-128 加密的 HLS 不是 DRM，yt-dlp/ffmpeg 可正常下载。
- Widevine / FairPlay / PlayReady / SAMPLE-AES 视为 DRM，停止候选不绕过。

## 微信公众号（article 命令）

- 正文容器：`<div id="js_content">`。
- 图片懒加载：`<img data-src="...">`，下载时带 `Referer` 指向文章 URL，否则部分图片 403。
- 发布时间：优先 `#publish_time`，回退 `var ct = "..."` 时间戳。
- 文章删除/需登录时正文为空，报告注明原因。
- 根相对路径图片（`/xxx.png`）会结合文章域名解析后再下载。

## 反爬与风控通用建议

- 批量下载使用 `-j N` 并发时，N 不要过大（建议 2~3），避免触发目标站点频率限制。
- 重复 URL 默认被历史去重跳过；确需重下用 `--force`。
- 若出口 IP 已被站点风控（429/验证码），本地工具通常无法仅靠代码恢复，建议更换网络出口。
- 遇到平台规则更新导致的 extractor 报错，先看是否是 yt-dlp 版本过旧（`pip install -U yt-dlp`）。