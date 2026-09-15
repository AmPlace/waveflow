import asyncio
import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import json
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, model_validator
from contextlib import asynccontextmanager
import database
import epg_binding_management
import epg_management
import epg_read_resolver
import epg_source_management
from adapters import (
    AdapterResolveError,
    adapter_supports,
    parse_adapter_url,
    resolve_adapter_source,
)
from iptv_probe import probe_channel_source
from media_tools import media_tool_bin
from automation import (
    AutomationBusy,
    AutomationConfigurationError,
    AutomationOwnershipLostError,
    AutomationRequestError,
    AutomationRunRequest,
    AutomationRunResult,
    AutomationTaskNotFoundError,
)
from market_tasks import (
    MARKET_MAXIMUM_INTERVAL_SECONDS,
    MARKET_MINIMUM_INTERVAL_SECONDS,
    MARKET_TASK_ID,
)
from epg_tasks import (
    create_production_automation_service,
    reconcile_epg_tasks_after_source_change,
    run_epg_refresh_now,
    run_epg_source_refresh_now,
)
from radio_tasks import reconcile_radio_automation_tasks
from ssrf_guard import UnsafeTargetError, assert_safe_target_url, assert_safe_host_ips
from core.config import get_settings
from core.settings_service import get_effective_settings, get_effective_settings_sync
from infrastructure.http_client import (
    ResponseTooLargeError,
    RedirectTargetRejected,
    StatelessAsyncClient,
    default_policy,
    fetch_bytes,
    request_with_safe_redirects,
    stream_with_safe_redirects,
)
from routers.auth import router as auth_router
from routers.media_credentials import router as media_credentials_router
from routers.media_proxy import router as media_proxy_router
from routers.settings import router as settings_router
from routers.plugins import router as plugins_router
from plugin_production import ProductionPluginSubsystem, default_plugin_root
from plugin_tasks import register_plugin_update_task
from provider_resolver import ProviderResolver
from radio_core import RadioResolver
from rtsp_playback import (
    RtspPlaybackOptions,
    RtspVideoMode,
    resolve_rtsp_playback_options,
    rtsp_video_args,
)
from routers.radio import router as radio_router
from routers.setup import router as setup_router
from security.dependencies import require_admin, require_browse_access, require_media_access, resolve_media_access
from security.source_ids import source_id_for, source_revision_for


TOKEN_REFRESH_INTERVAL_SECONDS = 18_000

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
IPTV_STREAM_READ_TIMEOUT_SECONDS = 12.0
IPTV_STREAM_RECONNECT_DELAY_SECONDS = 0.25
IPTV_STREAM_RECONNECT_MAX_DELAY_SECONDS = 2.0
IPTV_STREAM_NO_DATA_RETRIES = 5
IPTV_STREAM_NO_DATA_TIMEOUT_SECONDS = 45.0
IPTV_STREAM_SHORT_CONNECTION_BYTES = 256 * 1024
IPTV_STREAM_SHORT_CONNECTION_SECONDS = 2.0

# 规避部分Hinet CDN的验证问题
CDN_VERIFY_SSL = False


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "") or default))
    except ValueError:
        return default


CDN_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

def get_cdn_headers_for_station(station_id: str) -> dict[str, str]:
    return CDN_REQUEST_HEADERS.copy()

TOKEN_REFRESH_HTTP_STATUS_CODES = {401, 403, 404, 410}

_AUDIO_MIME_ALIASES = {
    "audio/mp3": "audio/mpeg",
    "audio/x-mp3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/x-aac": "audio/aac",
    "application/aac": "audio/aac",
    "audio/vnd.dlna.adts": "audio/aac",
}
_AUDIO_MIME_BY_EXTENSION = {
    "aac": "audio/aac",
    "adts": "audio/aac",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "oga": "audio/ogg",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "wav": "audio/wav",
    "webm": "audio/webm",
}


def _audio_proxy_content_type(upstream_url: str, upstream_content_type: str) -> str:
    """Return a safe media type for a stream declared as ``audio_http``."""
    raw_content_type = str(upstream_content_type or "").strip()
    base_content_type = raw_content_type.split(";", 1)[0].strip().lower()
    if base_content_type.startswith("audio/"):
        canonical = _AUDIO_MIME_ALIASES.get(base_content_type, base_content_type)
        return canonical + raw_content_type[len(base_content_type):]
    if base_content_type in {"application/ogg", "application/octet-stream"}:
        if base_content_type == "application/ogg":
            return raw_content_type

    path = urlparse(upstream_url).path.lower().rstrip("/")
    extension = path.rsplit(".", 1)[-1] if "." in path else ""
    return _AUDIO_MIME_BY_EXTENSION.get(extension, "application/octet-stream")


def _stream_error_summary(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"上游返回 HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "上游请求超时"
    return f"上游请求失败 ({type(error).__name__})"


logger = logging.getLogger("waveflow")


# NOTE: 旧 ``rewrite_m3u8_text`` 已被 ``core.m3u8_rewriter.rewrite_m3u8`` 取代，
# 通过 signed handle 屏蔽上游 URL，并把 access_token 透传到子 playlist/分片。
# 旧的电台 raw-url 公共入口已删除，
# 客户端统一使用 ``/api/media/channel/{station_id}/playlist.m3u8``。


# 全局复用的异步 HTTP 客户端，维持与上游 CDN 的 Keep-Alive 长连接
http_client = StatelessAsyncClient(
    timeout=HTTP_TIMEOUT,
    verify=CDN_VERIFY_SSL,
    limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)
)

RTSP_HLS_ROOT = Path(os.getenv("RTSP_HLS_ROOT") or (Path(tempfile.gettempdir()) / "waveflow_rtsp_hls"))
RTSP_HLS_SESSIONS: dict[str, dict] = {}
_RTSP_HLS_STARTUPS: dict[str, asyncio.Task] = {}
_RTSP_HLS_QUOTA_LOCK = asyncio.Lock()
_RTSP_RESERVED_SESSIONS: set[str] = set()
RTSP_HLS_IDLE_TTL = 90
RTSP_HLS_START_TIMEOUT = 18
RTSP_HLS_SEGMENT_SECONDS = _env_int("RTSP_HLS_SEGMENT_SECONDS", 2)
RTSP_HLS_LIST_SIZE = _env_int("RTSP_HLS_LIST_SIZE", 15, minimum=3)
RTSP_HLS_START_SEGMENTS = _env_int("RTSP_HLS_START_SEGMENTS", 2, minimum=1)
RTSP_HLS_DELETE_THRESHOLD = _env_int("RTSP_HLS_DELETE_THRESHOLD", 4, minimum=1)
RTSP_HLS_CLEANUP_INTERVAL_SECONDS = 10
RTSP_HLS_STALL_MIN_SECONDS = 15.0
RTSP_HLS_STALL_MULTIPLIER = 4.0
RTSP_SEGMENT_RE = re.compile(r"^seg_\d+\.ts$")
RTSP_SESSION_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_RTSP_HLS_CLEANUP_TASK: asyncio.Task | None = None
_RTSP_HLS_CLEANUP_STOP: asyncio.Event | None = None
_RTSP_HLS_SHUTTING_DOWN = False
_APP_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _observe_background_task(task: asyncio.Task, owner: str) -> None:
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        logger.error("后台任务异常退出: owner=%s error_type=%s", owner, type(error).__name__)


def _track_app_background_task(task: asyncio.Task, *, owner: str) -> asyncio.Task:
    _APP_BACKGROUND_TASKS.add(task)

    def done(completed: asyncio.Task) -> None:
        _APP_BACKGROUND_TASKS.discard(completed)
        _observe_background_task(completed, owner)

    task.add_done_callback(done)
    return task


def _ffmpeg_bin() -> str | None:
    ffmpeg = media_tool_bin("ffmpeg")
    if ffmpeg:
        logger.info("ffmpeg found at: %s", ffmpeg)
    else:
        logger.warning("ffmpeg NOT FOUND")
    return ffmpeg


def _rtsp_session_id(
    target_url: str,
    custom_ua: str = "",
    compat: bool = False,
    *,
    playback_options: RtspPlaybackOptions | None = None,
) -> str:
    options = playback_options or resolve_rtsp_playback_options(compat=compat)
    material = (
        f"{target_url}\n{custom_ua}\n"
        f"{options.video_mode.value}\n{options.timestamp_mode.value}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _validate_rtsp_url(target_url: str) -> None:
    parsed = urlparse(target_url)
    if parsed.scheme.lower() != "rtsp":
        raise HTTPException(status_code=400, detail="target_url 只允许 rtsp 地址。")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="无效的 rtsp 地址。")


def _validate_rtsp_proxy_request(target_url: str) -> None:
    """在任何 RTSP→HLS session 创建前执行统一的有效策略检查。

    这是 RTSP proxy 的最终安全边界，而不是某个具体 HTTP route 的入口检查。
    这样 smart IPTV、signed handle 以及未来的 RTSP 入口都不能绕过有效开关和
    现有 host/IP policy。
    """
    if not config_rtsp_proxy_enabled():
        raise HTTPException(status_code=503, detail="RTSP 代理已禁用")

    _validate_rtsp_url(target_url)
    parsed = urlparse(target_url)
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="rtsp 地址缺少 host。")
    try:
        assert_safe_host_ips(parsed.hostname)
    except UnsafeTargetError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _rtsp_proc_returncode(proc) -> int | None:
    if proc is None:
        return None
    poll = getattr(proc, "poll", None)
    if callable(poll):
        return poll()
    return getattr(proc, "returncode", None)


def _delete_rtsp_session_dir(session_id: str, session: dict | None = None) -> None:
    if not RTSP_SESSION_ID_RE.fullmatch(session_id):
        return
    raw_dir = session.get("dir") if session else None
    session_dir = Path(raw_dir) if raw_dir else RTSP_HLS_ROOT / session_id
    try:
        root = RTSP_HLS_ROOT.resolve()
        target = session_dir.resolve()
        if target.parent == root and target.name == session_id:
            shutil.rmtree(target, ignore_errors=True)
    except Exception:
        logger.warning("清理 RTSP 临时目录失败: %s", session_dir, exc_info=True)


def _clear_stale_rtsp_hls_dirs() -> None:
    try:
        RTSP_HLS_ROOT.mkdir(parents=True, exist_ok=True)
        for child in RTSP_HLS_ROOT.iterdir():
            if child.is_dir() and RTSP_SESSION_ID_RE.fullmatch(child.name):
                shutil.rmtree(child, ignore_errors=True)
    except Exception:
        logger.warning("清理旧 RTSP 临时目录失败: %s", RTSP_HLS_ROOT, exc_info=True)


def _rtsp_hls_segment_name(uri: str) -> str | None:
    """Return a generated local segment name from one playlist URI."""
    parsed = urlparse(uri.strip())
    name = Path(parsed.path).name
    if not RTSP_SEGMENT_RE.fullmatch(name):
        return None
    return name


