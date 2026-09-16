import asyncio
import json
import os
import re
import time
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from adapters import AdapterResolveError
from infrastructure.http_client import (
    RedirectTargetRejected,
    request_with_safe_redirects,
    stream_with_safe_redirects,
)
from plugin_runtime import PluginError
from m3u8_parser import adapter_provider, detect_source_type, is_youtube_url
from media_tools import media_tool_bin
from security.redact import redact_url
from ssrf_guard import UnsafeTargetError, assert_safe_target_url


STREAM_READ_BYTES = 1024 * 1024
STREAM_READ_SECONDS = 3.0
PLAYLIST_TIMEOUT = 8.0
SEGMENT_TIMEOUT = 5.0
STREAM_TIMEOUT = 8.0
HLS_SEGMENT_SAMPLE_LIMIT = 3
PROBE_EXTERNAL_SCHEMES = {"http", "https", "rtmp", "rtsp"}


def _youtube_adapter_url(url: str) -> str:
    return f"youtube://resolve?probe=1&url={quote(url, safe='')}"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


FFPROBE_TIMEOUT = _env_float("IPTV_FFPROBE_TIMEOUT", 6.0)
FFPROBE_PROBESIZE = 1024 * 1024
FFPROBE_ANALYZE_DURATION_US = 3_000_000
FFPROBE_CONCURRENCY = _env_int("IPTV_FFPROBE_CONCURRENCY", 2)
FFMPEG_TIMEOUT = _env_float("IPTV_FFMPEG_TIMEOUT", 7.0)
RT_FFMPEG_TIMEOUT = _env_float("IPTV_RT_FFMPEG_TIMEOUT", 5.0)
FFMPEG_SAMPLE_SECONDS = 5.0
FFMPEG_CONCURRENCY = _env_int("IPTV_FFMPEG_CONCURRENCY", 4)

_FFPROBE_SEMAPHORE = asyncio.Semaphore(FFPROBE_CONCURRENCY)
_FFMPEG_SEMAPHORE = asyncio.Semaphore(FFMPEG_CONCURRENCY)


NOT_LIVE_ERROR_CODES = {
    "douyu_not_live",
    "huya_not_live",
    "redbook_not_live",
    "bilibili_not_live",
    "changliao_not_live",
    "picarto_not_live",
    "showroom_not_live",
    "maoer_not_live",
    "sixroom_not_live",
    "blued_not_live",
}


def is_adapter_not_live_error(error_code: str | None) -> bool:
    value = (error_code or "").strip().lower()
    return value in NOT_LIVE_ERROR_CODES or value.endswith("_not_live")


def _empty_result(**overrides) -> dict[str, Any]:
    result = {
        "probe_status": "error",
        "live_status": "unknown",
        "probe_method": "",
        "latency_ms": 0,
        "speed_mbps": 0,
        "resolution": "",
        "fps": 0,
        "video_codec": "",
        "audio_codec": "",
        "requires_headers": False,
        "requires_proxy_declared": False,
        "proxy_required_hint": False,
        "last_error": "",
        "adapter_provider": "",
        "adapter_title": "",
        "probe_meta_json": "{}",
    }
    result.update(overrides)
    return result


def _safe_meta(meta: dict[str, Any]) -> str:
    try:
        return json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _merge_meta(result: dict[str, Any], meta: dict[str, Any]) -> None:
    existing_meta = {}
    try:
        existing_meta = json.loads(result.get("probe_meta_json") or "{}")
    except Exception:
        existing_meta = {}
    result["probe_meta_json"] = _safe_meta({**existing_meta, **meta})


