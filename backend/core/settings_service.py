from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import database as db

from core.config import (
    EffectiveSecurityConfig,
    RuntimeSecuritySettings,
    build_effective_config,
    get_deployment_config,
    mode_defaults,
)


SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    "anonymous_browse": {"type": "bool"},
    "anonymous_playback": {"type": "bool"},
    "allow_private": {"type": "bool"},
    "allow_loopback": {"type": "bool"},
    "enable_rtsp_proxy": {"type": "bool"},
    "rtsp_max_sessions": {"type": "int", "min": 1, "max": 32},
    "media_credential_default_ttl_days": {"type": "int", "min": 1, "max": 3650},
    "session_max_age_days": {"type": "int", "min": 1, "max": 365},
    "public_base_url": {"type": "str", "max_len": 512},
    "m3u8_cache_ttl": {"type": "int", "min": 1, "max": 3600, "restart_required": True},
    "m3u8_cache_max_entries": {"type": "int", "min": 1, "max": 10000, "restart_required": True},
    "probe_concurrency": {"type": "int", "min": 1, "max": 64},
    "subscription_refresh_cooldown": {"type": "int", "min": 0, "max": 86400},
}


class SettingsValidationError(ValueError):
    pass


def _coerce_value(key: str, value: Any) -> Any:
    schema = SETTING_SCHEMA[key]
    kind = schema["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise SettingsValidationError(f"{key} 必须是布尔值")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError(f"{key} 必须是整数")
        number = value
        minimum = int(schema.get("min", number))
        maximum = int(schema.get("max", number))
        if number < minimum or number > maximum:
            raise SettingsValidationError(f"{key} 必须在 {minimum} 至 {maximum} 之间")
        return number
    if kind == "str":
        if not isinstance(value, str):
            raise SettingsValidationError(f"{key} 必须是字符串")
        text = value.strip()
        max_len = int(schema.get("max_len", 1024))
        if len(text) > max_len:
            raise SettingsValidationError(f"{key} 长度不能超过 {max_len}")
        return text.rstrip("/") if key == "public_base_url" else text
    raise SettingsValidationError(f"{key} 类型不支持")


def validate_runtime_settings(payload: dict[str, Any], *, forced_keys: set[str] | frozenset[str]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key not in SETTING_SCHEMA:
            raise SettingsValidationError(f"不允许修改设置项: {key}")
        if key in forced_keys:
            raise SettingsValidationError(f"{key} 已由环境变量强制指定")
        clean[key] = _coerce_value(key, value)
    return clean


def runtime_settings_from_mapping(values: dict[str, Any]) -> RuntimeSecuritySettings:
    clean = {}
    for key, value in values.items():
        if key in SETTING_SCHEMA:
            try:
                clean[key] = _coerce_value(key, value)
            except SettingsValidationError:
                continue
    return RuntimeSecuritySettings(**clean)


async def get_effective_settings() -> EffectiveSecurityConfig:
    runtime = runtime_settings_from_mapping(await db.get_app_settings())
    return build_effective_config(get_deployment_config(), runtime)


def get_effective_settings_sync() -> EffectiveSecurityConfig:
    def _read_runtime() -> RuntimeSecuritySettings:
        conn = db._connect()
        try:
            rows = conn.execute("SELECT key, value_json FROM app_settings").fetchall()
        finally:
            conn.close()
        values = {}
        for row in rows:
            try:
                values[row["key"]] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue
        return runtime_settings_from_mapping(values)

    return build_effective_config(get_deployment_config(), _read_runtime())


async def list_runtime_settings() -> dict[str, Any]:
    deployment = get_deployment_config()
    raw = await db.get_app_settings()
    runtime = runtime_settings_from_mapping(raw)
    effective = build_effective_config(deployment, runtime)
    defaults = mode_defaults(deployment.mode)
    values = asdict(effective)
    values["forced_keys"] = sorted(effective.forced_keys)
    return {
        "mode": deployment.mode,
        "settings": {key: values[key] for key in SETTING_SCHEMA},
        "defaults": {key: getattr(defaults, key) for key in SETTING_SCHEMA},
        "runtime": {key: getattr(runtime, key) for key in SETTING_SCHEMA if getattr(runtime, key) is not None},
        "forced": sorted(effective.forced_keys),
        "schema": {
            key: {
                "type": value["type"],
                **({"min": value["min"]} if "min" in value else {}),
                **({"max": value["max"]} if "max" in value else {}),
                **({"restart_required": True} if value.get("restart_required") else {}),
            }
            for key, value in SETTING_SCHEMA.items()
        },
    }


async def update_runtime_settings(payload: dict[str, Any], *, updated_by: int | None) -> dict[str, Any]:
    deployment = get_deployment_config()
    current_raw = await db.get_app_settings()
    clean = validate_runtime_settings(payload, forced_keys={
        key for key in SETTING_SCHEMA if getattr(deployment, f"force_{key}", None) is not None
    } | ({"public_base_url"} if deployment.public_base_url_override else set()))
    current_raw.update(clean)
    await db.set_app_settings(clean, updated_by=updated_by)
    return await list_runtime_settings()