def _rtsp_hls_progress_snapshot(session: dict) -> dict | None:
    """Read a small, completed-output snapshot from one RTSP HLS session.

    The ffmpeg HLS muxer updates ``index.m3u8`` in place.  A read that ends in
    an incomplete EXTINF/URI pair is therefore treated as inconclusive rather
    than as a progress event or an immediate stall.
    """
    raw_dir = session.get("dir")
    if not raw_dir:
        return None
    session_dir = Path(raw_dir)
    playlist_path = session_dir / "index.m3u8"
    try:
        raw = playlist_path.read_bytes()
        if not raw or len(raw) > 256 * 1024:
            return None
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    lines = text.splitlines()
    if not lines or lines[0].strip() != "#EXTM3U":
        return None

    target_duration: float | None = None
    media_sequence: int | None = None
    pending_duration: float | None = None
    declared_segments: list[tuple[float, str]] = []
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                value = float(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None
            if not math.isfinite(value) or value <= 0:
                return None
            target_duration = value
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None
            continue
        if line.startswith("#EXTINF:"):
            try:
                value = float(line.split(":", 1)[1].split(",", 1)[0].strip())
            except (ValueError, IndexError):
                return None
            if not math.isfinite(value) or value < 0:
                return None
            pending_duration = value
            continue
        if line.startswith("#"):
            continue
        if pending_duration is None:
            continue
        segment_name = _rtsp_hls_segment_name(line)
        if segment_name is None:
            return None
        declared_segments.append((pending_duration, segment_name))
        pending_duration = None

    # An unfinished EXTINF at EOF is a normal transient state while ffmpeg is
    # rewriting the playlist.  Do not let it reset the progress clock.
    if pending_duration is not None or not declared_segments:
        return None

    latest_duration, latest_segment = declared_segments[-1]
    try:
        stat = (session_dir / latest_segment).stat()
    except OSError:
        # The playlist can briefly name a segment before the file is visible.
        return None

    return {
        "fingerprint": (
            target_duration,
            media_sequence,
            tuple(declared_segments),
        ),
        "last_segment_name": latest_segment,
        "last_segment_mtime_ns": stat.st_mtime_ns,
        "last_segment_size": stat.st_size,
        "observed_target_duration": target_duration,
        "last_extinf": latest_duration,
    }


def _rtsp_hls_store_progress(session: dict, snapshot: dict, now: float | None = None) -> None:
    session["last_playlist_fingerprint"] = snapshot["fingerprint"]
    session["last_segment_name"] = snapshot["last_segment_name"]
    session["last_segment_mtime_ns"] = snapshot["last_segment_mtime_ns"]
    session["last_segment_size"] = snapshot["last_segment_size"]
    session["observed_target_duration"] = snapshot["observed_target_duration"]
    session["last_extinf"] = snapshot["last_extinf"]
    session["last_progress_at"] = time.monotonic() if now is None else now


def _initialize_rtsp_hls_progress(session: dict, now: float | None = None) -> None:
    """Create a READY baseline without making startup depend on parsing it."""
    snapshot = _rtsp_hls_progress_snapshot(session)
    if snapshot is None:
        session["last_playlist_fingerprint"] = None
        session["last_segment_name"] = None
        session["last_segment_mtime_ns"] = None
        session["last_segment_size"] = None
        session["observed_target_duration"] = None
        session["last_extinf"] = None
        session["progress_read_failures"] = 0
        session["last_progress_at"] = time.monotonic() if now is None else now
        return
    _rtsp_hls_store_progress(session, snapshot, now=now)
    session["progress_read_failures"] = 0


def _refresh_rtsp_hls_progress(session: dict, now: float | None = None) -> bool:
    """Record progress if the playlist or newest completed segment changed."""
    snapshot = _rtsp_hls_progress_snapshot(session)
    if snapshot is None:
        try:
            failures = int(session.get("progress_read_failures", 0))
        except (TypeError, ValueError):
            failures = 0
        session["progress_read_failures"] = failures + 1
        return False
    session["progress_read_failures"] = 0

    previous_fingerprint = session.get("last_playlist_fingerprint")
    changed = previous_fingerprint is None or snapshot["fingerprint"] != previous_fingerprint
    if not changed:
        changed = (
            snapshot["last_segment_name"] != session.get("last_segment_name")
            or snapshot["last_segment_mtime_ns"] != session.get("last_segment_mtime_ns")
            or snapshot["last_segment_size"] != session.get("last_segment_size")
        )
    if not changed:
        return False

    _rtsp_hls_store_progress(session, snapshot, now=now)
    return True


def _rtsp_hls_stall_timeout(session: dict) -> float:
    timeout = RTSP_HLS_STALL_MIN_SECONDS
    for key in ("observed_target_duration", "last_extinf"):
        try:
            duration = float(session.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            timeout = max(timeout, duration * RTSP_HLS_STALL_MULTIPLIER)
    return timeout


async def _stop_rtsp_process(proc, stderr_thread: threading.Thread | None = None) -> None:
    """Stop one ffmpeg process and drain its stderr worker before returning."""
    if proc and _rtsp_proc_returncode(proc) is None:
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=5)
        except (asyncio.TimeoutError, subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=5)
            except (asyncio.TimeoutError, subprocess.TimeoutExpired, OSError):
                logger.warning("等待 RTSP ffmpeg 退出超时")

    if (
        stderr_thread is not None
        and stderr_thread.is_alive()
        and stderr_thread is not threading.current_thread()
    ):
        pipe = getattr(proc, "stderr", None)
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass
        await asyncio.to_thread(stderr_thread.join, 1.0)


async def _stop_rtsp_session_owned(session_id: str, session: dict) -> None:
    try:
        await _stop_rtsp_process(session.get("process"), session.get("stderr_thread"))
    finally:
        if RTSP_HLS_SESSIONS.get(session_id) is session:
            RTSP_HLS_SESSIONS.pop(session_id, None)
            _delete_rtsp_session_dir(session_id, session)
        if session.get("stop_task") is asyncio.current_task():
            session.pop("stop_task", None)


async def _stop_rtsp_session(session_id: str, expected_session: dict | None = None) -> None:
    """Stop a session once, without allowing an old cleanup to touch a retry."""
    session = RTSP_HLS_SESSIONS.get(session_id)
    if expected_session is not None and session is not expected_session:
        return
    if session is None:
        # A retry may already own the same deterministic directory. Do not let
        # a late cleanup from an older task delete it.
        if expected_session is not None:
            return
        if session_id in _RTSP_HLS_STARTUPS or session_id in _RTSP_RESERVED_SESSIONS:
            return
        _delete_rtsp_session_dir(session_id)
        return

    current_task = asyncio.current_task()
    stop_task = session.get("stop_task")
    session["startup_state"] = "stopping"
    if stop_task is None:
        stop_task = asyncio.create_task(
            _stop_rtsp_session_owned(session_id, session),
            name=f"rtsp-session-stop:{session_id}",
        )
        session["stop_task"] = stop_task
    if stop_task is current_task:
        return
    try:
        await asyncio.shield(stop_task)
    except asyncio.CancelledError:
        # The caller may be the startup task that is itself being canceled.
        # Let the independent cleanup task finish before propagating that
        # cancellation, so a second shutdown/stop caller cannot strand ffmpeg.
        while not stop_task.done():
            try:
                await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                continue
        stop_task.result()
        raise


async def _rtsp_hls_cleanup_task(stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RTSP_HLS_CLEANUP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        await _rtsp_hls_cleanup_once()


async def _rtsp_hls_cleanup_once() -> None:
    """Run one idle/dead/stalled RTSP session sweep."""
    now = time.time()
    monotonic_now = time.monotonic()
    for session_id, session in list(RTSP_HLS_SESSIONS.items()):
        startup_state = session.get("startup_state")
        if startup_state in {"starting", "stopping"}:
            # A freshly-created process is governed by the startup owner, and
            # an independently-running stop task already owns teardown.
            continue

        proc = session.get("process")
        idle = now - float(session.get("last_access", 0))
        if proc and _rtsp_proc_returncode(proc) is not None:
            try:
                await _stop_rtsp_session(session_id, expected_session=session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("RTSP session cleanup failed: session=%s", session_id)
            continue
        if idle > RTSP_HLS_IDLE_TTL:
            try:
                await _stop_rtsp_session(session_id, expected_session=session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("RTSP session cleanup failed: session=%s", session_id)
            continue

        if startup_state != "ready":
            continue

        # Sessions created by older code/tests may not have progress fields.
        # Establish a grace-period baseline instead of treating missing state
        # as a stalled stream.
        if "last_progress_at" not in session:
            _initialize_rtsp_hls_progress(session, now=monotonic_now)
            continue

        if _refresh_rtsp_hls_progress(session, now=monotonic_now):
            continue

        # One incomplete/read-error snapshot is not enough evidence to kill a
        # session.  Give the in-place playlist update another sweep while
        # retaining the old progress timestamp.
        try:
            progress_read_failures = int(session.get("progress_read_failures", 0))
        except (TypeError, ValueError):
            progress_read_failures = 0
        if progress_read_failures == 1:
            continue

        last_progress_at = session.get("last_progress_at")
        try:
            progress_age = monotonic_now - float(last_progress_at)
        except (TypeError, ValueError):
            _initialize_rtsp_hls_progress(session, now=monotonic_now)
            continue
        stall_timeout = _rtsp_hls_stall_timeout(session)
        if progress_age < stall_timeout:
            continue

        # Yield once and take a final identity/progress snapshot.  A segment
        # may have become complete just after the first read; do not kill a
        # healthy session merely because the sweep caught that boundary.
        await asyncio.sleep(0)
        if RTSP_HLS_SESSIONS.get(session_id) is not session:
            continue
        if _refresh_rtsp_hls_progress(session):
            continue
        try:
            progress_age = time.monotonic() - float(session.get("last_progress_at"))
        except (TypeError, ValueError):
            continue
        stall_timeout = _rtsp_hls_stall_timeout(session)
        if progress_age < stall_timeout:
            continue

        logger.warning(
            "RTSP session stalled: session=%s progress_age=%.1fs stall_timeout=%.1fs "
            "target_duration=%s last_segment=%s",
            session_id,
            progress_age,
            stall_timeout,
            session.get("observed_target_duration"),
            session.get("last_segment_name"),
        )
        try:
            await _stop_rtsp_session(session_id, expected_session=session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("RTSP stalled session cleanup failed: session=%s", session_id)


def _start_rtsp_hls_cleanup() -> asyncio.Task:
    global _RTSP_HLS_CLEANUP_TASK, _RTSP_HLS_CLEANUP_STOP
    if _RTSP_HLS_CLEANUP_TASK is not None and not _RTSP_HLS_CLEANUP_TASK.done():
        return _RTSP_HLS_CLEANUP_TASK
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _rtsp_hls_cleanup_task(stop_event),
        name="rtsp-hls-cleanup",
    )
    _RTSP_HLS_CLEANUP_STOP = stop_event
    _RTSP_HLS_CLEANUP_TASK = task
    task.add_done_callback(lambda done: _observe_background_task(done, "rtsp_hls_cleanup"))
    return task


async def _stop_rtsp_hls_cleanup() -> None:
    global _RTSP_HLS_CLEANUP_TASK, _RTSP_HLS_CLEANUP_STOP
    task = _RTSP_HLS_CLEANUP_TASK
    stop_event = _RTSP_HLS_CLEANUP_STOP
    if stop_event is not None:
        stop_event.set()
    _RTSP_HLS_CLEANUP_TASK = None
    _RTSP_HLS_CLEANUP_STOP = None
    if task is not None and task is not asyncio.current_task():
        await asyncio.gather(task, return_exceptions=True)


async def _stop_all_rtsp_sessions(*, shutdown: bool = False):
    """Force-drain shared startups before stopping any remaining sessions."""
    global _RTSP_HLS_SHUTTING_DOWN
    if shutdown:
        # Set the gate and take the snapshot under the same quota lock used by
        # startup creation. This closes the window where a request had passed
        # the first gate check but was waiting to reserve its slot.
        async with _RTSP_HLS_QUOTA_LOCK:
            _RTSP_HLS_SHUTTING_DOWN = True
            startup_items = list(_RTSP_HLS_STARTUPS.items())
    else:
        startup_items = list(_RTSP_HLS_STARTUPS.items())
    for _session_id, task in startup_items:
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
    if startup_items:
        await asyncio.gather(
            *(task for _session_id, task in startup_items),
            return_exceptions=True,
        )

    # Done callbacks normally remove these entries. Keep the identity check so
    # a late callback from startup A cannot clear a newer startup B.
    for session_id, task in startup_items:
        if _RTSP_HLS_STARTUPS.get(session_id) is task:
            _RTSP_HLS_STARTUPS.pop(session_id, None)
            _RTSP_RESERVED_SESSIONS.discard(session_id)

    for session_id in list(RTSP_HLS_SESSIONS):
        await _stop_rtsp_session(session_id)

    # At this point no shared startup or registered session remains. Any
    # reservation left here belongs to a task that was canceled before its
    # coroutine body started; shutdown owns the registry and may clear it.
    if not _RTSP_HLS_STARTUPS and not RTSP_HLS_SESSIONS:
        _RTSP_RESERVED_SESSIONS.clear()


def _drain_rtsp_stderr(session_id: str, pipe) -> None:
    if not pipe:
        return
    tail = ""
    try:
        while True:
            chunk = pipe.read(1024)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="ignore")
            tail = (tail + text)[-1200:]
            session = RTSP_HLS_SESSIONS.get(session_id)
            if session is not None:
                session["stderr_tail"] = tail
    except Exception:
        return
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _finish_rtsp_startup(session_id: str, task: asyncio.Task) -> None:
    current = _RTSP_HLS_STARTUPS.get(session_id)
    if current is task:
        _RTSP_HLS_STARTUPS.pop(session_id, None)
        _RTSP_RESERVED_SESSIONS.discard(session_id)
    _observe_background_task(task, f"rtsp_hls_startup:{session_id}")


def _build_rtsp_ffmpeg_command(
    *,
    ffmpeg: str,
    target_url: str,
    custom_ua: str,
    session_id: str,
    session_dir: Path,
    playback_options: RtspPlaybackOptions,
) -> list[str]:
    headers = ["-user_agent", custom_ua] if custom_ua else []
    hls_flags = "delete_segments+append_list+omit_endlist"
    if playback_options.video_mode is RtspVideoMode.TRANSCODE:
        hls_flags += "+independent_segments"
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-fflags", "+genpts",
        "-rtsp_transport", "tcp",
        *headers,
        "-i", target_url,
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        *rtsp_video_args(playback_options),
        "-c:a", "aac",
        "-b:a", "128k",
        "-f", "hls",
        "-hls_time", str(RTSP_HLS_SEGMENT_SECONDS),
        "-hls_list_size", str(RTSP_HLS_LIST_SIZE),
        "-hls_delete_threshold", str(RTSP_HLS_DELETE_THRESHOLD),
        "-hls_flags", hls_flags,
        "-hls_segment_filename", str(session_dir / "seg_%05d.ts"),
        "-hls_base_url", f"/api/media/proxy/rtsp-segments/{session_id}/",
        str(session_dir / "index.m3u8"),
    ]


async def _start_rtsp_hls_session(
    target_url: str,
    custom_ua: str = "",
    compat: bool = False,
    *,
    playback_options: RtspPlaybackOptions | None = None,
) -> tuple[str, Path]:
    _validate_rtsp_url(target_url)
    options = playback_options or resolve_rtsp_playback_options(compat=compat)
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        raise HTTPException(status_code=503, detail="未找到 ffmpeg，请安装 ffmpeg 或设置 FFMPEG_BIN")

    RTSP_HLS_ROOT.mkdir(parents=True, exist_ok=True)
    session_id = _rtsp_session_id(
        target_url,
        custom_ua,
        compat,
        playback_options=options,
    )
    session_dir = RTSP_HLS_ROOT / session_id
    playlist_path = session_dir / "index.m3u8"
    existing_session = RTSP_HLS_SESSIONS.get(session_id)
    proc = existing_session.get("process") if existing_session else None
    if (
        proc
        and _rtsp_proc_returncode(proc) is None
        and playlist_path.exists()
        and existing_session.get("startup_state") == "ready"
    ):
        existing_session["last_access"] = time.time()
        return session_id, playlist_path

    if existing_session:
        await _stop_rtsp_session(session_id, expected_session=existing_session)

    _delete_rtsp_session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    cmd = _build_rtsp_ffmpeg_command(
        ffmpeg=ffmpeg,
        target_url=target_url,
        custom_ua=custom_ua,
        session_id=session_id,
        session_dir=session_dir,
        playback_options=options,
    )

    subprocess_kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    session: dict | None = None
    stderr_thread: threading.Thread | None = None
    try:
        proc = subprocess.Popen(cmd, **subprocess_kwargs)
    except FileNotFoundError as exc:
        _delete_rtsp_session_dir(session_id)
        raise HTTPException(status_code=503, detail=f"ffmpeg 不存在或不可执行: {ffmpeg}") from exc
    except Exception as exc:
        _delete_rtsp_session_dir(session_id)
        logger.exception("启动 ffmpeg 失败")
        raise HTTPException(status_code=500, detail=f"启动 ffmpeg 失败: {exc}") from exc

    session = {
        "process": proc,
        "dir": session_dir,
        "last_access": time.time(),
        "target_url": target_url,
        "compat": options.compat,
        "video_mode": options.video_mode.value,
        "timestamp_mode": options.timestamp_mode.value,
        "stderr_tail": "",
        "startup_state": "starting",
    }
    RTSP_HLS_SESSIONS[session_id] = session
    stderr_thread = threading.Thread(
        target=_drain_rtsp_stderr,
        args=(session_id, proc.stderr),
        daemon=True,
        name=f"rtsp-stderr:{session_id}",
    )
    session["stderr_thread"] = stderr_thread
    try:
        stderr_thread.start()
    except BaseException:
        await _stop_rtsp_session(session_id, expected_session=session)
        raise

    try:
        deadline = time.monotonic() + RTSP_HLS_START_TIMEOUT
        while time.monotonic() < deadline:
            if playlist_path.exists() and len(list(session_dir.glob("seg_*.ts"))) >= RTSP_HLS_START_SEGMENTS:
                if RTSP_HLS_SESSIONS.get(session_id) is not session:
                    raise RuntimeError("RTSP startup session ownership lost")
                session["last_access"] = time.time()
                _initialize_rtsp_hls_progress(session)
                session["startup_state"] = "ready"
                return session_id, playlist_path
            if _rtsp_proc_returncode(proc) is not None:
                stderr = session.get("stderr_tail", "")
                raise HTTPException(status_code=502, detail=f"ffmpeg RTSP 转 HLS 失败: {stderr or '进程退出'}")
            await asyncio.sleep(0.25)

        stderr = session.get("stderr_tail", "")
        raise HTTPException(status_code=504, detail=f"RTSP 转 HLS 起播超时{': ' + stderr[-500:] if stderr else ''}")
    except asyncio.CancelledError:
        await _stop_rtsp_session(session_id, expected_session=session)
        raise
    except BaseException:
        await _stop_rtsp_session(session_id, expected_session=session)
        raise


async def _ensure_rtsp_hls_session(
    target_url: str,
    custom_ua: str = "",
    compat: bool = False,
    *,
    playback_options: RtspPlaybackOptions | None = None,
) -> tuple[str, Path]:
    """Return one shared RTSP→HLS startup for each session identity.

    The check and task insertion happen before the first await, so two
    callers on the same event loop cannot both become startup owners. A
    shielded wait ensures cancellation of one HTTP waiter does not cancel the
    shared startup task.
    """
    _validate_rtsp_url(target_url)
    if _RTSP_HLS_SHUTTING_DOWN:
        raise HTTPException(status_code=503, detail="RTSP 服务正在关闭")
    options = playback_options or resolve_rtsp_playback_options(compat=compat)
    session_id = _rtsp_session_id(
        target_url,
        custom_ua,
        compat,
        playback_options=options,
    )
    session_dir = RTSP_HLS_ROOT / session_id
    playlist_path = session_dir / "index.m3u8"

    startup = _RTSP_HLS_STARTUPS.get(session_id)
    if startup is not None:
        if not startup.done():
            return await asyncio.shield(startup)

    session = RTSP_HLS_SESSIONS.get(session_id)
    proc = session.get("process") if session else None
    if (
        proc
        and _rtsp_proc_returncode(proc) is None
        and playlist_path.exists()
        and session.get("startup_state") == "ready"
    ):
        session["last_access"] = time.time()
        return session_id, playlist_path

    startup_to_await: asyncio.Task | None = None
    async with _RTSP_HLS_QUOTA_LOCK:
        if _RTSP_HLS_SHUTTING_DOWN:
            raise HTTPException(status_code=503, detail="RTSP 服务正在关闭")
        startup = _RTSP_HLS_STARTUPS.get(session_id)
        if startup is not None and startup.done():
            if _RTSP_HLS_STARTUPS.get(session_id) is startup:
                _RTSP_HLS_STARTUPS.pop(session_id, None)
            _RTSP_RESERVED_SESSIONS.discard(session_id)
            startup = None

        if startup is not None:
            # The quota is checked only by a genuine startup owner. Same-key
            # waiters join the already reserved task and consume no slot.
            startup_to_await = startup
        else:
            session = RTSP_HLS_SESSIONS.get(session_id)
            proc = session.get("process") if session else None
            if (
                proc
                and _rtsp_proc_returncode(proc) is None
                and playlist_path.exists()
                and session.get("startup_state") == "ready"
            ):
                session["last_access"] = time.time()
                return session_id, playlist_path

            settings = get_effective_settings_sync()
            active_ids = set(RTSP_HLS_SESSIONS) | _RTSP_RESERVED_SESSIONS
            if session_id not in active_ids and len(active_ids) >= settings.rtsp_max_sessions:
                raise HTTPException(status_code=503, detail="RTSP session capacity reached")

            _RTSP_RESERVED_SESSIONS.add(session_id)
            try:
                start_coro = (
                    _start_rtsp_hls_session(
                        target_url,
                        custom_ua,
                        compat,
                        playback_options=options,
                    )
                    if playback_options is not None
                    else _start_rtsp_hls_session(target_url, custom_ua, compat)
                )
                startup_to_await = asyncio.create_task(
                    start_coro,
                    name=f"rtsp-hls-startup:{session_id}",
                )
            except BaseException:
                _RTSP_RESERVED_SESSIONS.discard(session_id)
                raise
            _RTSP_HLS_STARTUPS[session_id] = startup_to_await
            startup_to_await.add_done_callback(
                lambda done: _finish_rtsp_startup(session_id, done)
            )

    return await asyncio.shield(startup_to_await)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _RTSP_HLS_SHUTTING_DOWN
    _RTSP_HLS_SHUTTING_DOWN = False
    automation_service = None
    plugin_subsystem = None
    app.state.automation_service = None
    app.state.plugin_subsystem = None
    app.state.provider_resolver = None
    app.state.radio_resolver = RadioResolver(runtime=None)
    try:
        await database.initialize()
        interrupted_subscriptions = await database.recover_interrupted_subscription_refreshes()
        if interrupted_subscriptions:
            logger.warning(
                "Recovered %d interrupted subscription refresh(es)",
                interrupted_subscriptions,
            )
        # Ensure production channel reads have a durable logical projection
        # before any API/media request can observe the database.
        await _run_channel_binding_maintenance('startup')
        try:
            app.state.provider_resolver = ProviderResolver.from_ownership_rows(
                await database.list_plugin_scheme_ownership(), runtime=None,
                legacy_resolver=resolve_adapter_source,
            )
        except Exception:
            # If the durable ownership read itself fails, do not guess that
            # every scheme is legacy.  A missing resolver is now an explicit
            # fail-closed state in media/probe callers.
            logger.exception("Unable to load durable Plugin ownership before startup")
            app.state.provider_resolver = None
        try:
            plugin_subsystem = await ProductionPluginSubsystem.create(
                root=default_plugin_root(), http_client=http_client,
            )
            app.state.plugin_subsystem = plugin_subsystem
            # Content Package updates that run through Market automation never see
            # this request, so the Content dependency contract is enforced inside
            # ``market`` through a validator wired to the live Plugin subsystem.
            plugin_service = getattr(plugin_subsystem, "service", None)
            if plugin_service is not None:
                _market.set_content_dependency_validator(plugin_service.dependency_projection)
            app.state.provider_resolver = plugin_subsystem.provider_resolver
            app.state.radio_resolver = getattr(
                plugin_subsystem, "radio_resolver", RadioResolver(runtime=None),
            )
            recovery = await plugin_subsystem.startup()
            unavailable = [item for item in recovery if item.get("status") != "active"]
            if unavailable:
                logger.warning("Plugin startup recovery completed with %d unavailable provider(s)", len(unavailable))
        except Exception:
            logger.exception("Plugin subsystem startup failed; Plugin ownership will fail closed")
            if plugin_subsystem is not None:
                try:
                    await plugin_subsystem.shutdown()
                except Exception:
                    logger.exception("Plugin subsystem cleanup after startup failure failed")
            plugin_subsystem = None
            app.state.plugin_subsystem = None
            _market.set_content_dependency_validator(None)
            app.state.radio_resolver = RadioResolver(runtime=None)
            try:
                app.state.provider_resolver = ProviderResolver.from_ownership_rows(
                    await database.list_plugin_scheme_ownership(), runtime=None,
                    legacy_resolver=resolve_adapter_source,
                )
            except Exception:
                logger.exception("Unable to reconcile Plugin ownership after startup failure")
                app.state.provider_resolver = None
        automation_service = await create_production_automation_service(http_client)
        if plugin_subsystem is not None:
            plugin_subsystem.automation_service = automation_service
            if await database.list_plugin_installations():
                register_plugin_update_task(automation_service.registry, plugin_subsystem)
            if (
                getattr(plugin_subsystem, "service", None) is not None
                and hasattr(automation_service, "registry")
                and hasattr(automation_service, "repository")
            ):
                await reconcile_radio_automation_tasks(
                    automation_service, plugin_subsystem,
                    retained_owner_identities={
                        f"{row['publisher_id']}/{row['plugin_id']}"
                        for row in await database.list_plugin_installations()
                    },
                )
        app.state.automation_service = automation_service
        await automation_service.start()
        _clear_stale_rtsp_hls_dirs()
        _start_rtsp_hls_cleanup()
        # logo 模板：本地兜底已在 import 时加载完成，这里启动后异步拉一次远程覆盖；
        # 拉失败就维持本地，按用户要求不重试。后续接入设置页后再做定时刷新 / 手动刷新。
        _track_app_background_task(
            asyncio.create_task(refresh_logo_template_from_remote(http_client), name="logo-template-refresh"),
            owner="logo_template_refresh",
        )
        yield
    finally:
        # RTSP startup tasks are shared session work, not request-owned
        # background jobs. Stop the periodic cleaner first, then cancel and
        # drain startup tasks before tearing down the remaining sessions.
        await _stop_rtsp_hls_cleanup()
        await _stop_all_rtsp_sessions(shutdown=True)
        await _shutdown_app_background_tasks()
        try:
            if automation_service is not None:
                await automation_service.stop()
        finally:
            app.state.automation_service = None
            try:
                if plugin_subsystem is not None:
                    await plugin_subsystem.shutdown()
            finally:
                app.state.plugin_subsystem = None
                app.state.provider_resolver = None
                app.state.radio_resolver = None
                await http_client.aclose()

app = FastAPI(
    title="WaveFlow",
    description="用于聚合电台 m3u8 与 ts 切片代理的后端服务。",
    version="0.1.0",
    lifespan=lifespan, 
    docs_url=None if get_settings().production else "/docs",
    redoc_url=None if get_settings().production else "/redoc",
    openapi_url=None if get_settings().production else "/openapi.json",
)
#跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().allowed_origins),
    allow_credentials=get_settings().mode == "desktop",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(setup_router)
app.include_router(auth_router)
app.include_router(media_credentials_router)
app.include_router(media_proxy_router)
app.include_router(radio_router)
app.include_router(settings_router)
app.include_router(plugins_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


def validate_target_url(target_url: str) -> None:
    """校验代理目标 URL 是否只使用 HTTP/HTTPS 协议"""

    parsed_target_url = urlparse(target_url)

    if parsed_target_url.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="target_url 只允许 http 或 https 地址。")


async def fetch_real_m3u8_text(real_m3u8_url: str, station_id: str) -> httpx.Response:
    """请求真实 CDN m3u8，并返回原始响应对象"""
    
    headers = get_cdn_headers_for_station(station_id)
    
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        verify=CDN_VERIFY_SSL,
    ) as client:
        return await request_with_safe_redirects(
            client,
            "GET",
            real_m3u8_url,
            headers=headers,
        )


@app.get("/api/config")
async def get_config() -> dict:
    """返回前端运行时配置；内容可见性不依赖访问者地区。"""
    settings = await get_effective_settings()
    return {
        "mode": settings.mode,
        "anonymousBrowse": settings.anonymous_browse,
        "anonymousPlayback": settings.anonymous_playback,
        "production": settings.production,
    }


# 旧 ``/api/{station_id}/playlist.m3u8`` / ``/api/{station_id}/{m3u8_name}.m3u8`` /
# ``/api/{station_id}/chunk.ts`` / ``/api/proxy/stream`` 公共入口已被
# ``/api/media/channel/{key}/playlist.m3u8`` + signed-handle 子路由统一取代，
# 不再注册以避免裸 ``target_url`` / ``url`` 形式的 SSRF 入口残留。


# =====================================================================
# IPTV 订阅管理
# =====================================================================

from m3u8_parser import parse_m3u_document, deduplicate_channels
from logo_template import refresh_logo_template_from_remote
import database as db
import market as _market


SUBSCRIPTION_MAX_RESPONSE_BYTES = 50 * 1024 * 1024
SUBSCRIPTION_CONNECT_TIMEOUT_SECONDS = 10.0
SUBSCRIPTION_READ_TIMEOUT_SECONDS = 15.0
SUBSCRIPTION_TOTAL_TIMEOUT_SECONDS = 60.0
SUBSCRIPTION_REFRESH_CONCURRENCY = 4


class SubscriptionFetchError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


class SubscriptionRefreshHttpError(HTTPException):
    def __init__(self, *, status_code: int, code: str, detail: str):
        self.code = code
        super().__init__(status_code=status_code, detail=detail)


def _subscription_fetch_error(error: Exception) -> SubscriptionFetchError:
    if isinstance(error, (RedirectTargetRejected, UnsafeTargetError)):
        return SubscriptionFetchError(
            "unsafe_target",
            "订阅地址不符合当前网络访问策略",
        )
    if isinstance(error, ResponseTooLargeError):
        return SubscriptionFetchError(
            "response_too_large",
            "订阅源内容超过允许大小",
        )
    if isinstance(error, httpx.TimeoutException):
        return SubscriptionFetchError("timeout", "订阅源请求超时")
    if isinstance(error, httpx.HTTPStatusError):
        return SubscriptionFetchError(
            "http_error",
            f"订阅源返回 HTTP {error.response.status_code}",
        )
    if isinstance(error, httpx.TooManyRedirects):
        return SubscriptionFetchError("redirect_failed", "订阅源重定向次数过多")
    if isinstance(error, httpx.ConnectError):
        return SubscriptionFetchError("connection_failed", "无法连接订阅源")
    return SubscriptionFetchError("fetch_failed", "拉取订阅源失败")


async def _fetch_subscription_document(url: str, *, custom_ua: str = ""):
    try:
        headers = {"User-Agent": custom_ua} if custom_ua else {}
        _final_url, body, response_headers = await asyncio.wait_for(
            fetch_bytes(
                url,
                headers=headers,
                policy=default_policy(
                    connect_timeout=SUBSCRIPTION_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=SUBSCRIPTION_READ_TIMEOUT_SECONDS,
                    max_response_bytes=SUBSCRIPTION_MAX_RESPONSE_BYTES,
                    verify_tls=True,
                ),
            ),
            timeout=SUBSCRIPTION_TOTAL_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as error:
        raise SubscriptionFetchError("timeout", "订阅源请求超时") from error
    except (httpx.HTTPError, UnsafeTargetError) as error:
        raise _subscription_fetch_error(error) from error

    charset = "utf-8"
    content_type = str(response_headers.get("content-type") or "")
    charset_match = re.search(r"charset\s*=\s*['\"]?([^;\s'\"]+)", content_type, re.I)
    if charset_match:
        charset = charset_match.group(1)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    try:
        return parse_m3u_document(text)
    except Exception as error:
        raise SubscriptionFetchError(
            "parse_failed",
            "订阅源内容解析失败",
        ) from error


async def _record_subscription_refresh_failure(
    subscription_id: int,
    generation: int,
    *,
    status: str,
    error: str,
) -> None:
    try:
        await db.mark_subscription_invalid_if_current(
            subscription_id,
            generation,
            status=status,
            error=error,
        )
    except asyncio.CancelledError:
        raise
    except Exception as state_error:
        logger.warning(
            "Subscription refresh failure state write failed: %s",
            type(state_error).__name__,
        )


async def _run_channel_binding_maintenance(trigger: str) -> None:
    """Refresh the logical projection after a committed channel update."""
    try:
        from epg_maintenance import run_epg_binding_maintenance
        result = await run_epg_binding_maintenance(sync_logical=True, trigger=trigger)
        if result['status'] != 'success':
            logger.warning('EPG binding maintenance deferred: %s', result['error'])
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning('EPG binding maintenance trigger failed: %s', type(error).__name__)


async def _run_epg_preference_maintenance(subscription_id: int, hints: tuple[tuple[str, str], ...]) -> None:
    """Persist derived M3U evidence after subscription data has committed."""
    try:
        from epg_preference_evidence import replace_subscription_url_tvg_evidence
        await replace_subscription_url_tvg_evidence(subscription_id, hints)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning('EPG preference evidence maintenance failed: %s', type(error).__name__)

@app.post("/api/admin/subscriptions", dependencies=[Depends(require_admin)])
async def add_subscription(request: Request):
    body = await request.json()
    url = (body.get('url') or '').strip()
    title = (body.get('title') or '').strip()
    custom_ua = (body.get('custom_ua') or '').strip()
    force_proxy = 1 if body.get('force_proxy') else 0
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")

    existing = await db.get_subscription_by_url(url)
    if existing:
        raise HTTPException(
            status_code=409,
            detail="订阅源已存在",
        )

    try:
        document = await _fetch_subscription_document(url, custom_ua=custom_ua)
    except SubscriptionFetchError as exc:
        status_code = 400 if exc.code == "unsafe_target" else 502
        raise SubscriptionRefreshHttpError(
            status_code=status_code,
            code=exc.code,
            detail=exc.detail,
        ) from exc

    channels = list(document.channels)
    if not channels:
        raise HTTPException(status_code=400, detail="未解析到任何频道")

    channels = deduplicate_channels(channels)

    # 自动检测标题
    if not title:
        title = _guess_sub_title(url, channels)

    try:
        sub_id = await db.add_subscription_with_channels(
            title=title,
            url=url,
            channels=channels,
            custom_ua=custom_ua,
            force_proxy=force_proxy,
        )
    except db.DuplicateSubscriptionError as exc:
        raise HTTPException(status_code=409, detail="订阅源已存在") from exc
    await _run_epg_preference_maintenance(sub_id, document.epg_url_hints)
    await _run_channel_binding_maintenance('subscription_add')
    # 频道数据变更后失效 Cover 缓存
    from core.cover_cache import invalidate_all_covers
    invalidate_all_covers()

    return {"id": sub_id, "title": title, "url": url, "channel_count": len(channels)}


@app.get("/api/admin/subscriptions", dependencies=[Depends(require_admin)])
async def list_subscriptions():
    return await db.get_subscriptions()


@app.get("/api/admin/subscriptions/{sub_id}", dependencies=[Depends(require_admin)])
async def get_subscription(sub_id: int):
    sub = await db.get_subscription(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    return sub


@app.delete("/api/admin/subscriptions/{sub_id}", dependencies=[Depends(require_admin)])
async def delete_subscription(sub_id: int):
    sub = await db.get_subscription(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    await db.delete_subscription(sub_id)
    await _run_channel_binding_maintenance('subscription_delete')
    # 频道数据变更后失效 Cover 缓存
    from core.cover_cache import invalidate_all_covers
    invalidate_all_covers()
    return {"ok": True}


async def _refresh_regular_subscription(sub: dict) -> dict:
    refresh_generation = await db.begin_subscription_refresh(sub['id'])
    try:
        document = await _fetch_subscription_document(
            sub['url'],
            custom_ua=sub.get('custom_ua') or '',
        )
    except asyncio.CancelledError:
        await _record_subscription_refresh_failure(
            sub['id'],
            refresh_generation,
            status='cancelled',
            error='订阅刷新已取消',
        )
        raise
    except SubscriptionFetchError as exc:
        await _record_subscription_refresh_failure(
            sub['id'],
            refresh_generation,
            status=exc.code,
            error=exc.detail,
        )
        raise SubscriptionRefreshHttpError(
            status_code=502,
            code=exc.code,
            detail=f"{exc.detail}，已保留旧数据",
        ) from exc

    channels = list(document.channels)
    channels = deduplicate_channels(channels)
    if not channels:
        error = "刷新结果未解析到任何频道，已保留旧数据"
        await _record_subscription_refresh_failure(
            sub['id'],
            refresh_generation,
            status='empty',
            error=error,
        )
        raise SubscriptionRefreshHttpError(
            status_code=502,
            code='empty',
            detail=error,
        )
    try:
        await db.replace_subscription_channels_atomic(
            sub['id'], channels, valid=1,
            expected_generation=refresh_generation,
        )
    except db.SubscriptionRefreshSuperseded as exc:
        raise SubscriptionRefreshHttpError(
            status_code=409,
            code='superseded',
            detail="刷新结果已被更新的请求取代",
        ) from exc
    except asyncio.CancelledError:
        await _record_subscription_refresh_failure(
            sub['id'],
            refresh_generation,
            status='cancelled',
            error='订阅刷新已取消',
        )
        raise
    except Exception as exc:
        error = "订阅数据保存失败，已保留旧数据"
        await _record_subscription_refresh_failure(
            sub['id'],
            refresh_generation,
            status='save_failed',
            error=error,
        )
        logger.warning(
            "Subscription dataset replacement failed: %s",
            type(exc).__name__,
        )
        raise SubscriptionRefreshHttpError(
            status_code=500,
            code='save_failed',
            detail=error,
        ) from exc
    await _run_epg_preference_maintenance(sub['id'], document.epg_url_hints)
    await _run_channel_binding_maintenance('subscription_refresh')
    # 频道数据变更后失效 Cover 缓存
    from core.cover_cache import invalidate_all_covers
    invalidate_all_covers()

    return {"status": "success", "channel_count": len(channels)}


async def _refresh_market_subscription(sub: dict) -> dict:
    package_id = str(sub.get('url') or '').removeprefix('market://').strip()
    if not package_id:
        raise HTTPException(status_code=400, detail="Market 订阅缺少 package_id")
    try:
        result = await _market.update_installed_package(package_id)
    except Exception as exc:
        _market_http_error(exc)
    # 频道数据变更后失效 Cover 缓存
    from core.cover_cache import invalidate_all_covers
    invalidate_all_covers()
    return result


@app.post("/api/admin/subscriptions/{sub_id}/refresh", dependencies=[Depends(require_admin)])
async def refresh_subscription(sub_id: int):
    sub = await db.get_subscription(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    if str(sub.get('url') or '').startswith('market://'):
        return await _refresh_market_subscription(sub)
    return await _refresh_regular_subscription(sub)


@app.post("/api/admin/subscriptions/refresh-all", dependencies=[Depends(require_admin)])
async def refresh_all_subscriptions():
    subs = await db.get_subscriptions()
    regular_limit = asyncio.Semaphore(SUBSCRIPTION_REFRESH_CONCURRENCY)
    market_lock = asyncio.Lock()

    async def refresh_one(sub: dict) -> dict:
        try:
            if str(sub.get('url') or '').startswith('market://'):
                async with market_lock:
                    result = await _refresh_market_subscription(sub)
            else:
                async with regular_limit:
                    result = await _refresh_regular_subscription(sub)
            return {
                "subscription_id": sub.get("id"),
                "title": sub.get("title"),
                "status": "updated",
                **(result or {}),
            }
        except HTTPException as exc:
            return {
                "subscription_id": sub.get("id"),
                "title": sub.get("title"),
                "status": "failed",
                "error_code": getattr(exc, "code", "refresh_failed"),
                "error": exc.detail if isinstance(exc.detail, str) else "订阅刷新失败",
            }
        except Exception as exc:
            logger.warning(
                "Subscription batch refresh item failed: %s",
                type(exc).__name__,
            )
            return {
                "subscription_id": sub.get("id"),
                "title": sub.get("title"),
                "status": "failed",
                "error_code": "internal_error",
                "error": "订阅刷新发生内部错误",
            }

    results = await asyncio.gather(*(refresh_one(sub) for sub in subs))
    updated = sum(item["status"] == "updated" for item in results)
    failed = len(results) - updated
    # 批量刷新后统一失效一次
    if updated > 0:
        from core.cover_cache import invalidate_all_covers
        invalidate_all_covers()
    status = "success" if failed == 0 else ("partial" if updated else "failed")
    return {
        "ok": True,
        "status": status,
        "updated": updated,
        "failed": failed,
        "results": results,
    }


# ── 频道 ──

@app.get("/api/admin/subscriptions/{sub_id}/channels", dependencies=[Depends(require_admin)])
async def list_channels(sub_id: int, group: str = '', search: str = ''):
    sub = await db.get_subscription(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    channels = await db.get_channels(sub_id, group=group, search=search)
    groups = await db.get_channel_groups(sub_id)
    return {"channels": channels, "groups": groups, "total": len(channels)}


# =====================================================================
# WaveFlow Market
# =====================================================================


class MarketAutomationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None
    interval_seconds: StrictInt | None = None

    @model_validator(mode="after")
    def validate_update(self):
        if self.enabled is None and self.interval_seconds is None:
            raise ValueError("至少需要提供 enabled 或 interval_seconds")
        if self.interval_seconds is not None:
            if self.interval_seconds < MARKET_MINIMUM_INTERVAL_SECONDS:
                raise ValueError("interval_seconds 小于允许下限")
            if (
                self.interval_seconds > MARKET_MAXIMUM_INTERVAL_SECONDS
            ):
                raise ValueError("interval_seconds 大于允许上限")
        return self


class MarketAutomationResponse(BaseModel):
    task_id: str
    enabled: bool
    interval_seconds: int
    minimum_interval_seconds: int
    maximum_interval_seconds: int | None
    initial_delay_seconds: int
    scheduled_task_type: str
    service_started: bool
    is_running: bool
    task_type: str
    last_started_at: str
    last_finished_at: str
    last_status: str
    checked_count: int
    updated_count: int
    skipped_count: int
    failed_count: int
    last_error: str


class MarketAutomationRunResponse(BaseModel):
    task_id: str
    task_type: str
    status: str
    checked_count: int
    updated_count: int
    skipped_count: int
    failed_count: int
    error: str
    errors: list[str]
    started_at: str
    finished_at: str


class MarketAutomationBusyResponse(BaseModel):
    code: str = "automation_busy"
    message: str = "Market 自动任务正在运行"
    current_task_type: str
    started_at: str
    status: str


def _get_market_automation_service(request: Request):
    service = getattr(request.app.state, "automation_service", None)
    if service is None or not service.is_started:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "automation_unavailable",
                "message": "Market 自动任务服务尚未就绪",
            },
        )
    return service


def _get_market_automation_definition(service):
    try:
        return service.registry.get(MARKET_TASK_ID)
    except (AutomationTaskNotFoundError, AutomationConfigurationError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "automation_unavailable",
                "message": "Market 自动任务配置不可用",
            },
        ) from exc


async def _market_automation_snapshot(request: Request) -> MarketAutomationResponse:
    service = _get_market_automation_service(request)
    try:
        definition = _get_market_automation_definition(service)
        config = await service.repository.ensure_config(definition)
        state = await service.repository.get_state(MARKET_TASK_ID)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("读取 Market 自动任务状态失败")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "automation_status_failed",
                "message": "读取 Market 自动任务状态失败",
            },
        ) from exc

    return MarketAutomationResponse(
        task_id=definition.task_id,
        enabled=config.enabled,
        interval_seconds=config.interval_seconds,
        minimum_interval_seconds=definition.minimum_interval_seconds,
        maximum_interval_seconds=definition.maximum_interval_seconds,
        initial_delay_seconds=definition.initial_delay_seconds,
        scheduled_task_type=definition.scheduled_task_type or "",
        service_started=service.is_started,
        is_running=bool(state and state.status == "running"),
        task_type=state.task_type if state else "",
        last_started_at=state.last_started_at if state else "",
        last_finished_at=state.last_finished_at if state else "",
        last_status=state.status if state else "never_run",
        checked_count=state.checked_count if state else 0,
        updated_count=state.updated_count if state else 0,
        skipped_count=state.skipped_count if state else 0,
        failed_count=state.failed_count if state else 0,
        last_error=state.last_error if state else "",
    )


async def _execute_market_automation(request: Request, task_type: str):
    service = _get_market_automation_service(request)
    try:
        return await service.runner.run(
            AutomationRunRequest(
                task_id=MARKET_TASK_ID,
                task_type=task_type,
                trigger="manual_api",
            )
        )
    except (AutomationTaskNotFoundError, AutomationConfigurationError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "automation_unavailable",
                "message": "Market 自动任务配置不可用",
            },
        ) from exc
    except AutomationRequestError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "automation_request_invalid",
                "message": str(exc),
            },
        ) from exc
    except AutomationOwnershipLostError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "automation_ownership_lost",
                "message": "Market 自动任务执行权已失效",
            },
        ) from exc
    except Exception as exc:
        logger.exception("执行 Market 自动任务失败: %s", task_type)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "automation_execution_failed",
                "message": "执行 Market 自动任务失败",
            },
        ) from exc


