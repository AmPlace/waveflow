"""Controlled source-level RTSP playback compatibility options."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class RtspVideoMode(str, Enum):
    COPY = "copy"
    TRANSCODE = "transcode"


class RtspTimestampMode(str, Enum):
    PASSTHROUGH = "passthrough"
    PTS_FROM_DTS = "pts_from_dts"


def normalize_rtsp_timestamp_mode(value: Any) -> str:
    if isinstance(value, RtspTimestampMode):
        return value.value
    raw = str(value or RtspTimestampMode.PASSTHROUGH.value).strip().lower()
    try:
        return RtspTimestampMode(raw).value
    except ValueError:
        return RtspTimestampMode.PASSTHROUGH.value


@dataclass(frozen=True)
class RtspPlaybackOptions:
    video_mode: RtspVideoMode = RtspVideoMode.COPY
    timestamp_mode: RtspTimestampMode = RtspTimestampMode.PASSTHROUGH

    def __post_init__(self) -> None:
        video_mode = RtspVideoMode(self.video_mode)
        timestamp_mode = RtspTimestampMode(self.timestamp_mode)
        if video_mode is RtspVideoMode.TRANSCODE:
            timestamp_mode = RtspTimestampMode.PASSTHROUGH
        object.__setattr__(self, "video_mode", video_mode)
        object.__setattr__(self, "timestamp_mode", timestamp_mode)

    @property
    def compat(self) -> bool:
        return self.video_mode is RtspVideoMode.TRANSCODE


def resolve_rtsp_playback_options(
    source: Mapping[str, Any] | None = None,
    *,
    compat: bool = False,
    timestamp_mode: Any = None,
) -> RtspPlaybackOptions:
    """Resolve one safe policy for formal, Smart, and legacy compat callers."""

    raw_timestamp_mode = timestamp_mode
    if raw_timestamp_mode is None and source is not None:
        raw_timestamp_mode = source.get("rtsp_timestamp_mode")
    return RtspPlaybackOptions(
        video_mode=RtspVideoMode.TRANSCODE if compat else RtspVideoMode.COPY,
        timestamp_mode=normalize_rtsp_timestamp_mode(raw_timestamp_mode),
    )


def rtsp_video_args(options: RtspPlaybackOptions) -> list[str]:
    if options.video_mode is RtspVideoMode.TRANSCODE:
        return [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-x264-params", "keyint=50:min-keyint=50:scenecut=0",
            "-force_key_frames", "expr:gte(t,n_forced*2)",
        ]

    args = ["-c:v", "copy"]
    if options.timestamp_mode is RtspTimestampMode.PTS_FROM_DTS:
        # Source-specific only: valid B-frame streams require PTS/DTS reordering.
        args.extend(["-bsf:v", "setts=pts=DTS"])
    return args
