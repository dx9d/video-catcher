# Troubleshooting

## 原则

正常 `download URL` 不应在中途让用户选择下载器、提供 Cookie 或关闭任何浏览器。

自动链路：

```text
yt-dlp
→ Lux（部分国内平台）
→ YouTube PO Token Provider（仅 YouTube 风控错误）
→ impersonation
→ web_safari（YouTube）
→ static discovery
```

## `yt-dlp not installed` / `curl_cffi` 缺失

统一安装：

```bash
python -m pip install -U -r requirements.txt
```

## ffmpeg / ffprobe 缺失

将 FFmpeg 加入 PATH。

ffmpeg 用于分轨合并，ffprobe 用于最终验证。

## YouTube `Sign in to confirm you're not a bot` / 403 / 429 / PO Token

自动顺序：

1. yt-dlp 常规
2. **本地 bgutil PO Token Provider + mweb**
3. curl_cffi impersonation
4. `web_safari` client retry
5. Static discovery

首次命中 PO Token 路线时，Skill 会自动把 `bgutil-ytdlp-pot-provider` 的 provider 源码下载并构建到：

```text
~/.video-catcher/providers/bgutil-ytdlp-pot-provider/1.3.1/
```

运行要求：

- `bgutil-ytdlp-pot-provider` Python plugin
- Node.js >= 20
- npm / npx

任务过程中 provider 只绑定本机 `127.0.0.1` 的临时端口，完成后关闭。如果 provider 安装/构建失败，不要立刻向用户求助，继续后续 fallback。

PO Token 也不是“彻底绕过 YouTube 风控”的保证。如果当前出口 IP 已经直接命中 429/`sorry/index`、内容本身需要账户权限/验证码，自动链路仍可能失败。

## 抖音 `Fresh cookies are needed`

yt-dlp 的抖音 extractor 可能在平台规则变化时要求 fresh cookies，甚至已有 Cookie 仍失败。

video-catcher 会继续：

- curl_cffi impersonation
- 静态 `playAddr/play_addr/downloadAddr` 等字段扫描
- `/video/tos/` extensionless CDN 识别

不要因为 yt-dlp extractor 报 Fresh cookies 就直接结束。

## DRM

发现 Widevine / PlayReady / FairPlay / CENC PSSH / SAMPLE-AES DRM 时停止该候选，不绕过。

普通 HLS AES-128 不简单视作 DRM。

## 静态发现没有结果

`inspect` 会输出候选列表。若静态发现没有找到媒体，常见原因：

- 页面是 SPA，媒体由 JS 动态注入
- 媒体 URL 需要签名（有效期短）
- 页面要求登录或触发风控

此时如实报告“未找到媒体候选”，并附 `inspect-report.md` 供人工排查。

## YouTube PO Token provider 构建失败

先运行：

```bash
python scripts/doctor.py
```

重点检查：

```text
youtube-pot-plugin
node-major
npm
npx
youtube-pot-capable
```

Python plugin 统一通过：

```bash
python -m pip install -U -r requirements.txt
```

Provider 源码由 Skill 在第一次需要时自动准备，不要求用户手工 clone 或启动常驻服务。若 GitHub 当时不可访问、npm 安装失败或 Node 版本过低，任务会回退到其余路线。

## 微信公众号文章抓不到正文

常见原因：

- 文章已删除，或需要关注/登录后可见
- 命中反爬页

此时 `article` 命令仍会生成 index.html（正文占位），报告会注明原因。