def _market_automation_run_response(result):
    if isinstance(result, AutomationBusy):
        body = MarketAutomationBusyResponse(
            current_task_type=result.task_type,
            started_at=result.started_at,
            status=result.status,
        )
        return JSONResponse(status_code=409, content=body.model_dump())
    if not isinstance(result, AutomationRunResult):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "automation_result_invalid",
                "message": "Market 自动任务返回了无效结果",
            },
        )
    return MarketAutomationRunResponse(
        task_id=result.task_id,
        task_type=result.task_type,
        status=result.status,
        checked_count=result.checked_count,
        updated_count=result.updated_count,
        skipped_count=result.skipped_count,
        failed_count=result.failed_count,
        error=result.error,
        errors=list(result.errors),
        started_at=result.started_at,
        finished_at=result.finished_at,
    )


def _invalidate_market_covers_after_update(result) -> None:
    if isinstance(result, AutomationRunResult) and result.updated_count > 0:
        from core.cover_cache import invalidate_all_covers
        invalidate_all_covers()

def _market_http_error(exc: Exception):
    from plugin_runtime import PluginError
    if isinstance(exc, PluginError):
        statuses = {
            "RESOURCE_NOT_FOUND": 404, "ARTIFACT_NOT_FOUND": 404,
            "PLUGIN_UNTRUSTED": 403, "CAPABILITY_DENIED": 403,
            "SCHEME_CONFLICT": 409, "PLUGIN_CANDIDATE_CONFLICT": 409,
            "PERMISSION_APPROVAL_REQUIRED": 409,
            # A Content Package dependency is a precondition of the Market
            # operation, not a malformed request: the operator resolves it (or
            # explicitly forces the uninstall) and retries.
            "DEPENDENCY_MISSING": 409, "PLUGIN_DEPENDENCY_ACTIVE": 409,
            "PLUGIN_UNAVAILABLE": 503,
            "PLUGIN_INCOMPATIBLE": 422, "PLATFORM_UNSUPPORTED": 422,
            "PYTHON_RUNTIME_UNSUPPORTED": 422, "DEPENDENCY_LOCK_INVALID": 422,
            "DEPENDENCY_PLATFORM_UNSUPPORTED": 422,
        }
        raise HTTPException(
            status_code=statuses.get(exc.code, 400), detail=exc.as_contract()
        ) from exc
    if isinstance(exc, _market.MarketError):
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _market_package_with_private_artifacts(package_id: str) -> dict:
    await _market.ensure_market_loaded()
    package = next(
        (item for item in _market.market_packages_snapshot() if item.get("id") == package_id),
        None,
    )
    if not package:
        package = await _market.get_package(package_id, include_internal=True)
    return package