def _headers_from_channel(ch: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    custom_ua = str(ch.get("custom_ua") or "").strip()
    referer = str(ch.get("referer") or "").strip()
    if custom_ua:
        headers["User-Agent"] = custom_ua
    if referer:
        headers["Referer"] = referer
    return headers


def _descriptor_probe_metadata(resolved: dict[str, Any]) -> dict[str, Any]:
    """Return descriptor metadata as probe observations, never controls."""
    return {
        field: resolved[field]
        for field in ("provider_diagnostics", "probe_hints")
        if isinstance(resolved.get(field), dict)
    }


def _ffmpeg_input_timeout_args(url: str, timeout_seconds: float) -> list[str]:
    timeout_us = str(int(timeout_seconds * 1_000_000))
    if (url or "").lower().startswith("rtsp://"):
        return ["-timeout", timeout_us]
    return ["-rw_timeout", timeout_us]


def _is_realtime_media_url(url: str) -> bool:
    return (url or "").lower().startswith(("rtsp://", "rtmp://"))


def _ffmpeg_probe_timeout(url: str) -> float:
    if _is_realtime_media_url(url):
        return RT_FFMPEG_TIMEOUT
    return FFMPEG_TIMEOUT


def _safe_ffmpeg_reason(reason: Any) -> str:
    value = str(reason or "").strip()
    if value in {
        "rt_stream",
        "http_timeout",
        "http_exception",
        "http_segment",
        "http_stream",
        "weak_http_probe",
    }:
        return value
    if re.fullmatch(r"(?:http|variant_http)_status_[0-9]{3}", value):
        return value
    return "probe_fallback"


def _requires_headers(headers: dict[str, str]) -> bool:
    return any(str(v or "").strip() for v in headers.values())


def _security_rejected_result(probe_method: str) -> dict[str, Any]:
    """Keep a policy rejection distinct from an upstream reachability error."""
    return _empty_result(
        probe_status="error",
        live_status="error",
        probe_method=probe_method,
        last_error="ssrf_policy_rejected",
        probe_meta_json=_safe_meta({
            "probe_quality": "blocked",
            "security_rejection": "ssrf_policy",
        }),
    )


async def _validate_external_probe_target(url: str) -> None:
    """Validate a URL before handing it to an external media process."""
    await assert_safe_target_url(url, allowed_schemes=PROBE_EXTERNAL_SCHEMES)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}


def _is_hls_url(url: str, content_type: str = "") -> bool:
    lower = (url or "").lower()
    ctype = (content_type or "").lower()
    return (
        ".m3u8" in lower
        or "mpegurl" in ctype
        or "m3u8" in ctype
        or "x-mpegurl" in ctype
    )


def _is_special_uri(uri: str) -> bool:
    lower = (uri or "").strip().lower()
    return lower.startswith(("data:", "skd:", "urn:"))


def _parse_stream_inf_bandwidth(line: str) -> int:
    for part in line.split(":")[-1].split(","):
        key, _, value = part.partition("=")
        if key.strip().upper() == "BANDWIDTH":
            try:
                return int(value.strip())
            except ValueError:
                return 0
    return 0


