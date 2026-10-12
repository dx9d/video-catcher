# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from common import human_size
from models import DownloadRecord, MediaCandidate
from format_inspector import MediaFormat


PUBLIC_USE_NOTICE = (
    "请仅处理你有权访问、复制和保存的内容；技术上可下载不代表获得转载、传播或商业使用授权。"
    "本工具不用于绕过 DRM、付费墙、会员/账号权限或其他访问控制。"
)
PUBLIC_USE_NOTICE_JSON = (
    "Use only for content you are authorized or otherwise legally permitted to access, copy, and save. "
    "Technical downloadability does not grant redistribution or commercial-use rights. "
    "Do not use this tool to bypass DRM, paywalls, account/ membership permissions, or other access controls."
)


def write_inspect_report(out_dir: Path, url: str, candidates: list[MediaCandidate], notes: list[str]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "inspect-report.json"
    md_path = out_dir / "inspect-report.md"
    payload = {
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": url,
        "notes": notes,
        "candidates": [item.public_dict() for item in candidates],
        "use_notice": PUBLIC_USE_NOTICE_JSON,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 视频资源检查报告", "", f"- 页面：<{url}>", f"- 候选资源：{len(candidates)}", ""]
    for note in notes:
        lines.append(f"- {note}")
    lines += ["", "| # | 分数 | 类型 | 来源 | 大小 | DRM | URL |", "| ---: | ---: | --- | --- | ---: | --- | --- |"]
    for index, item in enumerate(candidates, 1):
        if item.drm_status == "protected":
            drm = f"是：{item.drm_note}" if item.drm_note else "是"
        elif item.drm_status == "clear":
            drm = "否"
        elif item.drm_status == "unknown":
            drm = f"未知：{item.drm_note}" if item.drm_note else "未知"
        else:
            drm = "未检查"
        lines.append(
            f"| {index} | {item.score} | {item.kind} | {item.source} | {human_size(item.content_length) if item.content_length else ''} | {drm} | <{item.url}> |"
        )
    lines += ["", "---", "", f"> 使用声明：{PUBLIC_USE_NOTICE}"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path



def write_formats_report(
    out_dir: Path,
    url: str,
    formats: list[MediaFormat],
    notes: list[str],
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "formats-report.json"
    md_path = out_dir / "formats-report.md"
    public_meta = {
        key: value for key, value in (metadata or {}).items()
        if key in {"title", "id", "extractor", "extractor_key", "duration", "webpage_url", "live_status"}
    }
    payload = {
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": url,
        "metadata": public_meta,
        "notes": notes,
        "formats": [item.public_dict() for item in formats],
        "use_notice": PUBLIC_USE_NOTICE_JSON,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["# 可下载清晰度报告", "", f"- 页面：<{url}>"]
    if public_meta.get("title"):
        lines.append(f"- 标题：{public_meta['title']}")
    if public_meta.get("duration"):
        lines.append(f"- 时长：{public_meta['duration']} 秒")
    lines.append(f"- 可识别清晰度：{len(formats)}")
    for note in notes:
        lines.append(f"- {note}")
    lines += [
        "",
        "| # | 清晰度 | 分辨率 | FPS | 视频编码 | 音频 | 容器 | 码率 | 预计大小 | 来源 | format id |",
        "| ---: | --- | --- | ---: | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for index, item in enumerate(formats, 1):
        resolution = f"{item.width}×{item.height}" if item.width and item.height else (f"?×{item.height}" if item.height else "")
        bitrate = f"{item.bitrate / 1_000_000:.2f} Mbps" if item.bitrate else ""
        fps = f"{item.fps:g}" if item.fps else ""
        lines.append(
            f"| {index} | {item.label} | {resolution} | {fps} | {item.video_codec} | {item.audio_codec} | "
            f"{item.container} | {bitrate} | {item.display_size} | {item.source} | {item.format_id} |"
        )
    if formats:
        lines += [
            "",
            "用户从列表明确选择某一清晰度时，应使用 `--quality <高度> --quality-mode exact`；"
            "如果用户说“最高不超过 1080p”，则使用 `--quality 1080p --quality-mode at-most`。",
        ]
    lines += ["", "---", "", f"> 使用声明：{PUBLIC_USE_NOTICE}"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


def write_download_report(out_dir: Path, records: list[DownloadRecord], notes: list[str]) -> tuple[Path, Path]:
    json_path = out_dir / "download-report.json"
    md_path = out_dir / "download-report.md"
    payload = {
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "notes": notes,
        "records": [item.public_dict() for item in records],
        "use_notice": PUBLIC_USE_NOTICE_JSON,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = sum(1 for item in records if item.status == "ok")
    skipped = sum(1 for item in records if item.status == "skipped")
    failed = len(records) - ok - skipped
    lines = [
        "# 视频下载报告", "",
        f"- 输出目录：`{out_dir}`",
        f"- 成功：{ok}",
        f"- 已跳过：{skipped}",
        f"- 失败：{failed}",
    ]
    for note in notes:
        lines.append(f"- {note}")
    lines += ["", "| 状态 | 策略 | 文件 | 大小 | 验证 | 原始链接 | 备注 |", "| --- | --- | --- | ---: | --- | --- | --- |"]
    for item in records:
        files = "<br>".join(f"`{Path(path).name}`" for path in item.files)
        verified = "✓" if item.verified is True else ("✗" if item.verified is False else "未验证")
        note = (item.note + ("; " if item.note and item.verification_note else "") + item.verification_note).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {item.status} | {item.strategy} | {files} | {human_size(item.bytes) if item.bytes else ''} | {verified} | <{item.source_url}> | {note} |"
        )
    lines += ["", "---", "", f"> 使用声明：{PUBLIC_USE_NOTICE}"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


def write_task_state(out_dir: Path, config: dict[str, Any]) -> Path:
    path = out_dir / "task.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