def _plugin_identity_from_package(package: dict) -> str:
    manifest = package.get("plugin_manifest") or {}
    publisher = str(manifest.get("publisher_id") or "")
    plugin_id = str(manifest.get("plugin_id") or "")
    if not publisher or not plugin_id:
        raise _market.MarketError("Plugin Package manifest 无效", 422)
    return f"{publisher}/{plugin_id}"


async def _ensure_content_plugin_dependencies(request: Request, package: dict) -> list[str]:
    requirements = package.get("requires_plugins") or []
    if not requirements:
        return []
    subsystem = getattr(request.app.state, "plugin_subsystem", None)
    if subsystem is None:
        from plugin_runtime import PluginError
        raise PluginError("PLUGIN_UNAVAILABLE", "Plugin subsystem is unavailable", category="runtime")
    packages = _market.market_packages_snapshot()
    installed: list[str] = []
    for requirement in requirements:
        identity = str(requirement.get("plugin") or "")
        projection = await subsystem.service.dependency_projection([requirement])
        if projection.get("status") == "ready":
            continue
        status = str(projection.get("status") or "dependency_missing")
        if status != "dependency_missing":
            from plugin_runtime import PluginError
            code = "PLUGIN_INCOMPATIBLE" if status == "plugin_incompatible" else "PLUGIN_UNAVAILABLE"
            raise PluginError(
                code,
                f"Required Plugin {identity} is {status}",
                category="dependency",
                details={"plugin": identity, "status": status},
            )
        try:
            result = await subsystem.install(identity, packages)
        except Exception as exc:
            from plugin_runtime import PluginError
            if isinstance(exc, PluginError) and exc.code == "PERMISSION_APPROVAL_REQUIRED":
                raise PluginError(
                    exc.code, exc.message, retryable=exc.retryable, category=exc.category,
                    details={**exc.details, "plugin": identity},
                ) from exc
            raise
        if not result.get("enabled"):
            await subsystem.service.enable(identity)
        installed.append(identity)
    return installed


@app.get("/api/admin/market", dependencies=[Depends(require_admin)])
async def get_market_summary():
    try:
        await _market.ensure_market_loaded()
        return _market.market_summary()
    except Exception as exc:
        _market_http_error(exc)


@app.get(
    "/api/admin/market/automation",
    response_model=MarketAutomationResponse,
    dependencies=[Depends(require_admin)],
)
async def get_market_automation(request: Request):
    return await _market_automation_snapshot(request)


@app.patch(
    "/api/admin/market/automation",
    response_model=MarketAutomationResponse,
    dependencies=[Depends(require_admin)],
)
async def update_market_automation(request: Request, body: MarketAutomationUpdateRequest):
    service = _get_market_automation_service(request)
    definition = _get_market_automation_definition(service)
    if body.interval_seconds is not None:
        if body.interval_seconds < definition.minimum_interval_seconds:
            raise HTTPException(status_code=422, detail="interval_seconds 小于允许下限")
        if (
            definition.maximum_interval_seconds is not None
            and body.interval_seconds > definition.maximum_interval_seconds
        ):
            raise HTTPException(status_code=422, detail="interval_seconds 大于允许上限")
    try:
        updated = await service.repository.update_config(
            MARKET_TASK_ID,
            enabled=body.enabled,
            interval_seconds=body.interval_seconds,
        )
    except Exception as exc:
        logger.exception("更新 Market 自动任务配置失败")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "automation_config_update_failed",
                "message": "更新 Market 自动任务配置失败",
            },
        ) from exc
    if updated is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "automation_unavailable",
                "message": "Market 自动任务配置不存在",
            },
        )
    try:
        service.notify_config_changed(MARKET_TASK_ID)
    except AutomationTaskNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "automation_unavailable",
                "message": "Market 自动任务调度器尚未就绪",
            },
        ) from exc
    return await _market_automation_snapshot(request)


@app.post(
    "/api/admin/market/check-updates",
    response_model=MarketAutomationRunResponse,
    responses={409: {"model": MarketAutomationBusyResponse}},
    dependencies=[Depends(require_admin)],
)
async def check_market_updates(request: Request):
    result = await _execute_market_automation(request, "check")
    return _market_automation_run_response(result)


@app.post(
    "/api/admin/market/run-auto-update",
    response_model=MarketAutomationRunResponse,
    responses={409: {"model": MarketAutomationBusyResponse}},
    dependencies=[Depends(require_admin)],
)
async def run_market_auto_update(request: Request):
    result = await _execute_market_automation(request, "auto_update")
    _invalidate_market_covers_after_update(result)
    return _market_automation_run_response(result)


@app.post(
    "/api/admin/market/update-all",
    response_model=MarketAutomationRunResponse,
    responses={409: {"model": MarketAutomationBusyResponse}},
    dependencies=[Depends(require_admin)],
)
async def update_all_market_packages(request: Request):
    result = await _execute_market_automation(request, "update_all")
    _invalidate_market_covers_after_update(result)
    return _market_automation_run_response(result)


@app.post("/api/admin/market/refresh", dependencies=[Depends(require_admin)])
async def refresh_market(request: Request):
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        source_id = (body or {}).get("source_id")
        result = await _market.refresh_market(
            (body or {}).get("market_url"),
            allow_private=_truthy_query((body or {}).get("allow_private", False)),
            source_id=int(source_id) if source_id else None,
        )
        from core.cover_cache import invalidate_all_covers
        invalidate_all_covers()
        return result
    except Exception as exc:
        _market_http_error(exc)


@app.get("/api/admin/market/sources", dependencies=[Depends(require_admin)])
async def list_market_sources():
    try:
        return {"sources": await _market.list_sources()}
    except Exception as exc:
        _market_http_error(exc)


@app.post("/api/admin/market/sources", dependencies=[Depends(require_admin)])
async def create_market_source(request: Request):
    try:
        body = await request.json()
        source = await _market.create_source(
            name=(body or {}).get("name", ""),
            url=(body or {}).get("url", ""),
            enabled=_truthy_query((body or {}).get("enabled", True)),
            allow_private=_truthy_query((body or {}).get("allow_private", False)),
        )
        return source
    except Exception as exc:
        _market_http_error(exc)


@app.put("/api/admin/market/sources/{source_id}", dependencies=[Depends(require_admin)])
async def update_market_source(source_id: int, request: Request):
    try:
        body = await request.json()
        return await _market.update_source(source_id, body or {})
    except Exception as exc:
        _market_http_error(exc)


@app.delete("/api/admin/market/sources/{source_id}", dependencies=[Depends(require_admin)])
async def delete_market_source(source_id: int):
    try:
        return await _market.delete_source(source_id)
    except Exception as exc:
        _market_http_error(exc)


@app.get("/api/admin/market/packages", dependencies=[Depends(require_admin)])
async def list_market_packages(
    search: str = '',
    region: str = '',
    operator: str = '',
    provider: str = '',
    kind: str = '',
    status: str = '',
    category: str = '',
    tag: str = '',
    supported_only: bool = True,
    importable_only: bool = False,
):
    try:
        packages = await _market.list_packages({
            "search": search,
            "region": region,
            "operator": operator,
            "provider": provider,
            "kind": kind,
            "status": status,
            "category": category,
            "tag": tag,
            "supported_only": supported_only,
            "importable_only": importable_only,
        })
        return {"packages": packages, "total": len(packages)}
    except Exception as exc:
        _market_http_error(exc)


@app.get("/api/admin/market/packages/{package_id}", dependencies=[Depends(require_admin)])
async def get_market_package(package_id: str):
    try:
        return await _market.get_package(package_id)
    except Exception as exc:
        _market_http_error(exc)


@app.post("/api/admin/market/packages/{package_id}/preview", dependencies=[Depends(require_admin)])
async def preview_market_package(package_id: str):
    try:
        return await _market.build_preview(package_id)
    except Exception as exc:
        _market_http_error(exc)


@app.post("/api/admin/market/packages/{package_id}/import", dependencies=[Depends(require_admin)])
async def import_market_package(package_id: str, request: Request):
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        package = await _market_package_with_private_artifacts(package_id)
        if package.get("package_type") == _market.PLUGIN_PACKAGE_TYPE:
            subsystem = getattr(request.app.state, "plugin_subsystem", None)
            if subsystem is None:
                from plugin_runtime import PluginError
                raise PluginError("PLUGIN_UNAVAILABLE", "Plugin subsystem is unavailable", category="runtime")
            result = await subsystem.install(_plugin_identity_from_package(package), [package])
            automation = getattr(request.app.state, "automation_service", None)
            if automation is not None:
                from plugin_tasks import reconcile_plugin_update_task
                await reconcile_plugin_update_task(automation, subsystem)
            return {"ok": True, "package_type": "plugin_package", **result}
        dependencies = await _ensure_content_plugin_dependencies(request, package)
        result = await _market.import_package(
            package_id,
            preview_id=(body or {}).get("preview_id", ""),
            prefer_cached_preview=_truthy_query((body or {}).get("prefer_cached_preview", True)),
            reinstall=_truthy_query((body or {}).get("reinstall", False)),
        )
        from core.cover_cache import invalidate_all_covers
        invalidate_all_covers()
        return {**result, "installed_plugins": dependencies}
    except Exception as exc:
        _market_http_error(exc)


@app.post("/api/admin/market/packages/{package_id}/update", dependencies=[Depends(require_admin)])
async def update_market_package(package_id: str, request: Request):
    try:
        package = await _market_package_with_private_artifacts(package_id)
        if package.get("package_type") == _market.PLUGIN_PACKAGE_TYPE:
            subsystem = getattr(request.app.state, "plugin_subsystem", None)
            if subsystem is None:
                from plugin_runtime import PluginError
                raise PluginError("PLUGIN_UNAVAILABLE", "Plugin subsystem is unavailable", category="runtime")
            result = await subsystem.install(_plugin_identity_from_package(package), [package])
            automation = getattr(request.app.state, "automation_service", None)
            if automation is not None:
                from plugin_tasks import reconcile_plugin_update_task
                await reconcile_plugin_update_task(automation, subsystem)
            return {"ok": True, "package_type": "plugin_package", **result}
        # An update replaces the Content Package payload, and the new version may
        # declare different Plugin requirements than the installed one.  V1
        # re-validates on the update path exactly like the import path instead of
        # assuming the previously installed dependencies still hold.
        dependencies = await _ensure_content_plugin_dependencies(request, package)
        result = await _market.update_installed_package(package_id)
        from core.cover_cache import invalidate_all_covers
        invalidate_all_covers()
        return {**result, "installed_plugins": dependencies}
    except Exception as exc:
        _market_http_error(exc)


@app.patch("/api/admin/market/packages/{package_id}/install", dependencies=[Depends(require_admin)])
async def update_market_install(package_id: str, request: Request):
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        return await _market.update_install_config(
            package_id,
            auto_update=_truthy_query((body or {}).get("auto_update", False)) if "auto_update" in (body or {}) else None,
        )
    except Exception as exc:
        _market_http_error(exc)


@app.post(
    "/api/admin/market/updates/run",
    dependencies=[Depends(require_admin)],
    deprecated=True,
)
async def run_market_updates(request: Request):
    result = await _execute_market_automation(request, "update_all")
    _invalidate_market_covers_after_update(result)
    response = _market_automation_run_response(result)
    if isinstance(response, JSONResponse):
        return response
    return {
        **response.model_dump(),
        "updated": response.updated_count,
        "skipped": response.skipped_count,
        "failed": response.failed_count,
        "results": [],
    }


@app.delete("/api/admin/market/packages/{package_id}/install", dependencies=[Depends(require_admin)])
async def uninstall_market_package(package_id: str, request: Request):
    try:
        force = _truthy_query(request.query_params.get("force", False))
        package = await _market_package_with_private_artifacts(package_id)
        if package.get("package_type") == _market.PLUGIN_PACKAGE_TYPE:
            subsystem = getattr(request.app.state, "plugin_subsystem", None)
            if subsystem is None:
                from plugin_runtime import PluginError
                raise PluginError("PLUGIN_UNAVAILABLE", "Plugin subsystem is unavailable", category="runtime")
            removed = await subsystem.uninstall(_plugin_identity_from_package(package), force=force)
            automation = getattr(request.app.state, "automation_service", None)
            if automation is not None:
                from plugin_tasks import reconcile_plugin_update_task
                await reconcile_plugin_update_task(automation, subsystem)
            return {"ok": True, "package_type": "plugin_package", "uninstalled": removed}
        result = await _market.uninstall_package(package_id)
        from core.cover_cache import invalidate_all_covers
        invalidate_all_covers()
        return result
    except Exception as exc:
        _market_http_error(exc)


# ── 前端聚合频道列表（跨源去重，每个频道保留所有可用链接）──

def _content_category(name: str) -> str:
    """内容分类兜底：全国频道（无省份归属）按内容词分类。"""
    import re
    if re.search(r'少儿|卡通|动画|动漫|宝宝|宝贝', name): return '少儿'
    if re.search(r'电影|影院|剧场', name): return '电影'
    if re.search(r'体育|足球|篮球|网球|高尔夫', name): return '体育'
    if re.search(r'纪录|探索|地理', name): return '纪录'
    if re.search(r'购物', name): return '购物'
    if re.search(r'戏曲|梨园', name): return '戏曲'
    if re.search(r'新闻|资讯', name): return '新闻'
    if re.search(r'音乐|MTV', name): return '音乐'
    return ''