def _pick_variant_url(playlist_text: str, base_url: str) -> str | None:
    variants: list[tuple[int, str]] = []
    pending_bandwidth: int | None = None
    for raw in playlist_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXT-X-STREAM-INF"):
            pending_bandwidth = _parse_stream_inf_bandwidth(line)
            continue
        if pending_bandwidth is not None:
            if not line.startswith("#") and not _is_special_uri(line):
                variants.append((pending_bandwidth, urljoin(base_url, line)))
            pending_bandwidth = None

    if not variants:
        return None
    variants.sort(key=lambda item: item[0])
    return variants[len(variants) // 2][1]


def _pick_recent_segment_urls(playlist_text: str, base_url: str, limit: int = HLS_SEGMENT_SAMPLE_LIMIT) -> list[str]:
    urls: list[str] = []
    for raw in playlist_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _is_special_uri(line):
            continue
        urls.append(urljoin(base_url, line))
    return urls[-limit:]


def _parse_fps(value: str | None) -> float:
    raw = str(value or "").strip()
    if not raw or raw == "0/0":
        return 0
    if "/" in raw:
        numerator, _, denominator = raw.partition("/")
        try:
            den = float(denominator)
            return round(float(numerator) / den, 3) if den else 0
        except ValueError:
            return 0
    try:
        return round(float(raw), 3)
    except ValueError:
        return 0


def _parse_size_bytes(value: str, unit: str | None) -> float:
    try:
        amount = float(value)
    except Exception:
        return 0
    normalized = (unit or "").strip().lower()
    if normalized in {"kib", "k", "kb"}:
        return amount * 1024
    if normalized in {"mib", "m", "mb"}:
        return amount * 1024 * 1024
    return amount


def _parse_time_seconds(value: str) -> float:
    try:
        parts = [float(part) for part in str(value).split(":")]
    except Exception:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0


def _parse_ffmpeg_output(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    video_line = re.search(r"(?:^|\n)\s*Stream #[^\n]*Video:\s*([^\n\r]+)", text)
    if video_line:
        line = video_line.group(1)
        info["video_codec"] = line.split(",", 1)[0].strip().split()[0]
        resolution_match = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if resolution_match:
            info["resolution"] = f"{resolution_match.group(1)}x{resolution_match.group(2)}"
        fps_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:fps|tbr|tbn)", line, re.I)
        if fps_match:
            try:
                info["fps"] = round(float(fps_match.group(1)), 3)
            except Exception:
                pass
    audio_line = re.search(r"(?:^|\n)\s*Stream #[^\n]*Audio:\s*([^\n\r]+)", text)
    if audio_line:
        info["audio_codec"] = audio_line.group(1).split(",", 1)[0].strip().split()[0]

    total_bytes = 0.0
    for key in ("video", "audio", "Lsize", "size"):
        match = re.search(rf"{key}=\s*([0-9]+(?:\.[0-9]+)?)\s*(KiB|MiB|kB|KB|B|kb)?", text, re.I)
        if not match and key in {"video", "audio"}:
            match = re.search(rf"{key}:\s*([0-9]+(?:\.[0-9]+)?)\s*(KiB|MiB|kB|KB|B|kb)?", text, re.I)
        if match:
            total_bytes += _parse_size_bytes(match.group(1), match.group(2))
    time_match = re.search(r"time=\s*([0-9:.]+)", text)
    seconds = _parse_time_seconds(time_match.group(1)) if time_match else 0
    if total_bytes > 0 and seconds > 0:
        info["speed_mbps"] = round(total_bytes / seconds / 1024 / 1024, 3)

    bitrate_match = re.search(r"bitrate\s*[:=]\s*([0-9.]+)\s*k?bits/s", text, re.I)
    if not bitrate_match:
        bitrate_match = re.search(r"(?:^|[,\s])([0-9.]+)\s*kb/s(?:[,\s]|$)", text, re.I)
    if "speed_mbps" not in info and bitrate_match:
        try:
            info["speed_mbps"] = round(float(bitrate_match.group(1)) / 8 / 1024, 3)
        except Exception:
            pass
    return info


def _ffprobe_header_string(headers: dict[str, str]) -> str:
    lines = []
    for key, value in headers.items():
        if not value or key.lower() == "user-agent":
            continue
        safe_key = str(key).replace("\r", "").replace("\n", "").strip()
        safe_value = str(value).replace("\r", "").replace("\n", "").strip()
        if safe_key and safe_value:
            lines.append(f"{safe_key}: {safe_value}\r\n")
    return "".join(lines)


async def _kill_process(proc: asyncio.subprocess.Process | None) -> None:
    if not proc or proc.returncode is not None:
        return
    proc.kill()
    try:
        await proc.wait()
    except Exception:
        pass


async def _probe_media_info(url: str, headers: dict[str, str]) -> dict[str, Any]:
    try:
        await _validate_external_probe_target(url)
    except UnsafeTargetError:
        return {"meta": {
            "ffprobe": False,
            "ffprobe_error": "ssrf_policy_rejected",
            "security_rejection": "ssrf_policy",
        }}

    ffprobe_bin = media_tool_bin("ffprobe")
    if not ffprobe_bin:
        return {"meta": {"ffprobe": False, "ffprobe_error": "ffprobe_not_found"}}

    args = [
        ffprobe_bin,
        "-v", "error",
        *_ffmpeg_input_timeout_args(url, FFPROBE_TIMEOUT),
        "-analyzeduration", str(FFPROBE_ANALYZE_DURATION_US),
        "-probesize", str(FFPROBE_PROBESIZE),
    ]
    user_agent = headers.get("User-Agent") or headers.get("user-agent")
    if user_agent:
        args.extend(["-user_agent", str(user_agent)])
    header_string = _ffprobe_header_string(headers)
    if header_string:
        args.extend(["-headers", header_string])
    args.extend(["-show_streams", "-of", "json", url])

    proc: asyncio.subprocess.Process | None = None
    async with _FFPROBE_SEMAPHORE:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFPROBE_TIMEOUT + 1)
        except asyncio.TimeoutError:
            await _kill_process(proc)
            return {"meta": {"ffprobe": False, "ffprobe_error": "ffprobe_timeout"}}
        except asyncio.CancelledError:
            await _kill_process(proc)
            raise
        except Exception:
            error_code = "ffprobe_spawn_failed" if proc is None else "ffprobe_failed"
            return {"meta": {"ffprobe": False, "ffprobe_error": error_code}}

    if proc.returncode != 0:
        return {
            "meta": {
                "ffprobe": False,
                "ffprobe_error": "ffprobe_failed",
                "ffprobe_exit_code": proc.returncode,
            }
        }

    try:
        data = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
    except Exception:
        return {"meta": {"ffprobe": False, "ffprobe_error": "invalid_json"}}

    streams = data.get("streams") if isinstance(data, dict) else []
    if not isinstance(streams, list):
        streams = []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info: dict[str, Any] = {"meta": {"ffprobe": True, "stream_count": len(streams)}}
    if video:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        if width and height:
            info["resolution"] = f"{width}x{height}"
        fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        if fps:
            info["fps"] = fps
        if video.get("codec_name"):
            info["video_codec"] = str(video.get("codec_name"))
    if audio and audio.get("codec_name"):
        info["audio_codec"] = str(audio.get("codec_name"))
    return info


