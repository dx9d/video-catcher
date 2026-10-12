# video-catcher

面向 **Codex、Claude Code 等本地 Agent** 的视频资源发现与下载 Skill。

它的目标很简单：用户只提供一个视频或网页链接，Agent 自动尝试解析、下载、合并和验证，不要求用户理解 `yt-dlp`、Cookie、HLS/DASH 或 FFmpeg。

```text
用户：下载这个视频：https://example.com/watch/123
                         ↓
                   video-catcher
                         ↓
        解析 → 发现 → 下载 → 合并 → 验证 → 报告
```

> [!IMPORTANT]
> **使用与免责声明**：请仅使用 video-catcher 处理你有权访问、复制和保存的内容。本项目不用于绕过 DRM、付费墙、会员/账号权限或其他访问控制，也不授予任何第三方内容的版权或再分发权。能够访问某个视频，不等于自动拥有下载、转载或传播该内容的权利。使用者应自行遵守适用法律法规、平台服务条款和内容许可。完整说明见 [`DISCLAIMER.md`](DISCLAIMER.md)。

> 本项目不会绕过 Widevine、FairPlay、PlayReady 等 DRM，也不能替代账号权限、会员权限或人工验证码。

## 主要能力

- **一句话下载**：网页链接、平台视频链接、MP4/WebM/MOV/MKV 直链、HLS `m3u8`、DASH `mpd`
- **平台解析**：优先使用 yt-dlp，覆盖 YouTube、Bilibili、Vimeo、X、TikTok、抖音、Instagram、Facebook 等其支持的平台
- **网页视频发现**：解析 `<video>`、`<source>`、meta、JSON 媒体字段和常见媒体 URL
- **清晰度探测**：读取 yt-dlp formats、HLS master playlist、DASH Representation
- **精确画质下载**：支持 `360p / 720p / 1080p / 2K / 4K / best`，可选择精确画质或最高不超过指定画质
- **分离音视频合并**：自动处理 video-only / audio-only 轨道并调用 FFmpeg 合并
- **断点续传与重试**：直链 `.part + HTTP Range`、yt-dlp continue、分片重试
- **YouTube 专用恢复路线**：普通解析失败后可自动尝试 PO Token Provider、浏览器指纹模拟和 `web_safari` client
- **批量下载与并发**：`--url-file` 批量 + `-j N` 并发控制
- **下载历史去重**：自动记录已下载 URL，重复链接默认跳过，`--force` 重新下载
- **公众号文章离线保存**：将微信公众号文章保存为离线 HTML + Markdown + 图片
- **DRM 检测**：区分 `protected / clear / unknown / not-checked`
- **下载验证**：使用 ffprobe 检查最终媒体文件
- **任务报告**：输出 Markdown / JSON 下载报告和 formats 报告

## 安装

### 1. Python 依赖

建议使用 Python 3.11+：

```bash
python -m pip install -U -r requirements.txt
```

### 2. FFmpeg

确保 `ffmpeg` 和 `ffprobe` 已加入 PATH：

```bash
ffmpeg -version
ffprobe -version
```

### 3. Lux（可选）

Lux 用于 Bilibili 等国内平台的 yt-dlp 兜底。安装后将其加入 PATH，或设置 `LUX_BIN` 环境变量指向二进制。

### 4. Node.js（可选，仅 YouTube PO Token 路线）

命中 YouTube 风控错误时，会自动构建 `bgutil-ytdlp-pot-provider`，需要 Node.js >= 20 与 npm/npx。

## 快速开始

```bash
# 下载单个视频（默认最高不超过 1080p）
python scripts/video_catcher.py download "https://example.com/video.mp4" --title "我的视频"

# 精确 1080p
python scripts/video_catcher.py download "URL" --quality 1080p --quality-mode exact

# 批量下载（一行一个 URL），3 个并发
python scripts/video_catcher.py download --url-file urls.txt --title "批量素材" -j 3

# 查看网页中有哪些可下载媒体
python scripts/video_catcher.py inspect "URL"

# 列出视频支持的清晰度
python scripts/video_catcher.py formats "URL"

# 恢复中断的任务
python scripts/video_catcher.py resume "输出目录/2026-01-01-任务名"

# 保存微信公众号文章为离线文档
python scripts/video_catcher.py article "https://mp.weixin.qq.com/s/xxx" --title "可选标题"
```

## 自动链路

`download` 默认按以下顺序自动尝试，非交互式：

```text
yt-dlp
→ Lux（部分国内平台）
→ YouTube PO Token Provider（仅 YouTube 风控错误）
→ impersonation
→ web_safari（YouTube）
→ static discovery
```

## 输出

默认输出到 `~/Videos/YYYY-MM-DD-任务名/`（可用 `--out-root` 或环境变量 `VIDEO_CATCHER_OUT` 覆盖），目录包含：

```text
task.json                 # 任务配置（支持 resume）
download-report.md/json   # 下载报告
info.json / 字幕 / 封面    # yt-dlp 附带元数据
```

`article` 命令输出到 `~/Documents/YYYY-MM-DD-标题/`（可用 `--out-root` 或环境变量 `WECHAT_ARTICLE_OUT` 覆盖）：

```text
index.html      # 离线可读全文（图片已本地化）
article.md      # Markdown 纯文本
images/         # 正文图片
article-report.md/json
```

## 项目结构

```text
video-catcher/
├── SKILL.md                  # Agent 技能指令
├── README.md
├── manifest.json             # Skill 元数据
├── requirements.txt
├── scripts/
│   ├── video_catcher.py      # 主入口（CLI）
│   ├── article_archiver.py   # 公众号文章离线保存
│   ├── ytdlp_downloader.py   # yt-dlp 封装
│   ├── direct_downloader.py  # HTTP 直链下载
│   ├── stream_downloader.py  # HLS/DASH 下载
│   ├── media_muxer.py        # FFmpeg 合并
│   ├── verifier.py           # ffprobe 验证
│   ├── format_inspector.py   # 清晰度探测
│   ├── media_detector.py     # 媒体识别/DRM
│   ├── static_resolver.py    # 静态媒体发现
│   ├── youtube_pot.py        # YouTube PO Token
│   ├── models.py / common.py / report.py / doctor.py / selftest.py
├── references/
│   ├── troubleshooting.md    # 故障排查
│   └── platform-notes.md     # 平台踩坑记录
├── tests/                    # 单元测试
└── evals/                    # Agent 行为评测
```

## 隐私与安全

- 本项目不读取、不导出、不依赖用户正在运行的 Chrome/Edge 浏览器 Cookie 数据库。
- 默认不需要 Cookie；如确有需要，用户可显式提供 Netscape 格式 `cookies.txt`（`--cookies-file`）。
- 生成的报告会自动对 Authorization / Cookie 等敏感头脱敏。
- 项目不会绕过 DRM、付费墙或账号权限。

## 平台笔记

各平台实测经验与踩坑记录见 [`references/platform-notes.md`](references/platform-notes.md)，故障排查见 [`references/troubleshooting.md`](references/troubleshooting.md)。

## 自测

```bash
# 依赖体检
python scripts/doctor.py

# 内置自测（本地 HTTP 服务，无需外网）
python scripts/selftest.py
```

## 许可

[GPL-3.0](LICENSE.txt)。第三方组件许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。