async def _get_aggregated_iptv_channels(group: str = '', search: str = '') -> tuple[list[dict], list[str]]:
    # Once the durable logical projection exists it is the read authority.
    # Never rebuild a conflicting/split membership from raw names on a read.
    projection = await db.get_iptv_logical_channel_projection()
    using_durable_projection = bool(projection)
    raw = projection if using_durable_projection else await db.get_aggregated_channels(group='', search='')

    from m3u8_parser import adapter_provider, channel_name_semantics, clean_channel_display_name, detect_source_type, normalize_channel_name, parse_youtube_channel_id, parse_youtube_video_id, source_display_label, _channel_alias
    from template import channel_template, normalize_group_name, detect_province
    from logo_template import logo_template

    logical_ids_by_key: dict[str, set[str]] = {}
    for item in raw:
        logical_id = str(item.get('logical_channel_id') or '')
        logical_name = str(item.get('logical_canonical_key') or '')
        if logical_id and logical_name:
            logical_ids_by_key.setdefault(logical_name, set()).add(logical_id)
    logical_public_keys = {
        logical_id: (
            logical_name if len(logical_ids_by_key.get(logical_name, set())) == 1
            else f'logical:{logical_id}'
        )
        for logical_id, logical_name in {
            str(item.get('logical_channel_id') or ''): str(item.get('logical_canonical_key') or '')
            for item in raw if item.get('logical_channel_id')
        }.items()
    }
    merged: dict[str, dict] = {}
    for ch in raw:
        semantics = channel_name_semantics(ch['name'])
        logical_id = str(ch.get('logical_channel_id') or '')
        if using_durable_projection and logical_id:
            key = logical_public_keys[logical_id]
            logical_candidate = str(ch.get('logical_canonical_key') or semantics['canonical_candidate'])
            display_name = str(ch.get('logical_display_name') or clean_channel_display_name(ch['name']))
        else:
            key = semantics['canonical_candidate']
            logical_candidate = key
            display_name = clean_channel_display_name(ch['name'])
        primary_name = _channel_alias.get_primary(display_name)
        if primary_name and primary_name != display_name:
            display_name = primary_name

        primary = _channel_alias.get_primary(ch['name'])
        tmpl_cat = channel_template.match(primary) or channel_template.match(ch['name'])
        raw_grp = ch['group_name'] or '其他'
        norm_grp = normalize_group_name(raw_grp)
        # 分类优先级（央视>卫视>省份>内容）：
        #   ① template 命中（CCTV5→央视、浙江卫视→卫视、金鹰卡通→少儿）
        #   ② detect_province 频道名识别省份（比源 group 可靠：临沂新闻→山东）
        #   ③ 源 group 有效省份（黑龙江地区→黑龙江）
        #   ④ 内容规则兜底（少儿/电影/体育/购物/戏曲）
        #   ⑤ 源 group 兜底
        if tmpl_cat:
            grp = tmpl_cat
        else:
            prov = detect_province(ch['name'])
            if prov:
                grp = prov
            elif norm_grp not in ('地方', '其他', '超清', 'hytest', 'zgzk'):
                grp = norm_grp
            else:
                grp = _content_category(ch['name']) or norm_grp

        if key not in merged:
            merged[key] = {
                'canonical_key': key,
                'logical_channel_id': logical_id,
                'logical_candidate': logical_candidate,
                'name': display_name,
                'group_name': grp,
                'logo_url': ch['logo_url'],
                'tvg_id': key,
                'tvg_name': ch['tvg_name'],
                'urls': [],
            }
        detected_source_type = detect_source_type(ch['url'])
        stored_source_type = ch.get('source_type')
        source_type = stored_source_type if stored_source_type and stored_source_type != 'hls' else detected_source_type
        youtube_video_id = parse_youtube_video_id(ch['url']) or ch.get('youtube_video_id')
        youtube_channel_id = parse_youtube_channel_id(ch['url'])
        source_entry = {
            'url': ch['url'],
            'is_working': ch['is_working'],
            'latency_ms': ch['latency_ms'],
            'last_tested': ch.get('last_tested', ''),
            'probe_status': ch.get('probe_status', 'untested'),
            'live_status': ch.get('live_status', 'unknown'),
            'probe_method': ch.get('probe_method', ''),
            'speed_mbps': ch.get('speed_mbps', 0),
            'resolution': ch.get('resolution', ''),
            'fps': ch.get('fps', 0),
            'video_codec': ch.get('video_codec', ''),
            'audio_codec': ch.get('audio_codec', ''),
            'requires_headers': ch.get('requires_headers', 0),
            'requires_proxy_declared': ch.get('requires_proxy_declared', 0),
            'proxy_required_hint': ch.get('proxy_required_hint', 0),
            'last_success_at': ch.get('last_success_at', ''),
            'last_error': ch.get('last_error', ''),
            'adapter_provider': ch.get('adapter_provider', ''),
            'adapter_title': ch.get('adapter_title', ''),
            'probe_meta_json': ch.get('probe_meta_json', '{}'),
            'sub_title': ch.get('sub_title', ''),
            'custom_ua': ch.get('custom_ua', ''),
            'referer': ch.get('referer', ''),
            'force_proxy': ch.get('force_proxy', 0),
            'source_type': source_type,
            'adapter': adapter_provider(ch['url']),
            'youtube_video_id': youtube_video_id,
            'youtube_channel_id': youtube_channel_id,
            'raw_name': ch['name'],
            'raw_tvg_id': ch.get('tvg_id', ''),
            'raw_tvg_name': ch.get('tvg_name', ''),
            'raw_group': ch.get('group_name', ''),
            'market_package_id': ch.get('market_package_id', ''),
            'market_source_id': ch.get('market_source_id', ''),
            'market_channel_id': ch.get('market_channel_id', ''),
            'market_source_item_id': ch.get('market_source_item_id', ''),
            'rtsp_timestamp_mode': ch.get('rtsp_timestamp_mode', 'passthrough'),
            'source_revision': source_revision_for(ch),
            'logical_channel_id': logical_id,
            'origin_display_name': ch.get('sub_title', ''),
            'provider_display_name': ch.get('adapter_provider', '') or ch.get('adapter_title', ''),
            'structured_qualifiers': {
                'operator': semantics.get('operator', ''),
                'quality_hint': semantics.get('quality_hint', ''),
                'role': semantics.get('role', 'main'),
                'transport': source_type,
            },
            'recommended_display_label': source_display_label(
                raw_name=ch.get('name', ''),
                subscription_title=ch.get('sub_title', ''),
                source_type=source_type,
                resolution=ch.get('resolution', ''),
                speed_mbps=ch.get('speed_mbps', 0),
            ),
        }
        source_entry['source_id'] = source_id_for(ch)
        merged[key]['urls'].append(source_entry)

    # 生成 tvg_id_candidates
    for ch in merged.values():
        raw_ids = list(dict.fromkeys(u['raw_tvg_id'] for u in ch['urls'] if u['raw_tvg_id']))
        raw_names = list(dict.fromkeys(u['raw_tvg_name'] or u['raw_name'] for u in ch['urls'] if u['raw_tvg_name'] or u['raw_name']))
        all_raw = [ch['canonical_key'], ch['tvg_id']] + raw_ids + raw_names + [ch['name']]
        candidates = list(dict.fromkeys(c for c in all_raw if c))
        normalized_candidates = list(dict.fromkeys(normalize_channel_name(c) for c in candidates if c))
        ch['tvg_id_candidates'] = candidates[:20]
        ch['normalized_candidates'] = normalized_candidates[:20]

        # 确保 tvg_name 不为空：优先取 display_name
        if not ch.get('tvg_name'):
            ch['tvg_name'] = ch['name']

        # 用 logo 模板覆盖：命中即换成 CDN 高清版，未命中维持原 M3U logo 兜底。
        # 见 backend/logo_template.py，模板按 canonical_key 查询，零误判、不影响去重。
        tmpl_logo = logo_template.lookup(ch['canonical_key'])
        if tmpl_logo:
            ch['logo_url'] = tmpl_logo

    # Installed Content/Logo Packages are the durable stable-logo authority.
    # Keep the existing ``logo_url`` shape for clients, adding provenance as a
    # non-breaking projection.  Provider visual metadata remains source-scoped
    # and is still served by /api/media/channel/{key}/visual.
    try:
        from logo_resolver import logo_resolver
        package_logos = await logo_resolver.resolve_many(list(merged.values()))
    except Exception:
        package_logos = {}
    for ch in merged.values():
        logo = package_logos.get(str(ch.get('logical_channel_id') or ''))
        if logo:
            ch['logo_url'] = logo['logo_url']
            ch['logo'] = logo
        elif ch.get('logo_url'):
            ch['logo'] = {
                'logo_url': ch['logo_url'],
                'source_type': 'existing_channel_or_template',
                'provenance': 'legacy_projection',
            }

    # 合并 logical EPG binding projection.  Runtime channel reads do not use
    # The runtime channel projection is sourced from logical bindings.
    epg_projection = {}
    try:
        epg_projection = await epg_read_resolver.resolve_epg_read_many(
            {str(ch.get('logical_candidate') or ch.get('canonical_key') or '') for ch in merged.values()}
        )
    except Exception:
        pass

    for ch in merged.values():
        resolution = epg_projection.get(str(ch.get('logical_candidate') or ch['canonical_key'])) or {}
        target = resolution.get('effective_target')
        binding = resolution.get('binding') or {}
        if target and resolution.get('effective_source') == 'logical':
            ch['epg_source_id'] = target.get('source_id')
            ch['epg_channel_id'] = target.get('channel_id') or ''
            ch['epg_match_type'] = binding.get('match_type', '')
            ch['epg_confidence'] = binding.get('confidence', 0)
            ch['epg_match_status'] = binding.get('status', 'matched')
            ch['epg_locked'] = bool(binding.get('locked'))
        else:
            ch['epg_source_id'] = None
            ch['epg_channel_id'] = ''
            ch['epg_match_type'] = ''
            ch['epg_confidence'] = 0
            ch['epg_match_status'] = 'unmatched'
            ch['epg_locked'] = False

    # 排序：可用优先，然后按延迟
    result = sorted(merged.values(), key=lambda c: (
        0 if any(u['is_working'] == 1 for u in c['urls']) else 1,
        min((u['latency_ms'] for u in c['urls'] if u['is_working'] == 1), default=9999),
    ))

    if search:
        needle = str(search).casefold()
        result = [
            ch for ch in result
            if needle in str(ch.get('name') or '').casefold()
            or any(needle in str(source.get('raw_name') or '').casefold() for source in ch.get('urls', []))
        ]
    if group:
        result = [c for c in result if c['group_name'] == group]

    groups = sorted(set(c['group_name'] for c in merged.values()))
    return result, groups


@app.get("/api/iptv/channels", dependencies=[Depends(require_browse_access)])
async def aggregated_channels(group: str = '', search: str = ''):
    result, groups = await _get_aggregated_iptv_channels(group=group, search=search)

    return {
        "channels": result,
        "groups": groups,
        "total": len(result),
    }


# ── 测速 ──

# 测速进度存储（内存）
_test_progress: dict[int, dict] = {}
_global_test_progress: dict = {}
_speed_test_lock = asyncio.Lock()
_speed_test_running = False
_speed_test_cancel_event: asyncio.Event | None = None
_speed_test_task: asyncio.Task | None = None


async def _shutdown_app_background_tasks() -> None:
    """Cancel and drain bounded admin/startup tasks owned by the app."""
    async with _speed_test_lock:
        if _speed_test_cancel_event is not None:
            _speed_test_cancel_event.set()
        tasks = tuple(
            task for task in _APP_BACKGROUND_TASKS
            if task is not asyncio.current_task() and not task.done()
        )
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _empty_test_progress(total: int = 0) -> dict:
    return {
        "total": total,
        "tested": 0,
        "working": 0,
        "failed": 0,
        "not_live": 0,
        "untested": 0,
        "phase": "",
        "current": "",
        "ffmpeg_active": 0,
        "cancelled": False,
    }


async def _begin_speed_test() -> None:
    global _speed_test_cancel_event, _speed_test_running
    async with _speed_test_lock:
        if _speed_test_running:
            raise HTTPException(status_code=409, detail="已有测速任务正在进行中")
        _speed_test_running = True
        _speed_test_cancel_event = asyncio.Event()


async def _finish_speed_test() -> None:
    global _speed_test_cancel_event, _speed_test_running, _speed_test_task
    async with _speed_test_lock:
        _speed_test_running = False
        _speed_test_cancel_event = None
        _speed_test_task = None


def _set_speed_test_task(task: asyncio.Task) -> None:
    global _speed_test_task
    _speed_test_task = task
    _track_app_background_task(task, owner="speed_test")


def _current_speed_test_cancel_event() -> asyncio.Event:
    return _speed_test_cancel_event or asyncio.Event()


def _probe_status_to_bucket(status: str) -> str:
    if status == "online":
        return "working"
    if status == "not_live":
        return "not_live"
    if status in {"unsupported", "untested"}:
        return "untested"
    return "failed"


@app.post("/api/admin/probes/test-all", dependencies=[Depends(require_admin)])
async def test_all_subscriptions():
    """测速所有订阅源的所有频道"""
    subs = await db.get_subscriptions()
    if not subs:
        raise HTTPException(status_code=400, detail="无订阅源")

    total = 0
    all_channels = []
    for sub in subs:
        channels = await db.get_channels(sub['id'])
        all_channels.extend(channels)
        total += len(channels)

    if not total:
        raise HTTPException(status_code=400, detail="无频道可测速")

    await _begin_speed_test()
    try:
        await db.reset_channel_statuses_all()
    except Exception:
        await _finish_speed_test()
        raise
    _global_test_progress.clear()
    _global_test_progress.update(_empty_test_progress(total))
    logger.info("开始测速: %d 个频道", total)
    task = asyncio.create_task(_run_speed_test_global(all_channels, _current_speed_test_cancel_event()))
    _set_speed_test_task(task)
    return {"total": total}