async def _enrich_with_ffprobe(result: dict[str, Any], url: str, headers: dict[str, str]) -> dict[str, Any]:
    if result.get("probe_status") != "online":
        return result
    if result.get("probe_method") == "ffmpeg":
        return result
    safe_final_url = str(_meta_value(result, "safe_final_url", "") or url)
    info = await _probe_media_info(safe_final_url, headers)
    for key in ("resolution", "fps", "video_codec", "audio_codec"):
        if info.get(key):
            result[key] = info[key]
    ffprobe_meta = info.get("meta") or {}
    quality = "strong" if ffprobe_meta.get("ffprobe") and int(ffprobe_meta.get("stream_count") or 0) > 0 else "http_only"
    _merge_meta(result, {**ffprobe_meta, "probe_quality": quality})
    return result


def _meta_value(result: dict[str, Any], key: str, default: Any = None) -> Any:
    try:
        meta = json.loads(result.get("probe_meta_json") or "{}")
    except Exception:
        meta = {}
    return meta.get(key, default)


def _sanitize_probe_meta_json(value: str) -> str:
    try:
        meta = json.loads(value or "{}")
    except Exception:
        return "{}"
    if not isinstance(meta, dict):
        return "{}"
    safe_final_url = meta.get("safe_final_url")
    if isinstance(safe_final_url, str) and safe_final_url:
        meta["safe_final_url"] = redact_url(safe_final_url)
    return _safe_meta(meta)


