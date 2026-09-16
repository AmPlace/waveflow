from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env(name: str) -> str | None:
    return os.getenv(name)


def _env_bool_optional(name: str) -> bool | None:
    raw = _env(name)
    if raw is None or raw == "":
        return None
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def _env_bool(name: str, default: bool) -> bool:
    value = _env_bool_optional(name)
    return default if value is None else value


def _env_int_optional(name: str, minimum: int = 0) -> int | None:
    raw = _env(name)
    if raw is None or raw == "":
        return None
    try:
        return max(minimum, int(raw))
    except ValueError:
        return None


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    value = _env_int_optional(name, minimum=minimum)
    return default if value is None else value


def _env_csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in (_env(name) or "").split(",") if item.strip())


@dataclass(frozen=True)
class DeploymentConfig:
    mode: str
    production: bool
    trusted_proxies: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bind_host: str
    bind_port: int
    data_directory: str
    public_base_url_override: str
    desktop_session: str
    session_signing_secret: str
    proxy_handle_secret: str
    docs_enabled: bool
    force_anonymous_browse: bool | None
    force_anonymous_playback: bool | None
    force_allow_private: bool | None
    force_allow_loopback: bool | None
    force_enable_rtsp_proxy: bool | None
    force_rtsp_max_sessions: int | None
    force_public_base_url: str | None
    force_m3u8_cache_ttl: int | None
    force_m3u8_cache_max_entries: int | None
    force_session_max_age_days: int | None
    force_media_credential_default_ttl_days: int | None
    force_probe_concurrency: int | None
    force_subscription_refresh_cooldown: int | None


@dataclass(frozen=True)
class RuntimeSecuritySettings:
    anonymous_browse: bool | None = None
    anonymous_playback: bool | None = None
    allow_private: bool | None = None
    allow_loopback: bool | None = None
    enable_rtsp_proxy: bool | None = None
    rtsp_max_sessions: int | None = None
    media_credential_default_ttl_days: int | None = None
    session_max_age_days: int | None = None
    public_base_url: str | None = None
    m3u8_cache_ttl: int | None = None
    m3u8_cache_max_entries: int | None = None
    probe_concurrency: int | None = None
    subscription_refresh_cooldown: int | None = None


@dataclass(frozen=True)
class EffectiveSecurityConfig:
    mode: str
    production: bool
    anonymous_browse: bool
    anonymous_playback: bool
    allow_private: bool
    allow_loopback: bool
    enable_rtsp_proxy: bool
    trusted_proxies: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    public_base_url: str
    rtsp_max_sessions: int
    media_credential_default_ttl_days: int
    session_max_age_days: int
    m3u8_cache_ttl: int
    m3u8_cache_max_entries: int
    probe_concurrency: int
    subscription_refresh_cooldown: int
    session_cookie_secure: bool
    desktop_session_secret: str
    session_secret: str
    proxy_handle_secret: str
    docs_enabled: bool
    forced_keys: frozenset[str]


_MODE_DEFAULTS: dict[str, RuntimeSecuritySettings] = {
    "desktop": RuntimeSecuritySettings(
        anonymous_browse=True,
        anonymous_playback=True,
        allow_private=True,
        allow_loopback=True,
        enable_rtsp_proxy=True,
        rtsp_max_sessions=8,
        media_credential_default_ttl_days=90,
        session_max_age_days=14,
        public_base_url="",
        m3u8_cache_ttl=60,
        m3u8_cache_max_entries=1000,
        probe_concurrency=8,
        subscription_refresh_cooldown=60,
    ),
    "nas": RuntimeSecuritySettings(
        anonymous_browse=True,
        anonymous_playback=True,
        allow_private=True,
        allow_loopback=False,
        enable_rtsp_proxy=True,
        rtsp_max_sessions=8,
        media_credential_default_ttl_days=90,
        session_max_age_days=14,
        public_base_url="",
        m3u8_cache_ttl=60,
        m3u8_cache_max_entries=1000,
        probe_concurrency=8,
        subscription_refresh_cooldown=60,
    ),
    "public": RuntimeSecuritySettings(
        anonymous_browse=False,
        anonymous_playback=False,
        allow_private=False,
        allow_loopback=False,
        enable_rtsp_proxy=False,
        rtsp_max_sessions=8,
        media_credential_default_ttl_days=90,
        session_max_age_days=14,
        public_base_url="",
        m3u8_cache_ttl=60,
        m3u8_cache_max_entries=1000,
        probe_concurrency=4,
        subscription_refresh_cooldown=300,
    ),
}


def mode_defaults(mode: str) -> RuntimeSecuritySettings:
    return _MODE_DEFAULTS.get(mode, _MODE_DEFAULTS["nas"])