@app.post("/api/admin/subscriptions/{sub_id}/test-all", dependencies=[Depends(require_admin)])
async def test_subscription(sub_id: int):
    """测速单个订阅源的所有频道"""
    sub = await db.get_subscription(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")

    channels = await db.get_channels(sub_id)
    if not channels:
        raise HTTPException(status_code=400, detail="该订阅无频道")

    await _begin_speed_test()
    try:
        await db.reset_channel_statuses(sub_id)
    except Exception:
        await _finish_speed_test()
        raise
    _test_progress[sub_id] = _empty_test_progress(len(channels))
    _global_test_progress.clear()
    _global_test_progress.update(_empty_test_progress(len(channels)))
    logger.info("开始测速订阅 %s: %d 个频道", sub['title'], len(channels))
    task = asyncio.create_task(_run_speed_test_sub(sub_id, channels, _current_speed_test_cancel_event()))
    _set_speed_test_task(task)
    return {"total": len(channels)}


def _speed_test_semaphore_for_channel(
    ch: dict,
    semaphores: dict[str, asyncio.Semaphore],
    default_sem: asyncio.Semaphore,
    adapter_limits: dict[str, int],
) -> asyncio.Semaphore:
    from m3u8_parser import adapter_provider as _ap, is_youtube_url as _is_yt

    url = ch.get("url", "")
    name = "youtube" if _is_yt(url) else _ap(url)
    limit = adapter_limits.get(name)
    if limit is None:
        return default_sem
    return semaphores.setdefault(name, asyncio.Semaphore(limit))


async def _run_speed_test_sub(sub_id: int, channels: list[dict], cancel_event: asyncio.Event):
    adapter_limits: dict[str, int] = {"youtube": 1}
    semaphores: dict[str, asyncio.Semaphore] = {}
    default_sem = asyncio.Semaphore(10)

    tasks: list[asyncio.Task] = []

    async def _limited_test(ch):
        expected_source_revision = source_revision_for(ch)
        async with _speed_test_semaphore_for_channel(ch, semaphores, default_sem, adapter_limits):
            if cancel_event.is_set():
                return ch, {"probe_status": "untested", "latency_ms": 0, "last_error": "cancelled"}, expected_source_revision
            try:
                result = await asyncio.wait_for(probe_channel_source(
                    ch, http_client, provider_resolver=getattr(app.state, "provider_resolver", None)
                ), timeout=24)
                return ch, result, expected_source_revision
            except asyncio.TimeoutError:
                return ch, {"probe_status": "timeout", "latency_ms": 0, "last_error": "timeout"}, expected_source_revision
            except Exception:
                return ch, {"probe_status": "error", "latency_ms": 0, "last_error": "probe_internal_error"}, expected_source_revision

    try:
        tasks = [asyncio.create_task(_limited_test(ch)) for ch in channels]
        for coro in asyncio.as_completed(tasks):
            if cancel_event.is_set():
                break
            result = {"probe_status": "error", "latency_ms": 0, "last_error": "unknown"}
            ch = None
            expected_source_revision = None
            try:
                ch, result, expected_source_revision = await coro
                _test_progress[sub_id]["current"] = ch.get("name", "")
                _test_progress[sub_id]["phase"] = result.get("probe_method", "")
                _global_test_progress["current"] = ch.get("name", "")
                _global_test_progress["phase"] = result.get("probe_method", "")
                await db.update_channel_probe_result(
                    ch['id'], result, expected_source_revision=expected_source_revision
                )
            except Exception as exc:
                logger.warning("测速异常: %s", type(exc).__name__)
                if ch:
                    await db.update_channel_probe_result(
                        ch['id'], result, expected_source_revision=expected_source_revision
                    )
            bucket = _probe_status_to_bucket(str(result.get("probe_status") or "error"))
            _test_progress[sub_id]['tested'] += 1
            _global_test_progress['tested'] += 1
            _test_progress[sub_id][bucket] += 1
            _global_test_progress[bucket] += 1
        if cancel_event.is_set():
            for task in tasks:
                task.cancel()
            _test_progress[sub_id]["phase"] = "cancelled"
            _global_test_progress["phase"] = "cancelled"
            _test_progress[sub_id]["cancelled"] = True
            _global_test_progress["cancelled"] = True
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            await db.update_subscription(sub_id, last_tested=datetime.now(timezone.utc).isoformat())
    except asyncio.CancelledError:
        cancel_event.set()
        _test_progress[sub_id]["phase"] = "cancelled"
        _global_test_progress["phase"] = "cancelled"
        _test_progress[sub_id]["cancelled"] = True
        _global_test_progress["cancelled"] = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await _finish_speed_test()


async def _run_speed_test_global(channels: list[dict], cancel_event: asyncio.Event):
    adapter_limits: dict[str, int] = {"youtube": 1}
    semaphores: dict[str, asyncio.Semaphore] = {}
    default_sem = asyncio.Semaphore(10)

    tasks: list[asyncio.Task] = []

    async def _limited_test(ch):
        expected_source_revision = source_revision_for(ch)
        async with _speed_test_semaphore_for_channel(ch, semaphores, default_sem, adapter_limits):
            if cancel_event.is_set():
                return ch, {"probe_status": "untested", "latency_ms": 0, "last_error": "cancelled"}, expected_source_revision
            try:
                result = await asyncio.wait_for(probe_channel_source(
                    ch, http_client, provider_resolver=getattr(app.state, "provider_resolver", None)
                ), timeout=24)
                return ch, result, expected_source_revision
            except asyncio.TimeoutError:
                logger.warning("测速超时: %s", ch.get('name', ch.get('url', ''))[:60])
                return ch, {"probe_status": "timeout", "latency_ms": 0, "last_error": "timeout"}, expected_source_revision
            except Exception:
                return ch, {"probe_status": "error", "latency_ms": 0, "last_error": "probe_internal_error"}, expected_source_revision

    try:
        tasks = [asyncio.create_task(_limited_test(ch)) for ch in channels]
        for coro in asyncio.as_completed(tasks):
            if cancel_event.is_set():
                break
            result = {"probe_status": "error", "latency_ms": 0, "last_error": "unknown"}
            ch = None
            expected_source_revision = None
            try:
                ch, result, expected_source_revision = await coro
                _global_test_progress["current"] = ch.get("name", "")
                _global_test_progress["phase"] = result.get("probe_method", "")
                await db.update_channel_probe_result(
                    ch['id'], result, expected_source_revision=expected_source_revision
                )
            except Exception as exc:
                logger.warning("测速异常: %s", type(exc).__name__)
                if ch:
                    await db.update_channel_probe_result(
                        ch['id'], result, expected_source_revision=expected_source_revision
                    )
            bucket = _probe_status_to_bucket(str(result.get("probe_status") or "error"))
            _global_test_progress['tested'] += 1
            _global_test_progress[bucket] += 1
            tested = _global_test_progress['tested']
            total = _global_test_progress['total']
            if tested % 50 == 0 or tested == total:
                logger.info(
                    "测速进度: %d/%d (可用:%d 不可用:%d 未开播:%d 未测试:%d)",
                    tested,
                    total,
                    _global_test_progress['working'],
                    _global_test_progress['failed'],
                    _global_test_progress['not_live'],
                    _global_test_progress['untested'],
                )
        if cancel_event.is_set():
            for task in tasks:
                task.cancel()
            _global_test_progress["phase"] = "cancelled"
            _global_test_progress["cancelled"] = True
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            # 测速完成，更新所有订阅的 last_tested
            now = datetime.now(timezone.utc).isoformat()
            subs = await db.get_subscriptions()
            for sub in subs:
                await db.update_subscription(sub['id'], last_tested=now)
    except asyncio.CancelledError:
        cancel_event.set()
        _global_test_progress["phase"] = "cancelled"
        _global_test_progress["cancelled"] = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await _finish_speed_test()


@app.post("/api/admin/probes/test-cancel", dependencies=[Depends(require_admin)])
async def cancel_speed_test():
    async with _speed_test_lock:
        if not _speed_test_running:
            return {"cancelled": False, "running": False}
        if _speed_test_cancel_event:
            _speed_test_cancel_event.set()
        if _speed_test_task and not _speed_test_task.done():
            _speed_test_task.cancel()
        return {"cancelled": True, "running": True}


@app.get("/api/admin/probes/test-status", dependencies=[Depends(require_admin)])
async def global_test_status():
    return _global_test_progress or _empty_test_progress()


@app.get("/api/admin/subscriptions/{sub_id}/test-status", dependencies=[Depends(require_admin)])
async def test_status(sub_id: int):
    return _test_progress.get(sub_id, _empty_test_progress())


# ── 播放代理 ──

# ── Thin playlist 代理缓存 ────────────────────────────
# 所有 playlist 拉取走这里：重试 + single-flight + 短暂成功回退。
# 不做队列、不做后台刷新、不自己维护 MEDIA-SEQUENCE。
_THIN_CACHE: dict[str, tuple[str, str, float]] = {}   # cache_key → (text, final_url, ts)


class _ThinLockEntry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_THIN_LOCKS: dict[str, _ThinLockEntry] = {}
_THIN_TTL = 1.5          # 新鲜窗口：合并并发请求 + 极短回放，避免 short-window 上游（如 yxfy 3×5s）丢段
_THIN_STALE_MAX_AGE = 15.0
_THIN_MAX_ENTRIES = 200


def _thin_cache_key(
    url: str,
    cache_scope: str = "",
    headers: dict[str, str] | None = None,
    omit_headers: set[str] | frozenset[str] | None = None,
) -> str:
    """按完整上游请求语义生成不暴露 token/header 的稳定 cache key。"""
    parsed = urlparse(url)
    request_url = parsed._replace(fragment="").geturl()
    normalized_headers = sorted(
        (str(key).strip().lower(), str(value).strip())
        for key, value in (headers or {}).items()
    )
    material = json.dumps(
        [cache_scope, request_url, normalized_headers, sorted(name.lower() for name in (omit_headers or ()))],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _thin_lock_entry(cache_key: str) -> _ThinLockEntry:
    """Register one user before it can wait, preventing split-lock races."""
    entry = _THIN_LOCKS.get(cache_key)
    if entry is None:
        entry = _ThinLockEntry()
        _THIN_LOCKS[cache_key] = entry
    entry.users += 1
    return entry


def _release_thin_lock_entry(cache_key: str, entry: _ThinLockEntry) -> None:
    entry.users -= 1
    if entry.users == 0 and _THIN_LOCKS.get(cache_key) is entry:
        _THIN_LOCKS.pop(cache_key, None)


async def _thin_playlist_fetch(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    cache_scope: str = "",
    omit_user_agent: bool = False,
) -> tuple[str, str]:
    """拉取上游 playlist，带薄韧性。

    - single-flight：同一 URL 并发请求合并为一次上游 fetch
    - 重试 1 次（共 2 次尝试，间隔 0.3s）
    - 成功结果按完整请求语义缓存 1.5s，作为后续瞬时失败时的回退
    - timeout/connect/5xx 全失败且有历史成功结果 → 返回 stale 缓存
    - 401/403/404/410 明确失效 → 清缓存且禁止 stale 回退
    - 全失败且无缓存 → raise HTTPException(502)

    返回 ``(响应文本, 最终 URL)``。
    """
    request_headers = dict(headers or {})
    omitted_headers = frozenset({"User-Agent"}) if omit_user_agent else frozenset()
    ck = _thin_cache_key(url, cache_scope, request_headers, omitted_headers)
    _h = dict(request_headers)
    _h.setdefault("Accept-Encoding", "identity")

    lock_entry = _thin_lock_entry(ck)

    try:
        async with lock_entry.lock:
            # single-flight：锁内再检查一次缓存（可能被上一个持有者写入）
            cached = _THIN_CACHE.get(ck)
            if cached and cached[2] > time.time():
                return cached[0], cached[1]

            last_exc = None
            last_text = ""
            last_url = ""
            allow_stale_fallback = True

            for attempt in range(2):
                try:
                    resp = await request_with_safe_redirects(
                        http_client,
                        "GET",
                        url,
                        headers=_h,
                        timeout=8,
                        omit_headers=omitted_headers,
                    )
                    resp.raise_for_status()
                    last_text = resp.text
                    last_url = str(resp.url)

                    # 成功：写入短暂缓存（按完整请求语义）
                    now = time.time()
                    _THIN_CACHE[ck] = (last_text, last_url, now + _THIN_TTL)
                    # 防止缓存无限制增长
                    if len(_THIN_CACHE) > _THIN_MAX_ENTRIES:
                        expired = [k for k, v in _THIN_CACHE.items() if v[2] <= now]
                        for k in expired:
                            _THIN_CACHE.pop(k, None)
                        # 如果过期清理不够，删最老的
                        while len(_THIN_CACHE) > _THIN_MAX_ENTRIES:
                            oldest = min(_THIN_CACHE, key=lambda k: _THIN_CACHE[k][2], default=None)
                            if oldest:
                                _THIN_CACHE.pop(oldest, None)
                            else:
                                break

                    return last_text, last_url
                except RedirectTargetRejected as exc:
                    raise HTTPException(status_code=403, detail=str(exc.cause)) from exc
                except httpx.HTTPError as exc:
                    last_exc = exc
                    response = getattr(exc, "response", None)
                    if response is not None and response.status_code in TOKEN_REFRESH_HTTP_STATUS_CODES:
                        # The upstream explicitly rejected this request identity. Returning an
                        # expired playlist would keep signed child handles pinned to the dead URL.
                        _THIN_CACHE.pop(ck, None)
                        allow_stale_fallback = False
                        break
                    if attempt == 0:
                        await asyncio.sleep(0.3)

            # Transient failures may use the last successful playlist even after its fresh TTL.
            cached = _THIN_CACHE.get(ck)
            stale_age = time.time() - cached[2] if cached else float("inf")
            if cached and allow_stale_fallback and stale_age <= _THIN_STALE_MAX_AGE:
                logger.warning(
                    "薄韧性回退: 上游 %s 拉取失败 (%s)，返回 %ds 前缓存",
                    url[:100], last_exc, int(stale_age + _THIN_TTL),
                )
                return cached[0], cached[1]

            raise HTTPException(status_code=502, detail=f"拉取 playlist 失败: {last_exc}")
    finally:
        _release_thin_lock_entry(ck, lock_entry)


# 旧 adapter 公共解析/播放入口已被 ``/api/media/channel/{key}/playlist.m3u8`` 取代：
# 入口完全基于稳定 channel canonical_key，后端内部完成 adapter resolve →
# ProxyContext 注入 → signed handle → 重写。前端不再传 adapter URL。


# ── adapter 直播间封面/头像 ─────────────────────────────────────────────
# 哪些 adapter 支持 cover，由 adapter 模块自己声明 ADAPTER_CAPABILITIES = {"cover": True}，
# 这里只做"中央实现"：轻量 metadata 抓取 + URL 缓存。失败/未声明一律返回 200 + 空字符串，
# 让前端继续走 logo_url 兜底，避免把这条非关键路径变成报错弹窗源。

_ADAPTER_COVER_SUCCESS_TTL_SECONDS = 6 * 3600  # cover URL 都是平台 CDN 长期 URL，6h 足够
_ADAPTER_COVER_FAILURE_TTL_SECONDS = 5 * 60    # 失败/未开播短缓存，避免反复打风控接口
_ADAPTER_COVER_HTTP_TIMEOUT = 6.0

_adapter_cover_cache: dict[str, dict] = {}
_adapter_cover_locks: dict[str, asyncio.Lock] = {}


def _adapter_cover_empty(adapter_name: str = '') -> dict:
    return {
        "ok": True,
        "adapter": adapter_name,
        "cover_url": "",
        "avatar_url": "",
        "title": "",
        "anchor_name": "",
        "is_live": False,
    }


def _adapter_cover_cache_get(cache_key: str) -> dict | None:
    item = _adapter_cover_cache.get(cache_key)
    if not item:
        return None
    if float(item.get("expires_at", 0)) <= time.time():
        _adapter_cover_cache.pop(cache_key, None)
        return None
    return dict(item.get("payload") or {})


def _adapter_cover_cache_set(cache_key: str, payload: dict, ttl: int) -> None:
    if ttl <= 0:
        _adapter_cover_cache.pop(cache_key, None)
        return
    _adapter_cover_cache[cache_key] = {
        "expires_at": time.time() + ttl,
        "payload": dict(payload),
    }


async def _fetch_bilibili_cover(room_id: str) -> dict:
    api = (
        "https://api.live.bilibili.com/xlive/web-room/v1/index/getH5InfoByRoom"
        f"?room_id={quote(room_id, safe='')}"
    )
    resp = await http_client.get(
        api,
        timeout=_ADAPTER_COVER_HTTP_TIMEOUT,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://live.bilibili.com/{room_id}",
        },
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    room_info = data.get("room_info") or {}
    base_info = ((data.get("anchor_info") or {}).get("base_info")) or {}
    cover = str(room_info.get("cover") or room_info.get("keyframe") or "").strip()
    avatar = str(base_info.get("face") or "").strip()
    return {
        "ok": True,
        "adapter": "bilibili",
        "cover_url": cover,
        "avatar_url": avatar,
        "title": str(room_info.get("title") or "").strip(),
        "anchor_name": str(base_info.get("uname") or "").strip(),
        "is_live": int(room_info.get("live_status") or 0) == 1,
    }


async def _fetch_douyu_cover(room_id: str) -> dict:
    api = f"https://www.douyu.com/betard/{quote(room_id, safe='')}"
    resp = await http_client.get(
        api,
        timeout=_ADAPTER_COVER_HTTP_TIMEOUT,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.douyu.com/",
        },
    )
    resp.raise_for_status()
    room = (resp.json() or {}).get("room") or {}
    avatar_field = room.get("avatar")
    if isinstance(avatar_field, dict):
        avatar = str(avatar_field.get("big") or avatar_field.get("middle") or "").strip()
    else:
        avatar = str(avatar_field or room.get("avatar_mid") or "").strip()
    return {
        "ok": True,
        "adapter": "douyu",
        "cover_url": str(room.get("room_pic") or "").strip(),
        "avatar_url": avatar,
        "title": str(room.get("room_name") or "").strip(),
        "anchor_name": str(room.get("owner_name") or "").strip(),
        "is_live": int(room.get("show_status") or 0) == 1 and int(room.get("videoLoop") or 0) == 0,
    }


async def _fetch_huya_cover(room_id: str) -> dict:
    api = (
        "https://mp.huya.com/cache.php?m=Live&do=profileRoom&showSecret=1"
        f"&roomid={quote(room_id, safe='')}"
    )
    resp = await http_client.get(
        api,
        timeout=_ADAPTER_COVER_HTTP_TIMEOUT,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.huya.com/",
        },
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    live_data = data.get("liveData") or {}
    profile = data.get("profileInfo") or {}
    avatar = str(live_data.get("avatar180") or profile.get("avatar180") or "").strip()
    live_status = live_data.get("liveStatus")
    is_live = (str(live_status).upper() == "ON") if live_status is not None else bool(live_data.get("screenshot"))
    return {
        "ok": True,
        "adapter": "huya",
        "cover_url": str(live_data.get("screenshot") or "").strip(),
        "avatar_url": avatar,
        "title": str(live_data.get("introduction") or "").strip(),
        "anchor_name": str(live_data.get("nick") or profile.get("nick") or "").strip(),
        "is_live": is_live,
    }


async def _fetch_kuaishou_cover(room_id: str) -> dict:
    # 复用 kuaishou.py 同款 __INITIAL_STATE__ 解析，但只读 poster/avatar，不取 playUrls。
    from curl_cffi.requests import AsyncSession

    url = f"https://live.kuaishou.com/u/{quote(room_id, safe='')}"
    async with AsyncSession(impersonate="chrome", timeout=_ADAPTER_COVER_HTTP_TIMEOUT) as session:
        resp = await session.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"kuaishou http {resp.status_code}")
    html = resp.text or ''
    match = re.search(r'<script>window\.__INITIAL_STATE__=(.*?);\(function\(\)\{var s;', html)
    if not match:
        return _adapter_cover_empty("kuaishou")
    raw = match.group(1)
    blocks = re.findall(r'(\{"liveStream".*?),"gameInfo', raw)
    if not blocks:
        return _adapter_cover_empty("kuaishou")
    obj = json.loads(blocks[0] + "}")
    live_stream = obj.get("liveStream") or {}
    author = obj.get("author") or {}
    poster = str(live_stream.get("poster") or live_stream.get("coverUrl") or "").strip()
    return {
        "ok": True,
        "adapter": "kuaishou",
        "cover_url": poster,
        "avatar_url": str(author.get("avatar") or author.get("headurl") or "").strip(),
        "title": str(live_stream.get("caption") or "").strip(),
        "anchor_name": str(author.get("name") or "").strip(),
        "is_live": bool(live_stream),
    }


_ADAPTER_COVER_FETCHERS = {
    "bilibili": _fetch_bilibili_cover,
    "douyu": _fetch_douyu_cover,
    "huya": _fetch_huya_cover,
    "kuaishou": _fetch_kuaishou_cover,
}


# ── 封面图片代理（绕过 CDN Referer 防盗链）──────────────────────────────
# 白名单：仅允许代理这些域名下的图片，防止被当作公共代理滥用。
# 格式: {域名后缀: 需要伪造的 Referer}
_COVER_IMG_PROXY_SOURCES = {
    "hdslb.com": "https://www.bilibili.com",
    "bilibili.com": "https://www.bilibili.com",
}


def _cover_img_referer_for(raw_url: str) -> str:
    """命中白名单返回需要伪造的 Referer；否则返回空串。"""
    host = (urlparse(raw_url).hostname or "").lower()
    for suffix, ref in _COVER_IMG_PROXY_SOURCES.items():
        if host == suffix or host.endswith("." + suffix):
            return ref
    return ""


def _cover_img_proxy_url(raw_url: str) -> str:
    """如果 raw_url 命中防盗链白名单，返回 image handle 路径；否则原样返回。"""
    if not _cover_img_referer_for(raw_url):
        return raw_url
    from security.proxy_handles import issue_cached_handle as _issue
    handle = _issue(kind="image", url=raw_url, src="adapter:cover")
    return f"/api/media/proxy/image/{handle}"


async def fetch_adapter_cover_payload(adapter_url: str) -> dict:
    """供 /api/media/channel/{key}/cover 调用。返回封面 payload。"""
    try:
        request = parse_adapter_url(adapter_url)
    except AdapterResolveError:
        return _adapter_cover_empty()

    if not adapter_supports(request.adapter, "cover"):
        return _adapter_cover_empty(request.adapter)

    cache_key = request.raw_url
    cached = _adapter_cover_cache_get(cache_key)
    if cached is not None:
        return cached

    lock = _adapter_cover_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _adapter_cover_cache_get(cache_key)
        if cached is not None:
            return cached

        fetcher = _ADAPTER_COVER_FETCHERS.get(request.adapter)
        if not fetcher:
            payload = _adapter_cover_empty(request.adapter)
            _adapter_cover_cache_set(cache_key, payload, _ADAPTER_COVER_FAILURE_TTL_SECONDS)
            return payload
        try:
            payload = await fetcher(request.resource_id.strip("/"))
            has_image = bool(str(payload.get("cover_url") or "").strip()) or bool(str(payload.get("avatar_url") or "").strip())
            ttl = _ADAPTER_COVER_SUCCESS_TTL_SECONDS if has_image else _ADAPTER_COVER_FAILURE_TTL_SECONDS
            for field in ("cover_url", "avatar_url"):
                raw = str(payload.get(field) or "").strip()
                if raw:
                    payload[field] = _cover_img_proxy_url(raw)
            _adapter_cover_cache_set(cache_key, payload, ttl)
            return payload
        except Exception as exc:
            logger.info("adapter cover fetch failed adapter=%s room=%s err=%s",
                        request.adapter, request.resource_id, exc)
            payload = _adapter_cover_empty(request.adapter)
            _adapter_cover_cache_set(cache_key, payload, _ADAPTER_COVER_FAILURE_TTL_SECONDS)
            return payload


# 旧 /api/iptv/adapter/cover / cover-img 入口已删除：
# 封面元数据走 /api/media/channel/{key}/cover；封面图片走 image handle。