async def _probe_with_ffmpeg(url: str, headers: dict[str, str], *, reason: str) -> dict[str, Any]:
    reason = _safe_ffmpeg_reason(reason)
    try:
        await _validate_external_probe_target(url)
    except UnsafeTargetError:
        return _security_rejected_result("ffmpeg")

    ffmpeg_bin = media_tool_bin("ffmpeg")
    if not ffmpeg_bin:
        return _empty_result(
            probe_status="unsupported",
            live_status="unknown",
            probe_method="ffmpeg",
            last_error="ffmpeg_not_found",
            probe_meta_json=_safe_meta({"probe_quality": "unsupported", "ffmpeg": False, "ffmpeg_reason": reason}),
        )

    probe_timeout = _ffmpeg_probe_timeout(url)
    args = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        *_ffmpeg_input_timeout_args(url, probe_timeout),
        "-t",
        str(FFMPEG_SAMPLE_SECONDS),
    ]
    if url.lower().startswith("rtsp://"):
        args.extend(["-rtsp_transport", "tcp"])
    user_agent = headers.get("User-Agent") or headers.get("user-agent")
    if user_agent:
        args.extend(["-user_agent", str(user_agent)])
    header_string = _ffprobe_header_string(headers)
    if header_string:
        args.extend(["-headers", header_string])
    args.extend(["-i", url, "-f", "null", "-"])

    started = time.monotonic()
    proc: asyncio.subprocess.Process | None = None
    stderr_parts: list[bytes] = []
    stdout_parts: list[bytes] = []
    async with _FFMPEG_SEMAPHORE:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= probe_timeout:
                    await _kill_process(proc)
                    break
                if proc.returncode is not None:
                    break
                try:
                    line = await asyncio.wait_for(proc.stderr.readline(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue
                if line:
                    stderr_parts.append(line)
                    parsed_so_far = _parse_ffmpeg_output(b"\n".join(stderr_parts).decode("utf-8", errors="ignore"))
                    if parsed_so_far.get("video_codec") or parsed_so_far.get("audio_codec") or parsed_so_far.get("resolution"):
                        # Enough metadata for validation; no need to decode more.
                        await _kill_process(proc)
                        break
                elif proc.returncode is not None:
                    break
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1)
                if stdout:
                    stdout_parts.append(stdout)
                if stderr:
                    stderr_parts.append(stderr)
            except asyncio.TimeoutError:
                await _kill_process(proc)
        except asyncio.CancelledError:
            await _kill_process(proc)
            raise
        except Exception:
            error_code = "ffmpeg_spawn_failed" if proc is None else "ffmpeg_failed"
            return _empty_result(
                probe_status="error",
                live_status="error",
                probe_method="ffmpeg",
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                last_error=error_code,
                probe_meta_json=_safe_meta({
                    "probe_quality": "failed",
                    "ffmpeg": False,
                    "ffmpeg_reason": reason,
                    "ffmpeg_error": error_code,
                }),
            )

    output = b"\n".join(stderr_parts + stdout_parts).decode("utf-8", errors="ignore")
    parsed = _parse_ffmpeg_output(output)
    stream_seen = bool(parsed.get("video_codec") or parsed.get("audio_codec") or "Stream #0:" in output or "Input #0," in output)
    if stream_seen:
        return _empty_result(
            probe_status="online",
            live_status="live",
            probe_method="ffmpeg",
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            speed_mbps=float(parsed.get("speed_mbps") or 0),
            resolution=str(parsed.get("resolution") or ""),
            fps=float(parsed.get("fps") or 0),
            video_codec=str(parsed.get("video_codec") or ""),
            audio_codec=str(parsed.get("audio_codec") or ""),
            probe_meta_json=_safe_meta({
                "probe_quality": "deep",
                "ffmpeg": True,
                "ffmpeg_reason": reason,
                "ffmpeg_exit_code": proc.returncode if proc else None,
            }),
        )

    return _empty_result(
        probe_status="offline",
        live_status="unknown",
        probe_method="ffmpeg",
        latency_ms=round((time.monotonic() - started) * 1000, 1),
        last_error="ffmpeg_no_stream",
        probe_meta_json=_safe_meta({
            "probe_quality": "failed",
            "ffmpeg": False,
            "ffmpeg_reason": reason,
            "ffmpeg_exit_code": proc.returncode if proc else None,
            "ffmpeg_error": "ffmpeg_no_stream",
        }),
    )


async def _read_stream_sample(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    timeout: float,
    max_bytes: int = STREAM_READ_BYTES,
    max_seconds: float = STREAM_READ_SECONDS,
) -> dict[str, float | int | bool | str]:
    started = time.monotonic()
    first_byte_at: float | None = None
    total = 0
    response = await stream_with_safe_redirects(
        client,
        "GET",
        url,
        headers=headers,
        timeout=timeout,
    )
    try:
        safe_final_url = str(response.url)
        if response.status_code >= 400:
            return {
                "ok": False,
                "latency_ms": 0,
                "speed_mbps": 0,
                "bytes": 0,
                "safe_final_url": safe_final_url,
            }
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            now = time.monotonic()
            if first_byte_at is None:
                first_byte_at = now
            total += len(chunk)
            if total >= max_bytes or now - started >= max_seconds:
                break
    finally:
        await response.aclose()

    elapsed = max(time.monotonic() - started, 0.001)
    latency_ms = ((first_byte_at or time.monotonic()) - started) * 1000
    return {
        "ok": total > 0,
        "latency_ms": round(latency_ms, 1),
        "speed_mbps": round(total / elapsed / 1024 / 1024, 3),
        "bytes": total,
        "safe_final_url": safe_final_url,
    }


async def _probe_hls(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> dict[str, Any]:
    started = time.monotonic()
    response = await request_with_safe_redirects(
        client,
        "GET",
        url,
        headers=headers,
        timeout=PLAYLIST_TIMEOUT,
    )
    try:
        latency_ms = (time.monotonic() - started) * 1000
        playlist_url = str(response.url)
        playlist_status = response.status_code
        playlist_text = response.text
    finally:
        await response.aclose()

    if playlist_status >= 400:
        return _empty_result(
            probe_status="offline",
            probe_method="http_segment",
            latency_ms=round(latency_ms, 1),
            last_error=f"http_status_{playlist_status}",
            probe_meta_json=_safe_meta({"safe_final_url": playlist_url}),
        )

    variant_url = _pick_variant_url(playlist_text, playlist_url)
    if variant_url:
        variant_response = await request_with_safe_redirects(
            client,
            "GET",
            variant_url,
            headers=headers,
            timeout=PLAYLIST_TIMEOUT,
        )
        try:
            variant_status = variant_response.status_code
            variant_text = variant_response.text
            variant_final_url = str(variant_response.url)
        finally:
            await variant_response.aclose()
        if variant_status < 400:
            playlist_text = variant_text
            playlist_url = variant_final_url
        else:
            return _empty_result(
                probe_status="offline",
                probe_method="http_segment",
                latency_ms=round(latency_ms, 1),
                last_error=f"variant_http_status_{variant_status}",
                probe_meta_json=_safe_meta({"variant": True, "safe_final_url": variant_final_url}),
            )

    segment_urls = _pick_recent_segment_urls(playlist_text, playlist_url)
    if not segment_urls:
        return _empty_result(
            probe_status="online",
            probe_method="http_segment",
            latency_ms=round(latency_ms, 1),
            probe_meta_json=_safe_meta({
                "hls_no_segments": True,
                "variant": bool(variant_url),
                "safe_final_url": playlist_url,
            }),
        )

    samples = []
    for segment_url in segment_urls:
        try:
            samples.append(await _read_stream_sample(client, segment_url, headers, timeout=SEGMENT_TIMEOUT, max_bytes=384 * 1024))
        except (RedirectTargetRejected, UnsafeTargetError):
            # A child resource is part of the probe target. Do not downgrade a
            # blocked segment to an ordinary network failure or try fallback
            # subprocess probing for the same unsafe source.
            raise
        except Exception:
            continue
    ok_samples = [item for item in samples if item.get("ok")]
    if not ok_samples:
        return _empty_result(
            probe_status="offline",
            probe_method="http_segment",
            latency_ms=round(latency_ms, 1),
            last_error="segment_unreachable",
            probe_meta_json=_safe_meta({
                "variant": bool(variant_url),
                "segment_count": len(segment_urls),
                "safe_final_url": playlist_url,
            }),
        )

    total_bytes = sum(int(item.get("bytes") or 0) for item in ok_samples)
    speed_values = [float(item.get("speed_mbps") or 0) for item in ok_samples if item.get("speed_mbps")]
    return _empty_result(
        probe_status="online",
        live_status="live",
        probe_method="http_segment",
        latency_ms=round(latency_ms + min(float(item.get("latency_ms") or 0) for item in ok_samples), 1),
        speed_mbps=round(sum(speed_values) / len(speed_values), 3) if speed_values else 0,
        probe_meta_json=_safe_meta({
            "variant": bool(variant_url),
            "segments_tested": len(ok_samples),
            "bytes": total_bytes,
            "safe_final_url": playlist_url,
        }),
    )


async def _probe_http_stream(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> dict[str, Any]:
    sample = await _read_stream_sample(client, url, headers, timeout=STREAM_TIMEOUT)
    if not sample.get("ok"):
        return _empty_result(
            probe_status="offline",
            probe_method="http_stream",
            last_error="stream_unreachable",
            probe_meta_json=_safe_meta({
                "safe_final_url": str(sample.get("safe_final_url") or url),
            }),
        )
    return _empty_result(
        probe_status="online",
        live_status="live",
        probe_method="http_stream",
        latency_ms=float(sample.get("latency_ms") or 0),
        speed_mbps=float(sample.get("speed_mbps") or 0),
        probe_meta_json=_safe_meta({
            "bytes": int(sample.get("bytes") or 0),
            "safe_final_url": str(sample.get("safe_final_url") or url),
        }),
    )


async def probe_channel_source(ch: dict[str, Any], client: httpx.AsyncClient, *, provider_resolver=None) -> dict[str, Any]:
    original_url = str(ch.get("url") or "").strip()
    headers = _headers_from_channel(ch)
    declared_proxy = _truthy(ch.get("force_proxy"))
    adapter = adapter_provider(original_url)
    adapter_title = ""
    resolved_youtube_video_id = ""
    resolved_youtube_page_is_live = False
    meta: dict[str, Any] = {}

    if not original_url:
        return _empty_result(
            probe_status="error",
            probe_method="skip",
            last_error="empty_url",
        )

    url = original_url
    stored_source_type = str(ch.get("source_type") or "").strip().lower()
    source_type = stored_source_type if stored_source_type and stored_source_type != "hls" else detect_source_type(url)
    if source_type == "youtube" or (source_type == "unsupported_youtube_url" and is_youtube_url(original_url)):
        source_type = "adapter"
        adapter = "youtube"
        original_url = _youtube_adapter_url(original_url)

    if source_type == "adapter":
        try:
            if provider_resolver is None:
                raise PluginError("PLUGIN_UNAVAILABLE", "Provider resolver is unavailable", category="lifecycle")
            resolved = await provider_resolver.resolve(original_url, client)
            url = str(resolved.get("url") or "")
            source_type = str(resolved.get("source_type") or detect_source_type(url)).strip().lower()
            resolved_headers = resolved.get("headers") if isinstance(resolved.get("headers"), dict) else {}
            headers.update({str(k): str(v) for k, v in resolved_headers.items() if v is not None})
            adapter = str(resolved.get("adapter") or adapter)
            adapter_title = str(resolved.get("title") or resolved.get("anchor_name") or "")
            resolved_youtube_video_id = str(resolved.get("youtube_video_id") or "").strip()
            resolved_youtube_page_is_live = bool(resolved.get("youtube_page_is_live"))
            descriptor_metadata = _descriptor_probe_metadata(resolved)
            if source_type == "probe_only":
                return _empty_result(
                    probe_status="online",
                    live_status="live",
                    probe_method="adapter_probe_only",
                    requires_headers=_requires_headers(headers),
                    requires_proxy_declared=declared_proxy,
                    proxy_required_hint=True,
                    adapter_provider=adapter,
                    adapter_title=adapter_title,
                    youtube_video_id=resolved_youtube_video_id,
                    probe_meta_json=_safe_meta({
                        "adapter": adapter,
                        "resolved_type": source_type,
                        "probe_quality": "metadata_only",
                        "youtube_page_is_live": True,
                        "source_type": source_type,
                        **descriptor_metadata,
                    }),
                )
            meta.update({
                "adapter": adapter,
                "resolved_type": source_type,
                "ttl": resolved.get("ttl"),
                "volatile_url": bool(resolved.get("volatile_url")),
                "cacheable": resolved.get("cacheable"),
                "warnings": resolved.get("warnings") or [],
                **descriptor_metadata,
            })
        except AdapterResolveError as exc:
            status = "not_live" if is_adapter_not_live_error(exc.error_code) else "error"
            cause = exc.__cause__ or exc
            err_vid = str(getattr(cause, "youtube_video_id", "") or getattr(exc, "youtube_video_id", "") or "").strip()
            # 页面说在播 → 不管 adapter 报什么，一律当 error（可重试）
            if getattr(cause, "youtube_page_is_live", False) or getattr(exc, "youtube_page_is_live", False):
                status = "error"
            result = _empty_result(
                probe_status=status,
                live_status="not_live" if status == "not_live" else "error",
                probe_method="adapter_resolve",
                last_error=exc.message,
                adapter_provider=adapter,
                proxy_required_hint=True,
                requires_headers=_requires_headers(headers),
                requires_proxy_declared=declared_proxy,
                probe_meta_json=_safe_meta({"error_code": exc.error_code, "retryable": exc.retryable}),
            )
            if err_vid:
                result["youtube_video_id"] = err_vid
            return result
        except PluginError as exc:
            status = "not_live" if exc.code == "NOT_LIVE" else "error"
            return _empty_result(
                probe_status=status,
                live_status="not_live" if status == "not_live" else "error",
                probe_method="adapter_resolve",
                last_error=exc.message,
                adapter_provider=adapter,
                proxy_required_hint=True,
                requires_headers=_requires_headers(headers),
                requires_proxy_declared=declared_proxy,
                probe_meta_json=_safe_meta({"error_code": exc.code, "retryable": exc.retryable}),
            )

    if not url:
        return _empty_result(
            probe_status="error",
            live_status="error",
            probe_method="adapter_resolve" if adapter else "skip",
            last_error="resolved_empty_url",
            adapter_provider=adapter,
            probe_meta_json=_safe_meta(meta),
        )

    lower_url = url.lower()
    is_rt_stream = _is_realtime_media_url(lower_url)
    try:
        if is_rt_stream:
            result = await _probe_with_ffmpeg(url, headers, reason="rt_stream")
        elif source_type == "hls" or _is_hls_url(url):
            result = await _probe_hls(client, url, headers)
        else:
            result = await _probe_http_stream(client, url, headers)
        result = await _enrich_with_ffprobe(result, url, headers)
        ffprobe_meta = {}
        try:
            ffprobe_meta = json.loads(result.get("probe_meta_json") or "{}")
        except Exception:
            ffprobe_meta = {}
        should_try_ffmpeg = (
            not is_rt_stream
            and (
                result.get("probe_status") in {"offline", "error", "timeout"}
                or (
                    result.get("probe_status") == "online"
                    and not result.get("speed_mbps")
                    and ffprobe_meta.get("probe_quality") == "http_only"
                )
            )
        )
        if should_try_ffmpeg:
            safe_final_url = str(_meta_value(result, "safe_final_url", "") or url)
            ffmpeg_result = await _probe_with_ffmpeg(
                safe_final_url,
                headers,
                reason=str(result.get("last_error") or result.get("probe_method") or "weak_http_probe"),
            )
            if ffmpeg_result.get("probe_status") == "online":
                result = ffmpeg_result
            elif result.get("probe_status") == "online":
                _merge_meta(result, {"ffmpeg_fallback": False, "ffmpeg_fallback_error": ffmpeg_result.get("last_error")})
    except (RedirectTargetRejected, UnsafeTargetError):
        result = _security_rejected_result(
            "ffmpeg" if is_rt_stream else "http_segment" if source_type == "hls" else "http_stream"
        )
    except httpx.TimeoutException:
        fallback_result = await _probe_with_ffmpeg(url, headers, reason="http_timeout")
        fallback_error = str(fallback_result.get("last_error") or "")
        if fallback_result.get("probe_status") == "online":
            result = fallback_result
            _merge_meta(result, {"http_error": "timeout", "media_fallback": "ffmpeg"})
        else:
            result = fallback_result
            _merge_meta(result, {
                "http_error": "timeout",
                "media_fallback_error": fallback_error or "media_probe_failed",
            })
            result.update(
                probe_status="timeout",
                probe_method="http_segment" if source_type == "hls" else "http_stream",
                last_error="timeout",
            )
    except Exception:
        fallback_result = await _probe_with_ffmpeg(url, headers, reason="http_exception")
        fallback_error = str(fallback_result.get("last_error") or "")
        if fallback_result.get("probe_status") == "online":
            result = fallback_result
            _merge_meta(result, {"http_error": "request_failed", "media_fallback": "ffmpeg"})
        else:
            result = fallback_result
            _merge_meta(result, {
                "http_error": "request_failed",
                "media_fallback_error": fallback_error or "media_probe_failed",
            })
            result.update(
                probe_status="error",
                probe_method="http_segment" if source_type == "hls" else "http_stream",
                last_error="http_probe_failed",
            )

    if result.get("probe_status") == "online":
        _merge_meta(result, {"probe_quality": _meta_value(result, "probe_quality", "http")})
    elif result.get("probe_status") in {"offline", "error", "timeout"}:
        _merge_meta(result, {"probe_quality": "failed"})
    # 页面确认在播 → offline 升级为 error（可重试），避免误判"未开播"
    if resolved_youtube_page_is_live and result.get("probe_status") == "offline":
        result["probe_status"] = "error"
        result["last_error"] = result.get("last_error") or "youtube_page_live_but_stream_unreachable"

    result["requires_headers"] = _requires_headers(headers)
    result["requires_proxy_declared"] = declared_proxy
    result["proxy_required_hint"] = bool(declared_proxy or adapter or _requires_headers(headers) or lower_url.startswith(("rtsp://", "rtmp://")))
    result["adapter_provider"] = adapter
    result["adapter_title"] = adapter_title
    if resolved_youtube_video_id:
        result["youtube_video_id"] = resolved_youtube_video_id
    existing_meta = {}
    try:
        existing_meta = json.loads(result.get("probe_meta_json") or "{}")
    except Exception:
        existing_meta = {}
    result["probe_meta_json"] = _sanitize_probe_meta_json(
        _safe_meta({**existing_meta, **meta, "source_type": source_type})
    )
    return result
