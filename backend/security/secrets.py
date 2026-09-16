"""服务端对称密钥获取（用途隔离 + 持久化兜底）。

链路：环境变量 → SQLite app_secrets 持久化 → 首次启动自动生成。

各调用点不直接读「根密钥」，而是通过 ``derive_key(purpose)`` 派生，
保证 proxy-handle / 未来 media-credential-mac / desktop-bridge 等用途
即便共用同一根密钥也不会因签名复用而互相污染。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone

import database as app_db
from core.config import get_deployment_config


_PROXY_HANDLE_ROOT_NAME = "proxy_handle_root"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_persisted(name: str) -> str:
    try:
        conn = app_db._connect()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, FileNotFoundError):
        # DB 还没初始化（首次启动、测试 in-memory db）——回退到生成
        return ""
    try:
        row = conn.execute(
            "SELECT value FROM app_secrets WHERE name=?", (name,)
        ).fetchone()
        return row["value"] if row else ""
    except sqlite3.OperationalError:
        # app_secrets 表还不存在（schema 未 migrate）
        return ""
    finally:
        conn.close()


def _write_persisted(name: str, value: str) -> None:
    try:
        conn = app_db._connect()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, FileNotFoundError):
        return
    try:
        conn.execute(
            "INSERT OR IGNORE INTO app_secrets(name, value, created_at) VALUES(?, ?, ?)",
            (name, value, _now_iso()),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # app_secrets 表还不存在
        pass
    finally:
        conn.close()


def _generate() -> str:
    # 32 字节 = 256 bit；token_urlsafe 的字节数与位数对应是 32 字节 → 43 字符串
    return secrets.token_urlsafe(32)


def _root_secret_sync() -> str:
    """同步获取 proxy-handle 根密钥；启动期、测试可注入用。

    注意：环境变量优先；测试时 ``WAVEFLOW_PROXY_HANDLE_SECRET`` 直接覆盖。
    持久化值仅在「环境变量未提供」时使用，避免某次部署忘记给 env 时
    密钥从 DB 漂移到旧值的语义模糊。

    环境变量直接读取 os.environ，不经过 DeploymentConfig。
    理由：DeploymentConfig 可能因 LRU cache 在跨测试复用旧值，
    更危险的是如果没有 app_secrets 表且 config 也没给 secret，
    _write_persisted 会 silent fail，导致下次 derive 仍读到空。
    """
    env_value = (os.environ.get("WAVEFLOW_PROXY_HANDLE_SECRET") or "").strip()
    if env_value:
        return env_value

    deployment = get_deployment_config()
    config_value = (deployment.proxy_handle_secret or "").strip()
    if config_value:
        return config_value

    persisted = _read_persisted(_PROXY_HANDLE_ROOT_NAME)
    if persisted:
        return persisted

    generated = _generate()
    _write_persisted(_PROXY_HANDLE_ROOT_NAME, generated)
    # 写入后重读以处理并发首启（INSERT OR IGNORE）
    return _read_persisted(_PROXY_HANDLE_ROOT_NAME) or generated


async def root_secret() -> str:
    return await asyncio.to_thread(_root_secret_sync)


def derive_key(purpose: str, *, root: str | None = None) -> bytes:
    """按用途派生子密钥。purpose 取自常量字符串，例如 ``proxy-handle-v1``。

    HMAC-SHA256(root, purpose) 等价于 RFC 5869 一步 expand；
    purpose 必须是稳定字面量，不能拼用户输入。
    """
    if not purpose:
        raise ValueError("derive_key purpose 必须非空")
    base = (root if root is not None else _root_secret_sync()).encode("utf-8")
    return hmac.new(base, purpose.encode("utf-8"), hashlib.sha256).digest()


def reset_for_tests() -> None:
    """测试用：重新读取，便于注入新的 env 值后清掉模块级缓存（目前无缓存）。"""
    return None


# 用途常量，集中维护，避免拼写漂移。
PROXY_HANDLE_PURPOSE = "proxy-handle-v1"
SOURCE_ID_PURPOSE = "media-source-id-v1"