async def serve_iptv_playlist_by_source(
    *,
    upstream_url: str,
    ctx_id: str,
    src_label: str,
    canonical_key: str,
    source_id: str = "",
    source_revision: str = "",
    access,
):
    """按上游 URL 拉取并重写 IPTV/Radio 的 Thin HLS playlist。

    这是所有新媒体 playlist 入口共用的边界：上游 URL 先经过 SSRF
    校验，再由 Thin fetch helper 负责安全 redirect、single-flight、短暂
    成功缓存和失败回退，最后交给统一的 M3U8 rewriter 生成 signed handles。
    """
    from security.proxy_context import get_registry as _get_registry
    from core.m3u8_rewriter import RewriteContext as _RC, rewrite_m3u8 as _rw

    if urlparse(upstream_url).scheme.lower() not in ("http", "https"):
        raise HTTPException(status_code=400, detail="上游 scheme 不被允许")

    ctx = _get_registry().get(ctx_id) if ctx_id else None
    headers: dict[str, str] = {}
    if ctx:
        if ctx.no_ua:
            if ctx.custom_ua:
                headers["User-Agent"] = ctx.custom_ua
        elif ctx.custom_ua:
            headers["User-Agent"] = ctx.custom_ua
        if ctx.referer:
            headers["Referer"] = ctx.referer
        if ctx.cookie:
            headers["Cookie"] = ctx.cookie
    headers.setdefault("Accept-Encoding", "identity")

    source_ref = source_id or f"channel:{canonical_key}"
    cache_scope = f"{source_ref}:{source_revision or ''}"
    # _thin_playlist_fetch validates the initial target immediately before the
    # request and every redirect hop. Avoid a second settings/DNS pass here.
    omit_user_agent = bool(ctx and ctx.no_ua and not ctx.custom_ua)
    text, final_url = await _thin_playlist_fetch(
        upstream_url,
        headers,
        cache_scope=cache_scope,
        omit_user_agent=omit_user_agent,
    )
    access_token = access.propagated_access_token if access else ""
    rewrite_ctx = _RC(
        base_url=final_url,
        src_id=source_ref,
        src_label=src_label,
        ctx_id=ctx_id,
        propagated_access_token=access_token or "",
        proxy_segments=True,
    )
    body = _rw(text, rewrite_ctx)
    return Response(
        content=body,
        media_type="application/x-mpegURL",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ── RTSP 代理内部入口（公共路由由 routers/media_proxy.py 提供）──


async def serve_rtsp_playlist_response(
    *,
    upstream_url: str,
    custom_ua: str = "",
    compat: bool = False,
    playback_options: RtspPlaybackOptions | None = None,
) -> FileResponse:
    """供 media_proxy router 调用的 RTSP 内部入口。

    这里是 RTSP proxy 的最终安全边界；不假设调用方已经执行过校验。
    """
    if not upstream_url:
        raise HTTPException(status_code=400, detail="缺少 target_url")
    _validate_rtsp_proxy_request(upstream_url)
    try:
        session_id, playlist_path = await _ensure_rtsp_hls_session(
            upstream_url,
            custom_ua,
            compat=compat,
            playback_options=playback_options,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("RTSP 代理失败")
        raise HTTPException(status_code=500, detail=f"RTSP 代理失败: {exc}") from exc

    session = RTSP_HLS_SESSIONS.get(session_id)
    if session:
        session["last_access"] = time.time()
    return FileResponse(
        playlist_path,
        media_type="application/x-mpegURL",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/media/proxy/rtsp-segments/{session_id}/{filename}", dependencies=[Depends(require_media_access)])
async def media_proxy_rtsp_segment(session_id: str, filename: str):
    """RTSP→HLS 本地分片（不签 handle，由 session_id 鉴权）。"""
    if not re.fullmatch(r"[0-9a-f]{24}", session_id) or not RTSP_SEGMENT_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="无效的分片地址")

    session = RTSP_HLS_SESSIONS.get(session_id)
    if session:
        session["last_access"] = time.time()
    segment_path = RTSP_HLS_ROOT / session_id / filename
    if not segment_path.exists():
        raise HTTPException(status_code=404, detail="分片不存在")

    return FileResponse(
        segment_path,
        media_type="video/MP2T",
        headers={"Cache-Control": "no-cache"},
    )


async def serve_iptv_proxy_stream_response(
    *,
    request: Request,
    upstream_url: str,
    upstream_headers: dict[str, str],
    stream_type: str = "",
) -> StreamingResponse:
    """供 media_proxy router 调用的 MPEG-TS/FLV 流代理。

    handle payload 已通过 SSRF 校验，reconnect 内部直接使用上游 URL。
    """
    parsed = urlparse(upstream_url)
    stream_log_origin = f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else "[invalid-origin]"
    headers = {
        "User-Agent": upstream_headers.get("User-Agent", CDN_REQUEST_HEADERS["User-Agent"]),
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Referer": upstream_headers.get("Referer", f"{parsed.scheme}://{parsed.netloc}/"),
        # 与 chunk 路径一致：强制 identity，避免 httpx 默认 gzip,br 在 mpegts/flv
        # 长连接上把 Content-Encoding 误标。下游不需要解码。
        "Accept-Encoding": "identity",
    }
    if upstream_headers.get("Cookie"):
        headers["Cookie"] = upstream_headers["Cookie"]

    stream_timeout = httpx.Timeout(None, connect=10.0, read=IPTV_STREAM_READ_TIMEOUT_SECONDS)
    stream_client = httpx.AsyncClient(
        timeout=stream_timeout,
        follow_redirects=False,
        verify=CDN_VERIFY_SSL,
    )

    async def open_upstream() -> httpx.Response:
        response: httpx.Response | None = None
        try:
            response = await stream_with_safe_redirects(
                stream_client,
                "GET",
                upstream_url,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            if response is not None:
                await response.aclose()
            raise

        content_length = response.headers.get("content-length")
        if content_length and content_length.isdigit():
            length = int(content_length)
            if 0 < length < IPTV_STREAM_SHORT_CONNECTION_BYTES:
                logger.warning(
                    "IPTV stream 上游 Content-Length 较小: %s bytes url=%s",
                    length,
                    stream_log_origin,
                )
        return response

    try:
        initial_upstream = await open_upstream()
    except RedirectTargetRejected as exc:
        await stream_client.aclose()
        raise HTTPException(status_code=403, detail=str(exc.cause)) from exc
    except httpx.HTTPError as exc:
        await stream_client.aclose()
        raise HTTPException(status_code=502, detail=f"拉取直播流失败: {_stream_error_summary(exc)}") from exc

    async def stream_bytes() -> AsyncIterator[bytes]:
        upstream: httpx.Response | None = initial_upstream
        no_data_retries = 0
        last_data_at = time.monotonic()
        async def client_disconnected() -> bool:
            return bool(request and await request.is_disconnected())

        async def sleep_unless_disconnected(delay: float) -> bool:
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if await client_disconnected():
                    return True
                await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            return await client_disconnected()

        try:
            while True:
                if await client_disconnected():
                    break

                opened_at = time.monotonic()
                bytes_this_connection = 0
                close_reason = "eof"

                try:
                    # identity 编码（headers 已声明），用 aiter_raw 跳过 httpx 解码层。
                    async for chunk in upstream.aiter_raw():
                        if await client_disconnected():
                            close_reason = "client disconnected"
                            break
                        if chunk:
                            bytes_this_connection += len(chunk)
                            no_data_retries = 0
                            last_data_at = time.monotonic()
                            yield chunk
                except httpx.ReadTimeout:
                    close_reason = f"read timeout after {IPTV_STREAM_READ_TIMEOUT_SECONDS:.0f}s"
                except httpx.HTTPError as exc:
                    close_reason = type(exc).__name__
                finally:
                    if upstream is not None:
                        await upstream.aclose()
                        upstream = None

                if await client_disconnected():
                    break

                elapsed = time.monotonic() - opened_at
                if bytes_this_connection < IPTV_STREAM_SHORT_CONNECTION_BYTES and elapsed < IPTV_STREAM_SHORT_CONNECTION_SECONDS:
                    logger.warning(
                        "IPTV stream 上游短连接结束: reason=%s bytes=%s elapsed=%.2fs url=%s",
                        close_reason,
                        bytes_this_connection,
                        elapsed,
                        stream_log_origin,
                    )
                else:
                    logger.info(
                        "IPTV stream 上游连接结束，准备重连: reason=%s bytes=%s elapsed=%.2fs",
                        close_reason,
                        bytes_this_connection,
                        elapsed,
                    )

                if bytes_this_connection == 0:
                    no_data_retries += 1
                    no_data_age = time.monotonic() - last_data_at
                    if no_data_retries >= IPTV_STREAM_NO_DATA_RETRIES or no_data_age > IPTV_STREAM_NO_DATA_TIMEOUT_SECONDS:
                        logger.warning(
                            "IPTV stream 上游连续无数据，结束代理流: retries=%s no_data_age=%.2fs url=%s",
                            no_data_retries,
                            no_data_age,
                            stream_log_origin,
                        )
                        break

                delay = min(
                    IPTV_STREAM_RECONNECT_DELAY_SECONDS * max(1, no_data_retries),
                    IPTV_STREAM_RECONNECT_MAX_DELAY_SECONDS,
                )
                if await sleep_unless_disconnected(delay):
                    break

                while True:
                    if await client_disconnected():
                        return
                    try:
                        upstream = await open_upstream()
                        break
                    except RedirectTargetRejected as exc:
                        logger.warning(
                            "IPTV stream redirect target rejected: %s",
                            exc.cause,
                        )
                        break
                    except httpx.HTTPError as exc:
                        no_data_retries += 1
                        no_data_age = time.monotonic() - last_data_at
                        logger.warning(
                            "IPTV stream 上游重连失败: retries=%s no_data_age=%.2fs error=%s url=%s",
                            no_data_retries,
                            no_data_age,
                            type(exc).__name__,
                            stream_log_origin,
                        )
                        if no_data_retries >= IPTV_STREAM_NO_DATA_RETRIES or no_data_age > IPTV_STREAM_NO_DATA_TIMEOUT_SECONDS:
                            return
                        retry_delay = min(
                            IPTV_STREAM_RECONNECT_DELAY_SECONDS * no_data_retries,
                            IPTV_STREAM_RECONNECT_MAX_DELAY_SECONDS,
                        )
                        if await sleep_unless_disconnected(retry_delay):
                            return
        finally:
            if upstream is not None:
                await upstream.aclose()
                upstream = None
            await stream_client.aclose()

    stream_kind = (stream_type or "").strip().lower()
    if stream_kind == "audio_http":
        media_type = _audio_proxy_content_type(
            upstream_url,
            initial_upstream.headers.get("content-type", ""),
        )
    else:
        media_type = "video/x-flv" if stream_kind == "http_flv" else "video/MP2T"

    return StreamingResponse(
        stream_bytes(),
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*",
        },
    )


def config_rtsp_proxy_enabled() -> bool:
    """供 media_proxy router 同步查询当前 RTSP 策略。"""
    from core.settings_service import get_effective_settings_sync
    return get_effective_settings_sync().enable_rtsp_proxy


# ── EPG ──

import epg as _epg


async def _epg_source_request_body(
    request: Request,
    *,
    allowed_fields: set[str],
) -> dict:
    try:
        body = await request.json()
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": "请求体必须是 JSON 对象"},
        ) from error
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": "请求体必须是 JSON 对象"},
        )
    unknown = sorted(set(body) - allowed_fields)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_request",
                "message": "请求包含不支持的字段",
                "fields": unknown,
            },
        )
    return body


def _raise_epg_source_management_error(
    error: epg_source_management.EpgSourceManagementError,
) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail=error.as_dict(),
    ) from error


def _raise_epg_binding_management_error(
    error: epg_binding_management.EpgBindingManagementError,
) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail=error.as_dict(),
    ) from error


def _raise_unexpected_epg_binding_error(
    error: BaseException,
    *,
    operation: str,
) -> None:
    logger.warning(
        "epg_binding_management_unavailable",
        extra={
            "epg_binding_management": {
                "operation": operation,
                "error_type": type(error).__name__,
            }
        },
    )
    raise HTTPException(
        status_code=500,
        detail={
            "code": "binding_write_failed",
            "message": "EPG binding 操作失败",
        },
    ) from error


async def _epg_source_projection(request: Request, source_id: int) -> dict:
    rows = await epg_management.list_epg_source_statuses(
        getattr(request.app.state, "automation_service", None)
    )
    source = next((row for row in rows if row["id"] == source_id), None)
    if source is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "source_not_found", "message": "EPG 来源不存在"},
        )
    return source


@app.post("/api/admin/epg/sources", dependencies=[Depends(require_admin)])
async def add_epg_source(request: Request):
    body = await _epg_source_request_body(
        request,
        allowed_fields={"name", "url", "enabled"},
    )
    try:
        source, preference_reconciled = (
            await epg_source_management.create_custom_epg_source(
                name=body.get("name"),
                url=body.get("url"),
                enabled=body.get("enabled", True),
            )
        )
        source_id = int(source["id"])
        automation_reconciled = await reconcile_epg_tasks_after_source_change(
            getattr(request.app.state, "automation_service", None),
            http_client,
            source_id=source_id,
            operation="create",
        )
        return {
            "source": await _epg_source_projection(request, source_id),
            "automation_reconciled": automation_reconciled,
            "preference_reconciled": preference_reconciled,
        }
    except epg_source_management.EpgSourceManagementError as error:
        _raise_epg_source_management_error(error)


@app.get("/api/admin/epg/sources", dependencies=[Depends(require_admin)])
async def list_epg_sources(request: Request):
    return await epg_management.list_epg_source_statuses(
        getattr(request.app.state, "automation_service", None)
    )


@app.get("/api/admin/epg/overview", dependencies=[Depends(require_admin)])
async def get_epg_management_overview():
    return await epg_management.get_epg_management_overview()


@app.get("/api/admin/epg/matching", dependencies=[Depends(require_admin)])
async def list_epg_matching_channels(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=epg_management.MAX_PAGE_SIZE),
    scope: str = Query("all"),
    logical_scope: str = Query("active"),
    diagnostic_status: str = Query(""),
    source_id: int | None = Query(None, ge=1),
    q: str = Query("", max_length=256),
):
    try:
        return await epg_management.list_epg_matching_channels(
            page=page,
            page_size=page_size,
            scope=scope,
            logical_scope=logical_scope,
            diagnostic_status=diagnostic_status,
            source_id=source_id,
            text=q,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/admin/epg/matching/{logical_channel_id}", dependencies=[Depends(require_admin)])
async def get_epg_matching_detail(
    logical_channel_id: str,
    candidate_limit: int = Query(10, ge=1, le=epg_management.MAX_CANDIDATES),
):
    try:
        detail = await epg_management.get_epg_matching_detail(
            logical_channel_id,
            candidate_limit=candidate_limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if detail is None:
        raise HTTPException(status_code=404, detail="逻辑频道不存在")
    return detail


@app.get("/api/admin/epg/catalog", dependencies=[Depends(require_admin)])
async def search_epg_catalog(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=epg_management.MAX_PAGE_SIZE),
    q: str = Query("", max_length=256),
    source_id: int | None = Query(None, ge=1),
    availability: str = Query("all"),
):
    try:
        return await epg_management.search_epg_catalog(
            page=page,
            page_size=page_size,
            text=q,
            source_id=source_id,
            availability=availability,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put(
    "/api/admin/epg/logical-channels/{logical_channel_id}/binding",
    dependencies=[Depends(require_admin)],
)
async def set_logical_epg_binding(logical_channel_id: str, request: Request):
    body = await _epg_source_request_body(
        request,
        allowed_fields={"epg_source_id", "epg_channel_id"},
    )
    try:
        return await epg_binding_management.bind_manual_epg_target(
            logical_channel_id=logical_channel_id,
            epg_source_id=body.get("epg_source_id"),
            epg_channel_id=body.get("epg_channel_id"),
        )
    except epg_binding_management.EpgBindingManagementError as error:
        _raise_epg_binding_management_error(error)
    except Exception as error:
        _raise_unexpected_epg_binding_error(error, operation="manual_bind")


@app.put(
    "/api/admin/epg/logical-channels/{logical_channel_id}/binding/lock",
    dependencies=[Depends(require_admin)],
)
async def lock_logical_epg_binding(logical_channel_id: str):
    try:
        return await epg_binding_management.set_logical_epg_binding_lock(
            logical_channel_id=logical_channel_id,
            locked=True,
        )
    except epg_binding_management.EpgBindingManagementError as error:
        _raise_epg_binding_management_error(error)
    except Exception as error:
        _raise_unexpected_epg_binding_error(error, operation="lock")


@app.delete(
    "/api/admin/epg/logical-channels/{logical_channel_id}/binding/lock",
    dependencies=[Depends(require_admin)],
)
async def unlock_logical_epg_binding(logical_channel_id: str):
    try:
        return await epg_binding_management.set_logical_epg_binding_lock(
            logical_channel_id=logical_channel_id,
            locked=False,
        )
    except epg_binding_management.EpgBindingManagementError as error:
        _raise_epg_binding_management_error(error)
    except Exception as error:
        _raise_unexpected_epg_binding_error(error, operation="unlock")


@app.post(
    "/api/admin/epg/logical-channels/{logical_channel_id}/restore-automatic",
    dependencies=[Depends(require_admin)],
)
async def restore_logical_epg_automatic(logical_channel_id: str):
    try:
        return await epg_binding_management.restore_logical_epg_automatic(
            logical_channel_id=logical_channel_id,
        )
    except epg_binding_management.EpgBindingManagementError as error:
        _raise_epg_binding_management_error(error)
    except Exception as error:
        _raise_unexpected_epg_binding_error(
            error,
            operation="restore_automatic",
        )


@app.put(
    "/api/admin/epg/logical-channels/{logical_channel_id}/no-epg",
    dependencies=[Depends(require_admin)],
)
async def disable_logical_channel_epg(logical_channel_id: str):
    try:
        return await epg_binding_management.disable_logical_epg(
            logical_channel_id=logical_channel_id,
        )
    except epg_binding_management.EpgBindingManagementError as error:
        _raise_epg_binding_management_error(error)
    except Exception as error:
        _raise_unexpected_epg_binding_error(error, operation="disable_epg")


@app.put(
    "/api/admin/epg/subscriptions/{subscription_id}/source-preference",
    dependencies=[Depends(require_admin)],
)
async def set_subscription_epg_source_preference(
    subscription_id: int,
    request: Request,
):
    body = await _epg_source_request_body(
        request,
        allowed_fields={"epg_source_id"},
    )
    try:
        return await epg_binding_management.set_subscription_epg_source_preference(
            subscription_id=subscription_id,
            epg_source_id=body.get("epg_source_id"),
        )
    except epg_binding_management.EpgBindingManagementError as error:
        _raise_epg_binding_management_error(error)
    except Exception as error:
        _raise_unexpected_epg_binding_error(
            error,
            operation="set_source_preference",
        )


@app.delete(
    "/api/admin/epg/subscriptions/{subscription_id}/source-preference",
    dependencies=[Depends(require_admin)],
)
async def clear_subscription_epg_source_preference(subscription_id: int):
    try:
        return await epg_binding_management.clear_subscription_epg_source_preference(
            subscription_id=subscription_id,
        )
    except epg_binding_management.EpgBindingManagementError as error:
        _raise_epg_binding_management_error(error)
    except Exception as error:
        _raise_unexpected_epg_binding_error(
            error,
            operation="clear_source_preference",
        )


@app.patch("/api/admin/epg/sources/{source_id}", dependencies=[Depends(require_admin)])
async def update_epg_source(source_id: int, request: Request):
    body = await _epg_source_request_body(
        request,
        allowed_fields={"name", "url", "enabled"},
    )
    try:
        _source, preference_reconciled = (
            await epg_source_management.update_managed_epg_source(
                source_id,
                **body,
            )
        )
        automation_reconciled = await reconcile_epg_tasks_after_source_change(
            getattr(request.app.state, "automation_service", None),
            http_client,
            source_id=source_id,
            operation="update",
        )
        return {
            "source": await _epg_source_projection(request, source_id),
            "automation_reconciled": automation_reconciled,
            "preference_reconciled": preference_reconciled,
        }
    except epg_source_management.EpgSourceManagementError as error:
        _raise_epg_source_management_error(error)


@app.get(
    "/api/admin/epg/sources/{source_id}/delete-impact",
    dependencies=[Depends(require_admin)],
)
async def preview_epg_source_delete(source_id: int):
    try:
        return await epg_source_management.preview_epg_source_delete(source_id)
    except epg_source_management.EpgSourceManagementError as error:
        _raise_epg_source_management_error(error)


@app.delete("/api/admin/epg/sources/{source_id}", dependencies=[Depends(require_admin)])
async def delete_epg_source(
    source_id: int,
    request: Request,
    confirm: bool = False,
):
    try:
        result, preference_reconciled = (
            await epg_source_management.delete_custom_epg_source(
                source_id,
                confirm=confirm,
            )
        )
        automation_reconciled = await reconcile_epg_tasks_after_source_change(
            getattr(request.app.state, "automation_service", None),
            http_client,
            source_id=source_id,
            operation="delete",
        )
        return {
            **result,
            "automation_reconciled": automation_reconciled,
            "preference_reconciled": preference_reconciled,
        }
    except epg_source_management.EpgSourceManagementError as error:
        _raise_epg_source_management_error(error)


@app.post(
    "/api/admin/epg/sources/{source_id}/refresh",
    dependencies=[Depends(require_admin)],
)
async def refresh_single_epg_source(source_id: int, request: Request):
    source = await db.get_epg_source(source_id)
    if source is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "source_not_found", "message": "EPG 来源不存在"},
        )
    if not bool(source.get("enabled")):
        raise HTTPException(
            status_code=409,
            detail={"code": "source_disabled", "message": "请先启用该 EPG 来源"},
        )
    service = getattr(request.app.state, "automation_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "automation_unavailable",
                "message": "EPG 自动任务服务暂不可用",
            },
        )
    try:
        result = await run_epg_source_refresh_now(
            service,
            http_client,
            source_id=source_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "epg_single_source_refresh_unavailable",
            extra={
                "epg_source_management": {
                    "source_id": source_id,
                    "error_type": type(error).__name__,
                }
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "refresh_unavailable",
                "message": "EPG 来源刷新暂不可用",
            },
        ) from error
    if isinstance(result, AutomationBusy):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_refresh_busy",
                "message": "该 EPG 来源正在刷新",
            },
        )
    failure_categories = {
        "partial": "refresh_partial",
        "failed": "refresh_failed",
        "cancelled": "refresh_cancelled",
    }
    messages = {
        "success": "EPG 来源刷新完成",
        "partial": "EPG 数据已刷新，但后续维护未完全完成",
        "failed": "EPG 来源刷新失败",
        "cancelled": "EPG 来源刷新已取消",
    }
    return {
        "source_id": source_id,
        "status": result.status,
        "updated": result.updated_count > 0,
        "failure_category": failure_categories.get(result.status, ""),
        "message": messages.get(result.status, "EPG 来源刷新结束"),
        "started_at": result.started_at,
        "finished_at": result.finished_at,
    }


