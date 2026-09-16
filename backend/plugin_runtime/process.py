from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from .errors import PluginError, invalid_response
from .protocol import LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, encode_frame, read_frame


logger = logging.getLogger("waveflow.plugin_runtime")


def sanitized_plugin_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    allowed = {"PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP",
               "SYSTEMROOT", "WINDIR"}
    return {key: value for key, value in source.items() if key in allowed and isinstance(value, str)}


class PluginProcess:
    def __init__(self, command: Sequence[str], instance_id: str, *,
                 on_exit: Callable[[int | None], Awaitable[None]] | None = None,
                 capability_handler: Callable[[str, dict[str, Any], float, dict[str, Any]], Awaitable[Any]] | None = None,
                 lifecycle_timeout: float = 30.0, environment: dict[str, str] | None = None,
                 working_directory: str | None = None):
        self.command = tuple(command)
        self.instance_id = instance_id
        self.on_exit = on_exit
        self.capability_handler = capability_handler
        self.lifecycle_timeout = lifecycle_timeout
        self.environment = dict(sanitized_plugin_environment() if environment is None else environment)
        self.working_directory = working_directory
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._completed: set[str] = set()
        self._completed_order: deque[str] = deque()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._wait_task: asyncio.Task | None = None
        self._capability_tasks: dict[str, tuple[asyncio.Task, str | None]] = {}
        self._write_lock = asyncio.Lock()
        self._stopping = False
        self.protocol_violations = 0
        self.stderr_lines: list[str] = []
        self.exit_code: int | None = None
        self.protocol_version = LEGACY_PROTOCOL_VERSION

    def diagnostic_tail(self, limit: int = 20) -> list[str]:
        """Return the bounded, sanitized tail of the Plugin's own stderr output.

        This buffer exists for developer diagnosis.  Callers must decide whether
        the consumer is inside the developer's trust boundary: plugin stderr may
        contain upstream URLs or credentials the Plugin author never intended to
        publish.  ``PluginError.as_contract`` never serializes it, so routing it
        through ``internal_diagnostics`` keeps it off the public API surface.
        """
        if limit < 1:
            return []
        return list(self.stderr_lines[-limit:])

    def negotiate_protocol(self, version: str) -> None:
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise PluginError("PLUGIN_INCOMPATIBLE", "Plugin protocol version is incompatible",
                              category="compatibility")
        self.protocol_version = version

    async def start(self) -> None:
        if self.process is not None:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin process is already running", category="lifecycle")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
                cwd=self.working_directory,
            )
        except (OSError, ValueError) as exc:
            raise PluginError("PLUGIN_UNAVAILABLE", "Unable to start plugin process", retryable=True,
                              category="lifecycle") from exc
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"plugin-reader:{self.instance_id}")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name=f"plugin-stderr:{self.instance_id}")
        self._wait_task = asyncio.create_task(self._wait_loop(), name=f"plugin-wait:{self.instance_id}")

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise PluginError("PLUGIN_CRASHED", "Plugin process is not running", retryable=True, category="lifecycle")
        frame = encode_frame(payload)
        try:
            async with self._write_lock:
                process.stdin.write(frame)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as exc:
            raise PluginError("PLUGIN_CRASHED", "Plugin process closed its protocol stream", retryable=True,
                              category="lifecycle") from exc

    async def call(self, method: str, payload: dict[str, Any], *, timeout: float = 15.0,
                   context: dict[str, Any] | None = None, request_id: str | None = None) -> Any:
        if self._stopping and method != "runtime.shutdown":
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin is stopping", retryable=True, category="lifecycle")
        request_id = request_id or f"core:{uuid.uuid4().hex}"
        if request_id in self._pending or request_id in self._completed:
            raise invalid_response("Plugin request_id is not unique")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        envelope = {
            "protocol_version": self.protocol_version,
            "request_id": request_id,
            "method": method,
            "plugin_instance": self.instance_id,
            "deadline_unix_ms": int((time.time() + timeout) * 1000),
            "context": dict(context or {}),
            "payload": dict(payload),
        }
        if self.protocol_version != LEGACY_PROTOCOL_VERSION:
            envelope.update({"kind": "request", "sender": "core"})
        try:
            await self._send(envelope)
            try:
                response = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except asyncio.CancelledError:
                self._pending.pop(request_id, None)
                self._remember_completed(request_id)
                if not future.done():
                    future.cancel()
                await self.cancel(request_id)
                raise
            except asyncio.TimeoutError as exc:
                self._pending.pop(request_id, None)
                self._remember_completed(request_id)
                if not future.done():
                    future.cancel()
                await self.cancel(request_id)
                raise PluginError("PLUGIN_TIMEOUT", "Plugin request timed out", retryable=True,
                                  category="timeout", details={"method": method}) from exc
        except BaseException:
            self._pending.pop(request_id, None)
            await self._cancel_capabilities_for_parent(request_id)
            raise
        await self._cancel_capabilities_for_parent(request_id)
        self._remember_completed(request_id)
        if response.get("status") == "error":
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise PluginError(str(error.get("code") or "INVALID_PLUGIN_RESPONSE"),
                              str(error.get("message") or "Plugin request failed"),
                              retryable=bool(error.get("retryable")), category=str(error.get("category") or "plugin"),
                              details=error.get("details") if isinstance(error.get("details"), dict) else {})
        return response.get("result")

    async def cancel(self, request_id: str) -> None:
        envelope = {
            "protocol_version": self.protocol_version,
            "request_id": f"core:{uuid.uuid4().hex}",
            "method": "runtime.cancel",
            "plugin_instance": self.instance_id,
            "deadline_unix_ms": int((time.time() + 1.0) * 1000),
            "context": {},
            "payload": {"request_id": request_id},
        }
        if self.protocol_version != LEGACY_PROTOCOL_VERSION:
            envelope.update({"kind": "notification", "sender": "core"})
        try:
            await self._send(envelope)
        except PluginError:
            pass

    async def _reader_loop(self) -> None:
        assert self.process and self.process.stdout
        try:
            while True:
                message = await read_frame(self.process.stdout)
                if message.get("protocol_version") not in SUPPORTED_PROTOCOL_VERSIONS:
                    raise invalid_response("Plugin protocol version mismatch")
                kind = message.get("kind")
                if kind is None:
                    kind = "response"
                sender = message.get("sender")
                if sender is not None and sender != "plugin":
                    raise invalid_response("Plugin message sender is invalid")
                if kind == "request":
                    if self.protocol_version == LEGACY_PROTOCOL_VERSION:
                        raise invalid_response("Plugin capability request requires protocol 1.1")
                    self._accept_capability_request(message)
                    continue
                if kind == "notification":
                    self._accept_notification(message)
                    continue
                if kind != "response":
                    raise invalid_response("Plugin message kind is invalid")
                request_id = message.get("request_id")
                if not isinstance(request_id, str):
                    raise invalid_response("Plugin response request_id is invalid")
                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    self.protocol_violations += 1
                    continue
                if message.get("status") not in {"ok", "error"}:
                    future.set_exception(invalid_response())
                    continue
                future.set_result(message)
        except asyncio.CancelledError:
            raise
        except PluginError as exc:
            self.protocol_violations += 1
            process = self.process
            if process is not None and (process.returncode is not None or process.stdout.at_eof()):
                self._fail_pending(PluginError("PLUGIN_CRASHED", "Plugin process closed its protocol stream",
                                               retryable=True, category="lifecycle"))
            else:
                self._fail_pending(exc)
                # A malformed frame makes correlation impossible. Treat the protocol stream as fatal;
                # the wait monitor will remove active routing through the normal crash path.
                if process is not None and process.returncode is None:
                    process.kill()

    def _accept_capability_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        method = message.get("method")
        payload = message.get("payload")
        deadline_ms = message.get("deadline_unix_ms")
        context = message.get("context", {})
        if (not isinstance(request_id, str) or not request_id.startswith("plugin:")
                or request_id in self._capability_tasks or request_id in self._completed
                or not isinstance(method, str) or not method.startswith("core.")
                or not isinstance(payload, dict) or not isinstance(context, dict)
                or not isinstance(deadline_ms, (int, float))):
            raise invalid_response("Invalid Plugin capability request")
        if self._stopping or self.capability_handler is None:
            task = asyncio.create_task(self._send_capability_error(
                request_id, PluginError("CAPABILITY_DENIED", "Core capability is unavailable", category="capability")
            ))
        else:
            timeout = max(0.0, (float(deadline_ms) / 1000.0) - time.time())
            task = asyncio.create_task(
                self._run_capability(request_id, method, payload, timeout, context),
                name=f"plugin-capability:{self.instance_id}:{request_id}",
            )
        parent = context.get("parent_request_id") if isinstance(context.get("parent_request_id"), str) else None
        self._capability_tasks[request_id] = (task, parent)
        task.add_done_callback(lambda _task, key=request_id: self._capability_tasks.pop(key, None))

    def _accept_notification(self, message: dict[str, Any]) -> None:
        if message.get("method") != "runtime.cancel" or not isinstance(message.get("payload"), dict):
            raise invalid_response("Invalid Plugin notification")
        target = message["payload"].get("request_id")
        entry = self._capability_tasks.get(target) if isinstance(target, str) else None
        if entry and not entry[0].done():
            entry[0].cancel()

    async def _run_capability(self, request_id: str, method: str, payload: dict[str, Any],
                              timeout: float, context: dict[str, Any]) -> None:
        try:
            if timeout <= 0:
                raise PluginError("PLUGIN_TIMEOUT", "Capability request deadline expired", retryable=True,
                                  category="timeout")
            assert self.capability_handler is not None
            result = await asyncio.wait_for(
                self.capability_handler(method, payload, timeout, context), timeout=timeout
            )
            await self._send({
                "protocol_version": self.protocol_version, "kind": "response", "sender": "core",
                "request_id": request_id, "status": "ok", "result": result, "error": None,
                "diagnostics": {},
            })
        except asyncio.CancelledError:
            raise
        except PluginError as exc:
            await self._send_capability_error(request_id, exc)
        except BaseException:
            await self._send_capability_error(request_id, PluginError(
                "TEMPORARY_UPSTREAM_FAILURE", "Core capability failed", retryable=True, category="capability"
            ))
        finally:
            self._remember_completed(request_id)

    async def _send_capability_error(self, request_id: str, exc: PluginError) -> None:
        try:
            await self._send({
                "protocol_version": self.protocol_version, "kind": "response", "sender": "core",
                "request_id": request_id, "status": "error", "result": None,
                "error": exc.as_contract(), "diagnostics": {},
            })
        except PluginError:
            pass

    async def _cancel_capabilities_for_parent(self, parent_request_id: str) -> None:
        tasks = [task for task, parent in self._capability_tasks.values()
                 if parent == parent_request_id and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_all_capabilities(self) -> None:
        tasks = [task for task, _parent in self._capability_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stderr_loop(self) -> None:
        assert self.process and self.process.stderr
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            sanitized = text[:500].replace("Authorization", "[redacted-header]").replace("Cookie", "[redacted-header]")
            self.stderr_lines.append(sanitized)
            self.stderr_lines[:] = self.stderr_lines[-100:]

    async def _wait_loop(self) -> None:
        assert self.process
        self.exit_code = await self.process.wait()
        # Drain the tail of the Plugin's stderr before the exit is reported, so
        # a startup crash carries the traceback that caused it.  The pipe is at
        # EOF once the process is gone, so this returns immediately in practice;
        # the timeout only bounds the pathological case.
        if self._stderr_task is not None and not self._stderr_task.done():
            await asyncio.wait({self._stderr_task}, timeout=1.0)
        await self._cancel_all_capabilities()
        if not self._stopping:
            self._fail_pending(PluginError("PLUGIN_CRASHED", "Plugin process exited unexpectedly", retryable=True,
                                           category="lifecycle"))
            if self.on_exit:
                await self.on_exit(self.exit_code)

    def _fail_pending(self, exc: PluginError) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    def _remember_completed(self, request_id: str) -> None:
        if request_id in self._completed:
            return
        self._completed.add(request_id)
        self._completed_order.append(request_id)
        while len(self._completed_order) > 4096:
            self._completed.discard(self._completed_order.popleft())

    async def stop(self, *, graceful: bool = True) -> None:
        process = self.process
        if process is None:
            return
        self._stopping = True
        if graceful and process.returncode is None:
            try:
                await self.call("runtime.shutdown", {}, timeout=min(self.lifecycle_timeout, 2.0))
            except PluginError:
                pass
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=self.lifecycle_timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self.exit_code = process.returncode
        self._fail_pending(PluginError("PLUGIN_UNAVAILABLE", "Plugin process stopped", category="lifecycle"))
        await self._cancel_all_capabilities()
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task, self._wait_task):
            if task and task is not current and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (self._reader_task, self._stderr_task, self._wait_task)
                               if task and task is not current), return_exceptions=True)
