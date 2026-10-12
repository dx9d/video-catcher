# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import requests

from common import find_executable, video_catcher_home

BGUTIL_VERSION = "1.3.1"
BGUTIL_ARCHIVE_URL = (
    f"https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{BGUTIL_VERSION}.zip"
)
BGUTIL_DISTRIBUTION = "bgutil-ytdlp-pot-provider"


@dataclass
class PotRuntime:
    base_url: str
    provider: str
    note: str

    @property
    def extractor_args(self) -> list[str]:
        return [
            "youtube:player_client=mweb",
            f"youtubepot-bgutilhttp:base_url={self.base_url}",
        ]


def is_youtube_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return "youtube.com/" in lowered or "youtu.be/" in lowered


def pot_worthy_error(note: str) -> bool:
    """Return True for YouTube failures where an automatic PO-token retry is useful."""
    lowered = str(note or "").lower()
    markers = (
        "sign in to confirm",
        "not a bot",
        "http error 403",
        "http error 429",
        "status code 403",
        "status code 429",
        "po token",
        "proof of origin",
        "requested format is not available",
        "nsig",
    )
    return any(marker in lowered for marker in markers)


def bgutil_plugin_version() -> str:
    try:
        return importlib.metadata.version(BGUTIL_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _node_major() -> int:
    node = find_executable("node")
    if not node:
        return 0
    try:
        proc = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return 0
    match = re.search(r"v?(\d+)", (proc.stdout or proc.stderr or "").strip())
    return int(match.group(1)) if match else 0


def pot_prerequisites() -> tuple[bool, str]:
    plugin = bgutil_plugin_version()
    if not plugin:
        return False, "bgutil-ytdlp-pot-provider Python plugin is not installed"
    if plugin != BGUTIL_VERSION:
        return False, (
            f"bgutil plugin/provider version mismatch: plugin={plugin}, provider={BGUTIL_VERSION}; "
            "reinstall requirements.txt"
        )
    node_major = _node_major()
    if node_major < 20:
        if node_major:
            return False, f"Node.js >=20 is required for automatic YouTube PO tokens (found Node {node_major})"
        return False, "Node.js >=20 is required for automatic YouTube PO tokens"
    if not find_executable("npm") or not find_executable("npx"):
        return False, "npm/npx are required to bootstrap the local bgutil PO-token provider"
    return True, f"bgutil plugin {plugin}; Node {node_major}"


def provider_root() -> Path:
    return video_catcher_home() / "providers" / "bgutil-ytdlp-pot-provider" / BGUTIL_VERSION


def provider_server_dir() -> Path:
    return provider_root() / "server"


def _tool_command(tool: str, *args: str) -> list[str]:
    path = Path(tool)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", tool, *args]
    return [tool, *args]


def _download_archive(target: Path, *, timeout: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    limit = 100 * 1024 * 1024
    written = 0
    with requests.get(BGUTIL_ARCHIVE_URL, stream=True, timeout=min(timeout, 90)) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(1024 * 256):
                if chunk:
                    written += len(chunk)
                    if written > limit:
                        raise RuntimeError("bgutil provider archive exceeded the 100 MB safety limit")
                    handle.write(chunk)


def _safe_extract(bundle: zipfile.ZipFile, target: Path) -> None:
    root = target.resolve()
    for member in bundle.infolist():
        destination = (target / member.filename).resolve()
        if destination != root and root not in destination.parents:
            raise RuntimeError("unsafe path detected in bgutil provider archive")
    bundle.extractall(target)


def ensure_bgutil_server(*, timeout: int = 600) -> tuple[Path | None, str]:
    """Download/build the bgutil HTTP provider into video-catcher's private cache."""
    ok, prereq_note = pot_prerequisites()
    if not ok:
        return None, prereq_note

    server_dir = provider_server_dir()
    entrypoint = server_dir / "build" / "main.js"
    marker = provider_root() / ".ready.json"
    if entrypoint.is_file():
        return server_dir, f"cached bgutil provider {BGUTIL_VERSION} ready"

    root = provider_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video-catcher-bgutil-") as raw_tmp:
        tmp = Path(raw_tmp)
        archive = tmp / "provider.zip"
        try:
            _download_archive(archive, timeout=timeout)
            with zipfile.ZipFile(archive) as bundle:
                _safe_extract(bundle, tmp / "src")
            extracted_roots = [path for path in (tmp / "src").iterdir() if path.is_dir()]
            if len(extracted_roots) != 1 or not (extracted_roots[0] / "server").is_dir():
                return None, "downloaded bgutil archive did not contain the expected server directory"

            staged = tmp / "staged"
            shutil.copytree(extracted_roots[0] / "server", staged)
            npm = find_executable("npm") or "npm"
            npx = find_executable("npx") or "npx"
            install = subprocess.run(
                _tool_command(npm, "ci"),
                cwd=staged,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if install.returncode != 0:
                tail = (install.stderr or install.stdout or "").strip().splitlines()
                return None, f"bgutil npm ci failed: {(tail[-1] if tail else install.returncode)[:220]}"
            build = subprocess.run(
                _tool_command(npx, "tsc"),
                cwd=staged,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if build.returncode != 0 or not (staged / "build" / "main.js").is_file():
                tail = (build.stderr or build.stdout or "").strip().splitlines()
                return None, f"bgutil TypeScript build failed: {(tail[-1] if tail else build.returncode)[:220]}"

            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(server_dir))
            marker.write_text(
                json.dumps(
                    {
                        "provider": "bgutil-ytdlp-pot-provider",
                        "version": BGUTIL_VERSION,
                        "source": BGUTIL_ARCHIVE_URL,
                        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return server_dir, f"downloaded and built bgutil provider {BGUTIL_VERSION}"
        except subprocess.TimeoutExpired:
            return None, f"bgutil provider setup timed out after {timeout}s"
        except Exception as exc:  # noqa: BLE001
            return None, f"bgutil provider setup failed: {str(exc)[:220]}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[bytes], *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except Exception:
        pass


@contextlib.contextmanager
def youtube_pot_session(*, setup_timeout: int = 600) -> Iterator[PotRuntime]:
    """Start a task-scoped local bgutil provider and stop it on every exit path."""
    server_dir, setup_note = ensure_bgutil_server(timeout=setup_timeout)
    if server_dir is None:
        raise RuntimeError(setup_note)

    node = find_executable("node")
    if not node:
        raise RuntimeError("Node.js >=20 is required for the bgutil provider")
    port = _free_port()
    log_dir = video_catcher_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "youtube-pot-provider.log"
    log_handle = log_path.open("ab")
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [node, str(server_dir / "build" / "main.js"), "--port", str(port)],
            cwd=server_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        if not _wait_for_port(port, process):
            log_handle.flush()
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-1]
            except Exception:
                pass
            raise RuntimeError(f"bgutil provider did not become ready{': ' + tail[:180] if tail else ''}")
        yield PotRuntime(
            base_url=f"http://127.0.0.1:{port}",
            provider=f"bgutil/{BGUTIL_VERSION}",
            note=setup_note,
        )
    finally:
        if process is not None:
            _stop_process(process)
        log_handle.close()