async def _run_manual_epg_refresh(automation_service) -> None:
    try:
        if automation_service is not None and automation_service.is_started:
            await run_epg_refresh_now(automation_service, http_client)
        else:
            await _epg.refresh_epg_sources(http_client)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "epg_manual_refresh_failed",
            extra={"epg_automation": {"error_type": type(error).__name__}},
        )


@app.post("/api/admin/epg/refresh", dependencies=[Depends(require_admin)])
async def refresh_epg(request: Request):
    _track_app_background_task(
        asyncio.create_task(
            _run_manual_epg_refresh(getattr(request.app.state, "automation_service", None)),
            name="manual-epg-refresh",
        ),
        owner="manual_epg_refresh",
    )
    return {"ok": True}


def _epg_zoneinfo(tz: str = ''):
    try:
        return ZoneInfo(tz or 'Asia/Shanghai')
    except Exception:
        return timezone(timedelta(hours=8), 'Asia/Shanghai')


def _epg_parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def _epg_date_from_query(value: str, tzinfo) -> str:
    if value:
        try:
            return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
        except ValueError:
            pass
    return datetime.now(tzinfo).date().isoformat()


def _epg_day_bounds(date_value: str, tzinfo) -> tuple[str, str]:
    day = datetime.strptime(date_value, '%Y-%m-%d').date()
    start_local = datetime(day.year, day.month, day.day, tzinfo=tzinfo)
    end_local = start_local + timedelta(days=1) - timedelta(microseconds=1)
    return start_local.astimezone(timezone.utc).isoformat(), end_local.astimezone(timezone.utc).isoformat()


def _epg_available_dates(programs: list[dict], tzinfo, center_date: str) -> list[str]:
    center = datetime.strptime(center_date, '%Y-%m-%d').date()
    window_start = center - timedelta(days=7)
    window_end = center + timedelta(days=7)
    dates: set[str] = set()

    for program in programs:
        try:
            start_local = _epg_parse_iso(program['start']).astimezone(tzinfo)
            stop_local = _epg_parse_iso(program['stop']).astimezone(tzinfo) - timedelta(microseconds=1)
        except Exception:
            continue

        current_day = start_local.date()
        last_day = max(current_day, stop_local.date())
        while current_day <= last_day:
            if window_start <= current_day <= window_end:
                dates.add(current_day.isoformat())
            current_day += timedelta(days=1)

    return sorted(dates)


def _epg_nearest_date(requested_date: str, available_dates: list[str]) -> str:
    if not available_dates or requested_date in available_dates:
        return requested_date
    requested = datetime.strptime(requested_date, '%Y-%m-%d').date()
    return min(
        available_dates,
        key=lambda value: abs((datetime.strptime(value, '%Y-%m-%d').date() - requested).days),
    )


@app.get("/api/iptv/epg/programs/{canonical_key}", dependencies=[Depends(require_browse_access)])
async def get_epg_programs(canonical_key: str, date: str = '', tz: str = ''):
    comparison = None
    try:
        comparison = await epg_read_resolver.resolve_epg_read(
            canonical_key,
        )
        epg_read_resolver.emit_epg_read_diagnostic(comparison, context='programme')
    except Exception as exc:
        # A resolver failure is safe no-EPG for the logical-only read path.
        epg_read_resolver.emit_epg_read_resolver_error(context='programme', error=exc)

    effective_target = comparison.get('effective_target') if comparison else None
    if effective_target and effective_target.get('channel_id'):
        sid = effective_target['source_id']
        cid = effective_target['channel_id']
        effective_match_status = (comparison.get('binding') or {}).get('status', 'matched')
    else:
        tzinfo = _epg_zoneinfo(tz)
        return {
            "canonical_key": canonical_key,
            "match_status": (comparison or {}).get('management_mode') or 'unmatched',
            "current": None,
            "next": None,
            "programs": [],
            "date": _epg_date_from_query(date, tzinfo),
            "requested_date": date,
            "tz": str(tzinfo),
            "available_dates": [],
        }
    tzinfo = _epg_zoneinfo(tz)
    requested_date = _epg_date_from_query(date, tzinfo)
    now = datetime.now(timezone.utc).isoformat()

    today_local = datetime.now(tzinfo).date()
    window_start_local = datetime(today_local.year, today_local.month, today_local.day, tzinfo=tzinfo) - timedelta(days=7)
    window_end_local = window_start_local + timedelta(days=15) - timedelta(microseconds=1)
    window_programs = await db.get_epg_programs(
        sid,
        cid,
        start_after=window_start_local.astimezone(timezone.utc).isoformat(),
        start_before=window_end_local.astimezone(timezone.utc).isoformat(),
    )
    available_dates = _epg_available_dates(window_programs, tzinfo, today_local.isoformat())
    selected_date = _epg_nearest_date(requested_date, available_dates)
    day_start, day_end = _epg_day_bounds(selected_date, tzinfo)

    programs = await db.get_epg_programs(sid, cid, start_after=day_start, start_before=day_end)
    now_programs = await db.get_epg_programs(
        sid,
        cid,
        start_after=(datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(),
        start_before=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    )
    current = None
    next_prog = None
    for p in now_programs:
        if current is None and p['start'] <= now < p['stop']:
            total = (datetime.fromisoformat(p['stop']) - datetime.fromisoformat(p['start'])).total_seconds()
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(p['start'])).total_seconds()
            current = {
                'title': p['title'],
                'start': p['start'], 'stop': p['stop'],
                'progress': max(0, min(1, elapsed / total)) if total > 0 else 0,
                'remaining_minutes': max(0, int((datetime.fromisoformat(p['stop']) - datetime.now(timezone.utc)).total_seconds() / 60)),
            }
        elif p['start'] > now:
            if not next_prog:
                next_prog = {'title': p['title'], 'start': p['start'], 'stop': p['stop']}

    schedule = [{
        'title': p['title'], 'start': p['start'], 'stop': p['stop'],
        'status': 'current' if (p['start'] <= now < p['stop']) else ('past' if p['stop'] <= now else 'future'),
    } for p in programs]

    return {
        "canonical_key": canonical_key, "epg_source_id": sid, "epg_channel_id": cid,
        "match_status": effective_match_status,
        "current": current, "next": next_prog, "programs": schedule,
        "date": selected_date, "requested_date": requested_date, "tz": str(tzinfo),
        "available_dates": available_dates,
    }


@app.post("/api/iptv/epg/batch-current", dependencies=[Depends(require_browse_access)])
async def batch_current_programs(request: Request):
    body = await request.json()
    keys = body.get('canonical_keys', [])
    if not keys:
        return {}
    return await db.batch_get_current_programs(keys)


# ── 订阅导出 ──

IPTV_SUBSCRIPTION_MODES = {"hybrid", "direct", "proxy", "smart"}


def _csv_set(value: str = '') -> set[str]:
    return {item.strip() for item in (value or '').split(',') if item.strip()}


def _truthy_query(value: bool | int | str) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _m3u_attr(value: str = '') -> str:
    return str(value or '').replace('"', "'").replace('\n', ' ').strip()


def _source_type(source: dict) -> str:
    from m3u8_parser import detect_source_type

    declared = str(source.get('source_type') or '').strip().lower()
    if declared and declared != 'hls':
        return declared
    return detect_source_type(source.get('url', ''))


def _sorted_sources(sources: list[dict]) -> list[dict]:
    def _health_rank(source: dict) -> int:
        status = str(source.get("probe_status") or "").strip().lower()
        if source.get("is_working") == 1 or status == "online":
            return 0
        if status in {"", "untested", "unknown", "pending"} or source.get("is_working") in {-1, None}:
            return 1
        return 2

    return sorted(sources, key=lambda u: (
        _health_rank(u),
        u.get('latency_ms') or 9999,
        -(u.get('speed_mbps') or 0),
    ))


def _is_supported_export_source(source: dict, healthy_only: bool = True) -> bool:
    if not source.get('url'):
        return False
    if not _truthy_query(source.get('enabled', True)):
        return False
    if healthy_only and source.get('is_working') != 1:
        return False
    return _source_type(source) not in {'youtube', 'unsupported_youtube_url'}


_EXPORT_CREDENTIAL_QUERY_KEYS = frozenset({
    'token', 'access_token', 'auth', 'authorization', 'credential', 'password',
    'passwd', 'sig', 'signature', 'sign', 'hdnts', 'session', 'sessionid',
    'key', 'apikey', 'api_key', 'expires', 'exp',
})


def _source_has_export_credentials(source: dict) -> bool:
    """Detect obvious volatile/credential-bearing direct-export material."""
    for value in (source.get('url'), source.get('referer')):
        raw = str(value or '').strip()
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.username or parsed.password:
            return True
        if any(key.casefold() in _EXPORT_CREDENTIAL_QUERY_KEYS for key in parse_qs(parsed.query, keep_blank_values=True)):
            return True
    return False


def _source_direct_safe(source: dict) -> bool:
    """Return whether an external M3U client may receive the raw source URL."""
    source_type = _source_type(source)
    if source_type not in {"hls", "mpegts", "http_flv", "audio_http"}:
        return False
    if urlparse(str(source.get("url") or "")).scheme.lower() not in {"http", "https"}:
        return False
    if any(source.get(key) for key in (
        "force_proxy", "requires_headers", "requires_proxy_declared", "proxy_required_hint",
        "custom_ua", "referer", "adapter", "adapter_provider", "headers",
        "credential_refs", "volatile_url", "requires_proxy", "hidden_upstream",
    )):
        return False
    if source.get("direct_playable") is False:
        return False
    return not _source_has_export_credentials(source)

def _request_public_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")

def _absolute_api_url(request: Request, path: str) -> str:
    return f"{_request_public_base_url(request)}{path}"


def _iptv_proxy_path_for_channel(canonical_key: str) -> str:
    """用于 M3U 导出：按频道的 canonical_key 生成 media API 入口。

    不再把 raw upstream URL / Header 明文编入 query。
    导出的 .m3u 中每条频道地址都是：

        /api/media/channel/{canonical_key}/playlist.m3u8

    外部播放器通过 ``?access_token=wbm_...`` 传递 Media Credential。

    canonical_key 是频道聚合表的稳定身份，必须由调用方从聚合上下文显式传入。
    早期版本在缺失时按 source URL hash 造一个 ``src_<hash>`` 入口，那个入口在
    Core 里永远匹配不到频道（404），属于静默坏链，已删除：现在缺身份直接报错。
    """
    key = str(canonical_key or '').strip()
    if not key:
        raise HTTPException(status_code=500, detail="频道身份缺失")
    return f"/api/media/channel/{quote(key, safe='')}/playlist.m3u8"


def _iptv_proxy_url_for_channel(
    canonical_key: str, source: dict, request: Request, *, access: object | None = None
) -> str:
    """对外 URL + 可选 Media Credential 附加。

    ``canonical_key`` 决定频道入口，``source`` 只提供 source 身份（``source_id``）。
    ``access`` 应为 ``MediaAccessContext``（仅在确认为 credential 时透传 token）。
    """
    base = _absolute_api_url(request, _iptv_proxy_path_for_channel(canonical_key))
    source_id = str(source.get('source_id') or source_id_for(source))
    base += f"?source_id={quote(source_id, safe='')}"
    token = getattr(access, 'propagated_access_token', None) or ""
    if not token:
        return base
    return f"{base}&access_token={quote(token, safe='')}"


def _iptv_smart_url_for_channel(channel: dict, request: Request, *, access: object | None = None) -> str:
    path = f'/api/iptv/smart/{quote(str(channel["canonical_key"]), safe="")}.m3u8'
    base = _absolute_api_url(request, path)
    token = getattr(access, 'propagated_access_token', None) or ""
    return f"{base}?access_token={quote(token, safe='')}" if token else base


def _m3u_attrs_for_channel(channel: dict, include_epg: bool, include_logo: bool) -> str:
    attrs = []
    tvg_name = channel.get('tvg_name') or channel.get('name') or ''
    if tvg_name:
        attrs.append(f'tvg-name="{_m3u_attr(tvg_name)}"')
    export_tvg = channel.get('epg_channel_id') or channel.get('tvg_id') or channel.get('canonical_key', '')
    if include_epg and export_tvg:
        attrs.append(f'tvg-id="{_m3u_attr(export_tvg)}"')
    if include_logo and channel.get('logo_url'):
        attrs.append(f'tvg-logo="{_m3u_attr(channel["logo_url"])}"')
    if channel.get('group_name'):
        attrs.append(f'group-title="{_m3u_attr(channel["group_name"])}"')
    return ' '.join(attrs)


def _format_m3u_options(source: dict | None) -> list[str]:
    """为单条原始 url 生成 EXTVLCOPT/KODIPROP/WAVEFLOW 行。

    Referer / Custom UA 双写：EXTVLCOPT 给 VLC，KODIPROP:stream_headers 给 Kodi。
    KODIPROP 值要 url-encode，EXTVLCOPT 是裸值。
    """
    if not source:
        return []
    ref = str(source.get('referer') or '').strip()
    ua = str(source.get('custom_ua') or '').strip()
    force_proxy = bool(source.get('force_proxy'))
    out: list[str] = []
    if ref:
        out.append(f'#EXTVLCOPT:http-referrer={ref}')
    if ua:
        out.append(f'#EXTVLCOPT:http-user-agent={ua}')
    # KODIPROP stream_headers 把所有 header 拼成 url-encoded form
    if ref or ua:
        kodi_parts = []
        if ref:
            kodi_parts.append(f'Referer={quote(ref, safe="")}')
        if ua:
            kodi_parts.append(f'User-Agent={quote(ua, safe="")}')
        out.append(f'#KODIPROP:inputstream.adaptive.stream_headers={"&".join(kodi_parts)}')
    if force_proxy:
        out.append('#WAVEFLOW:requires_proxy=1')
    return out


def _subscription_urls_for_channel(
    channel: dict, mode: str, request: Request, healthy_only: bool, *, access: object = None
) -> list[tuple[str, dict | None]]:
    """返回 [(url, source_for_options), ...]。

    source_for_options 不为 None 时，表示这条 url 是裸的原始流地址，
    导出 m3u 时需要附加 EXTVLCOPT/KODIPROP/WAVEFLOW（让 VLC/Kodi 直连也能播）。
    为 None 时，表示这条 url 已经是 proxy/adapter/smart 包装地址，
    所有 header / proxy 语义已经编码到 url 内部，不需要也不能再附加注解。

    RTSP 在所有模式下都是 Core-only：``_source_direct_safe`` 不接受 ``rtsp``，
    因此 RTSP 源只会走 Core 频道入口，不存在把它直出到外部 M3U 的导出开关。
    """
    sources = _sorted_sources([
        source for source in channel.get('urls', [])
        if _is_supported_export_source(source, healthy_only=healthy_only)
    ])
    canonical_key = str(channel.get('canonical_key') or '')

    if mode == 'smart':
        if sources:
            return [(_iptv_smart_url_for_channel(channel, request, access=access), None)]
        return []

    if mode == 'direct':
        return [(source['url'], None) for source in sources if _source_direct_safe(source)]

    if mode == 'proxy':
        return [
            (_iptv_proxy_url_for_channel(canonical_key, source, request, access=access), None)
            for source in sources
        ]

    return [
        (source['url'], None) if _source_direct_safe(source)
        else (_iptv_proxy_url_for_channel(canonical_key, source, request, access=access), None)
        for source in sources
    ]


@app.get("/api/iptv/subscription.m3u")
async def export_iptv_subscription(
    request: Request,
    mode: str = 'smart',
    healthy_only: bool = True,
    include_epg: bool = True,
    include_logo: bool = True,
    groups: str = '',
    access: object = Depends(resolve_media_access),
):
    mode = (mode or 'smart').strip().lower()
    if mode not in IPTV_SUBSCRIPTION_MODES:
        raise HTTPException(status_code=400, detail="无效导出模式")

    channels, _groups = await _get_aggregated_iptv_channels()
    selected_groups = _csv_set(groups)
    if selected_groups:
        channels = [ch for ch in channels if ch.get('group_name') in selected_groups]

    healthy = _truthy_query(healthy_only)
    epg = _truthy_query(include_epg)
    logo = _truthy_query(include_logo)

    lines = ["#EXTM3U"]
    exported = 0
    for channel in channels:
        urls = _subscription_urls_for_channel(channel, mode, request, healthy, access=access)
        if not urls:
            continue
        attrs = _m3u_attrs_for_channel(channel, include_epg=epg, include_logo=logo)
        for url, source_opts in urls:
            lines.append(f'#EXTINF:-1 {attrs},{channel["name"]}')
            lines.extend(_format_m3u_options(source_opts))
            lines.append(url)
            exported += 1

    if exported == 0:
        raise HTTPException(status_code=404, detail="无可导出的频道")

    content = '\n'.join(lines)
    return Response(
        content=content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="waveflow_{mode}.m3u"'},
    )


@app.get("/api/iptv/smart/{canonical_key}.m3u8")
async def iptv_smart_playlist(
    canonical_key: str,
    request: Request,
    access=Depends(resolve_media_access),
):
    """Smart 频道级投递入口（Subscription Export V1 的稳定契约）。

    只做两件事：按 ``canonical_key`` 选一个当前 source，然后 307 重定向。
    直连安全（``_source_direct_safe``）的源直接重定向到上游 URL；其余全部交给
    canonical Core 频道入口 ``/api/media/channel/{canonical_key}/playlist.m3u8``，
    并带上显式 ``source_id``。

    Smart 不拥有播放实现：不建 RTSP session、不做 Plugin/Adapter resolve、
    不签 handle、不做 URL fallback，也不复制 Core 的 source selection。
    RTSP 只是 Core 内部的一种 transport，因此没有第二条 Smart RTSP 路由。
    """
    channels, _groups = await _get_aggregated_iptv_channels()
    channel = next((ch for ch in channels if ch.get('canonical_key') == canonical_key), None)
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")

    # 候选集与导出侧共用同一个 eligibility 谓词；健康只影响顺序
    # （``_sorted_sources`` 把在线源排前面），不作为硬门槛——否则频道级入口会比
    # 它委派的 Core 频道入口更严格，在探针误报时给出假的 503。
    sources = _sorted_sources([
        source for source in channel.get('urls', [])
        if _is_supported_export_source(source, healthy_only=False)
    ])
    for source in sources:
        if _source_direct_safe(source):
            try:
                await assert_safe_target_url(source['url'], allowed_schemes={"http", "https"})
            except UnsafeTargetError:
                continue
            return RedirectResponse(source['url'], status_code=307)
        # Proxy fallback is deliberately opaque to the external client.
        return RedirectResponse(
            _iptv_proxy_url_for_channel(canonical_key, source, request, access=access),
            status_code=307,
        )

    raise HTTPException(status_code=503, detail="没有可用播放源")


# ── 工具函数 ──

def _guess_sub_title(url: str, channels: list[dict]) -> str:
    """从 URL 或频道分组推测订阅源标题"""
    # 尝试从分组名推断
    groups = set(ch.get('group_name', '') for ch in channels if ch.get('group_name'))
    if groups:
        return ' / '.join(sorted(groups)[:3])

    # 从 URL 文件名推断
    parsed = urlparse(url)
    path = parsed.path.rstrip('/')
    if path:
        name = path.split('/')[-1]
        name = name.replace('.m3u', '').replace('.m3u8', '').replace('.txt', '')
        if name:
            return name

    return parsed.netloc or '未知订阅源'
