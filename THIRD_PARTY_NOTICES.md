# Third-Party Notices

video-catcher 自身以 **GPL-3.0-only** 发布。

本项目通过 Python 包、系统命令或运行时下载方式使用下列独立开源项目。除非特别说明，这些项目的源码并未直接包含在 video-catcher 压缩包中；它们仍受各自许可证约束。

| 项目 | 用途 | 上游许可 |
| --- | --- | --- |
| [Requests](https://github.com/psf/requests) | HTTP 请求、直链下载、页面读取 | Apache-2.0 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 视频平台解析与下载 | Unlicense（不同发行物可能包含额外第三方许可） |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | yt-dlp 浏览器/TLS 指纹模拟 | MIT |
| [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) | YouTube Proof-of-Origin Token Provider | GPL-3.0 |
| [FFmpeg](https://github.com/FFmpeg/FFmpeg) | HLS/DASH 下载、音视频合并、ffprobe 验证 | 主要为 LGPL-2.1+；具体二进制可能因编译选项适用 GPL 或其他条款 |

## 说明

- `requirements.txt` 安装的第三方 Python 包不会改变其自身许可证。
- video-catcher 会在需要 YouTube PO Token 时准备 `bgutil-ytdlp-pot-provider` 的运行时 Provider 缓存；该组件仍按其 GPL-3.0 条款使用。
- FFmpeg/ffprobe 不随本压缩包分发，用户应根据自己安装的具体构建版本确认适用许可。
- Node.js 等运行时组件不随本压缩包分发，也分别适用其上游许可。
- 重新发布包含第三方二进制或源码的衍生发行物时，发行者有责任同时满足对应的许可证和通知义务。

本文件用于说明第三方依赖关系，不构成法律意见。