@lru_cache(maxsize=1)
def get_deployment_config() -> DeploymentConfig:
    mode = (_env("WAVEFLOW_MODE") or "nas").strip().lower()
    if mode not in _MODE_DEFAULTS:
        mode = "nas"
    production = _env_bool("WAVEFLOW_PRODUCTION", mode == "public")
    docs_enabled = _env_bool("WAVEFLOW_DOCS_ENABLED", not production)
    public_base_url = (_env("WAVEFLOW_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return DeploymentConfig(
        mode=mode,
        production=production,
        trusted_proxies=_env_csv("WAVEFLOW_TRUSTED_PROXIES"),
        allowed_origins=_env_csv("WAVEFLOW_ALLOWED_ORIGINS"),
        bind_host=(_env("WAVEFLOW_BIND_HOST") or "").strip(),
        bind_port=_env_int("WAVEFLOW_BIND_PORT", 0, minimum=0),
        data_directory=(_env("WAVEFLOW_DATA_DIR") or "").strip(),
        public_base_url_override=public_base_url,
        desktop_session=(_env("WAVEFLOW_DESKTOP_SESSION") or "").strip(),
        session_signing_secret=(_env("WAVEFLOW_SESSION_SECRET") or "").strip(),
        proxy_handle_secret=(_env("WAVEFLOW_PROXY_HANDLE_SECRET") or "").strip(),
        docs_enabled=docs_enabled,
        force_anonymous_browse=_env_bool_optional("WAVEFLOW_ANONYMOUS_BROWSE"),
        force_anonymous_playback=_env_bool_optional("WAVEFLOW_ANONYMOUS_PLAYBACK"),
        force_allow_private=_env_bool_optional("WAVEFLOW_ALLOW_PRIVATE"),
        force_allow_loopback=_env_bool_optional("WAVEFLOW_ALLOW_LOOPBACK"),
        force_enable_rtsp_proxy=_env_bool_optional("WAVEFLOW_ENABLE_RTSP_PROXY"),
        force_rtsp_max_sessions=_env_int_optional("WAVEFLOW_RTSP_MAX_SESSIONS", minimum=1),
        force_public_base_url=public_base_url or None,
        force_m3u8_cache_ttl=_env_int_optional("WAVEFLOW_M3U8_CACHE_TTL", minimum=1),
        force_m3u8_cache_max_entries=_env_int_optional("WAVEFLOW_M3U8_CACHE_MAX_ENTRIES", minimum=1),
        force_session_max_age_days=_env_int_optional("WAVEFLOW_SESSION_MAX_AGE_DAYS", minimum=1),
        force_media_credential_default_ttl_days=_env_int_optional("WAVEFLOW_MEDIA_CREDENTIAL_DEFAULT_TTL_DAYS", minimum=1),
        force_probe_concurrency=_env_int_optional("WAVEFLOW_PROBE_CONCURRENCY", minimum=1),
        force_subscription_refresh_cooldown=_env_int_optional("WAVEFLOW_SUBSCRIPTION_REFRESH_COOLDOWN", minimum=0),
    )


def build_effective_config(
    deployment: DeploymentConfig,
    runtime: RuntimeSecuritySettings | None = None,
) -> EffectiveSecurityConfig:
    runtime = runtime or RuntimeSecuritySettings()
    defaults = mode_defaults(deployment.mode)
    forced_keys: set[str] = set()

    def choose(key: str):
        forced = getattr(deployment, f"force_{key}", None)
        if forced is not None:
            forced_keys.add(key)
            return forced
        value = getattr(runtime, key, None)
        if value is not None:
            return value
        return getattr(defaults, key)

    public_base_url = deployment.public_base_url_override or choose("public_base_url") or ""
    if deployment.public_base_url_override:
        forced_keys.add("public_base_url")

    session_max_age_days = int(choose("session_max_age_days"))
    return EffectiveSecurityConfig(
        mode=deployment.mode,
        production=deployment.production,
        anonymous_browse=bool(choose("anonymous_browse")),
        anonymous_playback=bool(choose("anonymous_playback")),
        allow_private=bool(choose("allow_private")),
        allow_loopback=bool(choose("allow_loopback")),
        enable_rtsp_proxy=bool(choose("enable_rtsp_proxy")),
        trusted_proxies=deployment.trusted_proxies,
        allowed_origins=deployment.allowed_origins,
        public_base_url=str(public_base_url).rstrip("/"),
        rtsp_max_sessions=int(choose("rtsp_max_sessions")),
        media_credential_default_ttl_days=int(choose("media_credential_default_ttl_days")),
        session_max_age_days=session_max_age_days,
        m3u8_cache_ttl=int(choose("m3u8_cache_ttl")),
        m3u8_cache_max_entries=int(choose("m3u8_cache_max_entries")),
        probe_concurrency=int(choose("probe_concurrency")),
        subscription_refresh_cooldown=int(choose("subscription_refresh_cooldown")),
        session_cookie_secure=_env_bool("WAVEFLOW_SESSION_COOKIE_SECURE", deployment.production or deployment.mode == "public"),
        desktop_session_secret=deployment.desktop_session,
        session_secret=deployment.session_signing_secret,
        proxy_handle_secret=deployment.proxy_handle_secret,
        docs_enabled=deployment.docs_enabled,
        forced_keys=frozenset(forced_keys),
    )


def get_settings() -> EffectiveSecurityConfig:
    """Synchronous fallback for startup and code paths not yet DB-aware."""
    return build_effective_config(get_deployment_config())


def reset_settings_cache() -> None:
    get_deployment_config.cache_clear()
