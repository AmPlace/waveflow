"""通用自动任务定义、持久化适配、单轮执行器和调度生命周期。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

import database as default_database


logger = logging.getLogger(__name__)

AUTOMATION_TRIGGERS = frozenset({"scheduler", "manual_api", "internal"})
AUTOMATION_RESULT_STATUSES = frozenset({"success", "partial", "failed", "cancelled"})
AUTOMATION_ERROR_MAX_LENGTH = 2048
AUTOMATION_INFRASTRUCTURE_RETRY_SECONDS = 60

Handler = Callable[["AutomationTaskContext"], Awaitable["AutomationHandlerResult"]]


class AutomationError(Exception):
    """自动任务执行模型的基础异常。"""


class AutomationRegistrationError(AutomationError):
    pass


class AutomationTaskNotFoundError(AutomationError):
    pass


class AutomationConfigurationError(AutomationError):
    pass


class AutomationRequestError(AutomationError):
    pass


class AutomationTriggerNotAllowedError(AutomationError):
    pass


class AutomationOwnershipLostError(AutomationError):
    pass


class AutomationWaitOutcome(str, Enum):
    TIMEOUT = "timeout"
    RECONFIGURED = "reconfigured"
    STOPPED = "stopped"


def _normalize_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是字符串")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} 不能为空")
    return value


def _normalize_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} 必须是非负整数")
    if value < 0:
        raise ValueError(f"{field_name} 不能小于 0")
    return value


def _normalize_positive_int(value: Any, field_name: str) -> int:
    value = _normalize_non_negative_int(value, field_name)
    if value == 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("时间提供器必须返回 datetime")
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat()


def _sanitize_error(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\x00", "").strip()
    text = re.sub(r"(?<![A-Za-z0-9])/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+", "<path>", text)
    text = re.sub(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s]+", "<path>", text)
    return text[:AUTOMATION_ERROR_MAX_LENGTH]


def _summarize_errors(error: str, errors: tuple[str, ...]) -> str:
    values = [item for item in (error, *errors) if item]
    return _sanitize_error("; ".join(values))


@dataclass(frozen=True, slots=True)
class AutomationTaskDefinition:
    task_id: str
    display_name: str
    conflict_group: str
    default_enabled: bool
    default_interval_seconds: int
    handler: Handler
    allow_manual_trigger: bool = True
    minimum_interval_seconds: int = 1
    maximum_interval_seconds: int | None = None
    initial_delay_seconds: int = 0
    allowed_task_types: frozenset[str] = field(default_factory=lambda: frozenset({"check"}))
    scheduled_task_type: str | None = None
    allow_automatic_scheduling: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _normalize_identifier(self.task_id, "task_id"))
        object.__setattr__(self, "display_name", _normalize_identifier(self.display_name, "display_name"))
        object.__setattr__(self, "conflict_group", _normalize_identifier(self.conflict_group, "conflict_group"))
        if not isinstance(self.default_enabled, bool):
            raise TypeError("default_enabled 必须是布尔值")
        if not isinstance(self.allow_manual_trigger, bool):
            raise TypeError("allow_manual_trigger 必须是布尔值")
        if not isinstance(self.allow_automatic_scheduling, bool):
            raise TypeError("allow_automatic_scheduling 必须是布尔值")
        minimum = _normalize_positive_int(self.minimum_interval_seconds, "minimum_interval_seconds")
        default_interval = _normalize_positive_int(self.default_interval_seconds, "default_interval_seconds")
        maximum = self.maximum_interval_seconds
        if maximum is not None:
            maximum = _normalize_positive_int(maximum, "maximum_interval_seconds")
            if minimum > maximum:
                raise ValueError("minimum_interval_seconds 不能大于 maximum_interval_seconds")
        if not minimum <= default_interval <= (maximum or default_interval):
            raise ValueError("default_interval_seconds 超出任务配置范围")
        initial_delay = _normalize_non_negative_int(self.initial_delay_seconds, "initial_delay_seconds")
        if not callable(self.handler):
            raise TypeError("handler 必须可调用")
        if isinstance(self.allowed_task_types, str):
            raise TypeError("allowed_task_types 必须是字符串集合")
        task_types = frozenset(self.allowed_task_types)
        if not task_types or any(not isinstance(item, str) or not item.strip() for item in task_types):
            raise ValueError("allowed_task_types 不能为空，且必须全部为非空字符串")
        scheduled_task_type = self.scheduled_task_type
        if scheduled_task_type is not None:
            scheduled_task_type = _normalize_identifier(scheduled_task_type, "scheduled_task_type")
            if scheduled_task_type not in task_types:
                raise ValueError("scheduled_task_type 必须包含在 allowed_task_types 中")
        if self.allow_automatic_scheduling and scheduled_task_type is None:
            raise ValueError("允许自动调度的任务必须设置 scheduled_task_type")
        object.__setattr__(self, "minimum_interval_seconds", minimum)
        object.__setattr__(self, "default_interval_seconds", default_interval)
        object.__setattr__(self, "maximum_interval_seconds", maximum)
        object.__setattr__(self, "initial_delay_seconds", initial_delay)
        object.__setattr__(self, "allowed_task_types", task_types)
        object.__setattr__(self, "scheduled_task_type", scheduled_task_type)


@dataclass(frozen=True, slots=True)
class AutomationRunRequest:
    task_id: str
    task_type: str
    trigger: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    stop_event: asyncio.Event | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _normalize_identifier(self.task_id, "task_id"))
        object.__setattr__(self, "task_type", _normalize_identifier(self.task_type, "task_type"))
        object.__setattr__(self, "trigger", _normalize_identifier(self.trigger, "trigger"))
        if self.trigger not in AUTOMATION_TRIGGERS:
            raise ValueError(f"不支持的自动任务触发来源: {self.trigger}")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters 必须是映射")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        if self.stop_event is not None and not isinstance(self.stop_event, asyncio.Event):
            raise TypeError("stop_event 必须是 asyncio.Event")


@dataclass(frozen=True, slots=True)
class AutomationHandlerResult:
    status: str | None = None
    checked_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    error: str = ""
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is not None and self.status not in AUTOMATION_RESULT_STATUSES:
            raise ValueError(f"非法自动任务结果状态: {self.status}")
        for field_name in ("checked_count", "updated_count", "skipped_count", "failed_count"):
            object.__setattr__(self, field_name, _normalize_non_negative_int(getattr(self, field_name), field_name))
        object.__setattr__(self, "error", _sanitize_error(self.error))
        if isinstance(self.errors, str):
            raise TypeError("errors 必须是错误字符串集合")
        normalized_errors = tuple(
            normalized
            for item in self.errors
            if (normalized := _sanitize_error(item))
        )
        object.__setattr__(self, "errors", normalized_errors)


@dataclass(frozen=True, slots=True)
class AutomationTaskConfig:
    task_id: str
    conflict_group: str
    enabled: bool
    interval_seconds: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class AutomationTaskState:
    task_id: str
    conflict_group: str
    task_type: str
    run_token: str
    last_started_at: str
    last_finished_at: str
    status: str
    checked_count: int
    updated_count: int
    skipped_count: int
    failed_count: int
    last_error: str


@dataclass(frozen=True, slots=True)
class AutomationClaim:
    claimed: bool
    state: AutomationTaskState


@dataclass(frozen=True, slots=True)
class AutomationBusy:
    task_id: str
    task_type: str
    conflict_group: str
    started_at: str
    status: str


@dataclass(frozen=True, slots=True)
class AutomationRunResult:
    task_id: str
    task_type: str
    status: str
    checked_count: int
    updated_count: int
    skipped_count: int
    failed_count: int
    error: str
    started_at: str
    finished_at: str
    errors: tuple[str, ...] = ()


def _config_from_row(row: Mapping[str, Any]) -> AutomationTaskConfig:
    return AutomationTaskConfig(
        task_id=row["task_id"],
        conflict_group=row["conflict_group"],
        enabled=bool(row["enabled"]),
        interval_seconds=int(row["interval_seconds"]),
        updated_at=row["updated_at"],
    )


def _state_from_row(row: Mapping[str, Any]) -> AutomationTaskState:
    return AutomationTaskState(
        task_id=row["task_id"],
        conflict_group=row["conflict_group"],
        task_type=row.get("task_type", "") if hasattr(row, "get") else row["task_type"],
        run_token=row.get("run_token", "") if hasattr(row, "get") else row["run_token"],
        last_started_at=row.get("last_started_at", "") if hasattr(row, "get") else row["last_started_at"],
        last_finished_at=row.get("last_finished_at", "") if hasattr(row, "get") else row["last_finished_at"],
        status=row.get("last_status", "never_run") if hasattr(row, "get") else row["last_status"],
        checked_count=int(row.get("checked_count", 0)) if hasattr(row, "get") else int(row["checked_count"]),
        updated_count=int(row.get("updated_count", 0)) if hasattr(row, "get") else int(row["updated_count"]),
        skipped_count=int(row.get("skipped_count", 0)) if hasattr(row, "get") else int(row["skipped_count"]),
        failed_count=int(row.get("failed_count", 0)) if hasattr(row, "get") else int(row["failed_count"]),
        last_error=row.get("last_error", "") if hasattr(row, "get") else row["last_error"],
    )


class AutomationRepository:
    """M2A-1 数据库入口的薄适配层，不包含业务或事务实现。"""

    def __init__(self, database_module=default_database):
        self._database = database_module

    async def ensure_config(self, definition: AutomationTaskDefinition) -> AutomationTaskConfig:
        row = await self._database.ensure_automation_task_config(
            definition.task_id,
            definition.conflict_group,
            definition.default_enabled,
            definition.default_interval_seconds,
        )
        return _config_from_row(row)

    async def get_config(self, task_id: str) -> AutomationTaskConfig | None:
        row = await self._database.get_automation_task_config(task_id)
        return _config_from_row(row) if row else None

    async def list_configs(self) -> list[AutomationTaskConfig]:
        rows = await self._database.list_automation_task_configs()
        return [_config_from_row(row) for row in rows]

    async def update_config(
        self,
        task_id: str,
        *,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
    ) -> AutomationTaskConfig | None:
        row = await self._database.update_automation_task_config(
            task_id,
            enabled=enabled,
            interval_seconds=interval_seconds,
        )
        return _config_from_row(row) if row else None

    async def delete_config(self, task_id: str) -> bool:
        return await self._database.delete_automation_task_config(task_id)

    async def claim(
        self,
        task_id: str,
        conflict_group: str,
        task_type: str,
        run_token: str,
        started_at: str,
    ) -> AutomationClaim:
        result = await self._database.claim_automation_task(
            task_id=task_id,
            conflict_group=conflict_group,
            task_type=task_type,
            run_token=run_token,
            started_at=started_at,
        )
        return AutomationClaim(
            claimed=bool(result["claimed"]),
            state=_state_from_row(result.get("state") or result.get("current")),
        )

    async def update_progress(self, task_id: str, run_token: str, **counts: Any) -> bool:
        return await self._database.update_automation_task_progress(task_id, run_token, **counts)

    async def complete(self, task_id: str, run_token: str, **result: Any) -> bool:
        return await self._database.complete_automation_task(task_id, run_token, **result)

    async def get_state(self, task_id: str) -> AutomationTaskState | None:
        row = await self._database.get_automation_task_state(task_id)
        return _state_from_row(row) if row else None

    async def list_states(self) -> list[AutomationTaskState]:
        rows = await self._database.list_automation_task_states()
        return [_state_from_row(row) for row in rows]

    async def get_busy(self, conflict_group: str) -> AutomationTaskState | None:
        row = await self._database.get_automation_conflict_group_state(conflict_group)
        return _state_from_row(row) if row else None

    async def recover_interrupted(self, **kwargs: Any) -> int:
        return await self._database.recover_interrupted_automation_tasks(**kwargs)


class AutomationRegistry:
    def __init__(self):
        self._definitions: dict[str, AutomationTaskDefinition] = {}

    def register(self, definition: AutomationTaskDefinition) -> None:
        if not isinstance(definition, AutomationTaskDefinition):
            raise TypeError("definition 必须是 AutomationTaskDefinition")
        if definition.task_id in self._definitions:
            raise AutomationRegistrationError(f"自动任务已注册: {definition.task_id}")
        self._definitions[definition.task_id] = definition

    def get(self, task_id: str) -> AutomationTaskDefinition:
        try:
            return self._definitions[task_id]
        except KeyError as exc:
            raise AutomationTaskNotFoundError(f"未知自动任务: {task_id}") from exc

    def list_definitions(self) -> tuple[AutomationTaskDefinition, ...]:
        return tuple(self._definitions.values())

    def unregister(self, task_id: str) -> AutomationTaskDefinition:
        try:
            return self._definitions.pop(task_id)
        except KeyError as exc:
            raise AutomationTaskNotFoundError(f"未知自动任务: {task_id}") from exc

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._definitions


class AutomationTaskContext:
    def __init__(
        self,
        *,
        task_id: str,
        task_type: str,
        trigger: str,
        parameters: Mapping[str, Any],
        run_token: str,
        stop_event: asyncio.Event,
        repository: AutomationRepository,
    ):
        self.task_id = task_id
        self.task_type = task_type
        self.trigger = trigger
        self.parameters = MappingProxyType(dict(parameters))
        self.run_token = run_token
        self._stop_event = stop_event
        self._repository = repository

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    @property
    def stop_event(self) -> asyncio.Event:
        """Expose the cooperative cancellation signal to domain services."""
        return self._stop_event

    async def report_progress(
        self,
        *,
        checked_count: int | None = None,
        updated_count: int | None = None,
        skipped_count: int | None = None,
        failed_count: int | None = None,
        error: str | None = None,
    ) -> None:
        persisted = await self._repository.update_progress(
            self.task_id,
            self.run_token,
            checked_count=checked_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            error=error,
        )
        if not persisted:
            raise AutomationOwnershipLostError("自动任务执行权已失效")


class AutomationRunner:
    """执行单轮任务；不负责等待、调度或创建后台协程。"""

    def __init__(
        self,
        registry: AutomationRegistry,
        repository: AutomationRepository | None = None,
        *,
        token_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.registry = registry
        self.repository = repository or AutomationRepository()
        self._token_factory = token_factory or (lambda: uuid.uuid4().hex)
        self._clock = clock or _utc_now

    def _timestamp(self) -> str:
        return _format_timestamp(self._clock())

    async def run(self, request: AutomationRunRequest) -> AutomationRunResult | AutomationBusy:
        if not isinstance(request, AutomationRunRequest):
            raise AutomationRequestError("request 必须是 AutomationRunRequest")
        definition = self.registry.get(request.task_id)
        if request.task_type not in definition.allowed_task_types:
            raise AutomationRequestError(f"任务不支持 task_type: {request.task_type}")
        if request.trigger == "manual_api" and not definition.allow_manual_trigger:
            raise AutomationTriggerNotAllowedError(f"任务不允许手动触发: {request.task_id}")

        config = await self.repository.ensure_config(definition)
        if config.conflict_group != definition.conflict_group:
            raise AutomationConfigurationError(
                f"任务 conflict_group 与数据库配置不一致: {request.task_id}"
            )
        if not definition.minimum_interval_seconds <= config.interval_seconds:
            raise AutomationConfigurationError(f"任务 interval_seconds 小于定义下限: {request.task_id}")
        if definition.maximum_interval_seconds is not None and config.interval_seconds > definition.maximum_interval_seconds:
            raise AutomationConfigurationError(f"任务 interval_seconds 大于定义上限: {request.task_id}")

        run_token = _normalize_identifier(self._token_factory(), "run_token")
        started_at = self._timestamp()
        claim = await self.repository.claim(
            request.task_id,
            definition.conflict_group,
            request.task_type,
            run_token,
            started_at,
        )
        if not claim.claimed:
            return AutomationBusy(
                task_id=claim.state.task_id,
                task_type=claim.state.task_type,
                conflict_group=claim.state.conflict_group,
                started_at=claim.state.last_started_at,
                status=claim.state.status,
            )

        stop_event = request.stop_event or asyncio.Event()
        context = AutomationTaskContext(
            task_id=request.task_id,
            task_type=request.task_type,
            trigger=request.trigger,
            parameters=request.parameters,
            run_token=run_token,
            stop_event=stop_event,
            repository=self.repository,
        )

        try:
            if stop_event.is_set():
                handler_result = AutomationHandlerResult(status="cancelled", error="任务收到停止请求")
            else:
                raw_result = definition.handler(context)
                if not inspect.isawaitable(raw_result):
                    raise TypeError("handler 必须返回可等待对象")
                raw_result = await raw_result
                if not isinstance(raw_result, AutomationHandlerResult):
                    raise TypeError("handler 必须返回 AutomationHandlerResult")
                handler_result = raw_result
        except asyncio.CancelledError:
            await self._try_complete_cancelled(
                request,
                run_token,
            )
            raise
        except Exception as exc:
            logger.exception("自动任务执行失败: %s", request.task_id)
            handler_result = AutomationHandlerResult(
                status="failed",
                error=_sanitize_error(str(exc)),
            )

        status = self._resolve_status(handler_result)
        finished_at = self._timestamp()
        error = _summarize_errors(handler_result.error, handler_result.errors)
        completed = await self.repository.complete(
            request.task_id,
            run_token,
            status=status,
            finished_at=finished_at,
            checked_count=handler_result.checked_count,
            updated_count=handler_result.updated_count,
            skipped_count=handler_result.skipped_count,
            failed_count=handler_result.failed_count,
            error=error,
        )
        if not completed:
            raise AutomationOwnershipLostError("自动任务完成时执行权已失效")
        return AutomationRunResult(
            task_id=request.task_id,
            task_type=request.task_type,
            status=status,
            checked_count=handler_result.checked_count,
            updated_count=handler_result.updated_count,
            skipped_count=handler_result.skipped_count,
            failed_count=handler_result.failed_count,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            errors=handler_result.errors,
        )

    async def _try_complete_cancelled(
        self,
        request: AutomationRunRequest,
        run_token: str,
    ) -> None:
        try:
            completed = await self.repository.complete(
                request.task_id,
                run_token,
                status="cancelled",
                finished_at=self._timestamp(),
                checked_count=0,
                updated_count=0,
                skipped_count=0,
                failed_count=0,
                error="任务被取消",
            )
            if not completed:
                logger.warning("自动任务取消时执行权已失效: %s", request.task_id)
        except Exception:
            logger.exception("自动任务取消状态写入失败: %s", request.task_id)

    @staticmethod
    def _resolve_status(result: AutomationHandlerResult) -> str:
        if result.status == "cancelled":
            return "cancelled"
        if result.status == "failed":
            return "failed"
        if result.status == "partial":
            return "partial"
        if result.failed_count == 0:
            return "success"
        has_successful_work = (
            result.updated_count > 0
            or result.skipped_count > 0
            or result.checked_count > result.failed_count
        )
        return "partial" if has_successful_work else "failed"


class AutomationEventWaiter:
    """等待超时、配置变化或停止，并在返回前回收内部短期 task。"""

    async def wait(
        self,
        delay_seconds: float | None,
        *,
        stop_event: asyncio.Event,
        config_event: asyncio.Event,
    ) -> AutomationWaitOutcome:
        if stop_event.is_set():
            return AutomationWaitOutcome.STOPPED
        if config_event.is_set():
            return AutomationWaitOutcome.RECONFIGURED
        if delay_seconds is not None and delay_seconds <= 0:
            return AutomationWaitOutcome.TIMEOUT

        stop_task = asyncio.create_task(stop_event.wait())
        config_task = asyncio.create_task(config_event.wait())
        tasks = (stop_task, config_task)
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=delay_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                return AutomationWaitOutcome.TIMEOUT
            if stop_task in done and stop_event.is_set():
                return AutomationWaitOutcome.STOPPED
            return AutomationWaitOutcome.RECONFIGURED
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def _validate_schedule_config(
    definition: AutomationTaskDefinition,
    config: AutomationTaskConfig,
) -> None:
    if config.conflict_group != definition.conflict_group:
        raise AutomationConfigurationError(
            f"任务 conflict_group 与数据库配置不一致: {definition.task_id}"
        )
    if config.interval_seconds < definition.minimum_interval_seconds:
        raise AutomationConfigurationError(
            f"任务 interval_seconds 小于定义下限: {definition.task_id}"
        )
    if (
        definition.maximum_interval_seconds is not None
        and config.interval_seconds > definition.maximum_interval_seconds
    ):
        raise AutomationConfigurationError(
            f"任务 interval_seconds 大于定义上限: {definition.task_id}"
        )


class AutomationScheduler:
    """调度一个任务；不 claim、不直接执行 handler，也不拥有长期 task handle。"""

    def __init__(
        self,
        definition: AutomationTaskDefinition,
        runner: AutomationRunner,
        repository: AutomationRepository,
        *,
        waiter: AutomationEventWaiter | None = None,
        infrastructure_retry_seconds: int = AUTOMATION_INFRASTRUCTURE_RETRY_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ):
        if not definition.allow_automatic_scheduling or definition.scheduled_task_type is None:
            raise AutomationConfigurationError(f"任务不允许自动调度: {definition.task_id}")
        self.definition = definition
        self.runner = runner
        self.repository = repository
        self.waiter = waiter or AutomationEventWaiter()
        self.infrastructure_retry_seconds = _normalize_positive_int(
            infrastructure_retry_seconds,
            "infrastructure_retry_seconds",
        )
        self._monotonic = monotonic or asyncio.get_running_loop().time
        self.stop_event = asyncio.Event()
        self.config_event = asyncio.Event()

    def stop(self) -> None:
        self.stop_event.set()
        self.config_event.set()

    def notify_config_changed(self) -> None:
        self.config_event.set()

    async def run(self) -> None:
        needs_initial_delay = True
        while not self.stop_event.is_set():
            try:
                config = await self._load_config()
                if not config.enabled:
                    needs_initial_delay = True
                    outcome = await self._wait(None)
                    if outcome is AutomationWaitOutcome.STOPPED:
                        return
                    self.config_event.clear()
                    continue

                if needs_initial_delay:
                    outcome = await self._wait_initial_delay()
                    if outcome is AutomationWaitOutcome.STOPPED:
                        return
                    if outcome is AutomationWaitOutcome.RECONFIGURED:
                        continue
                else:
                    outcome = await self._wait(config.interval_seconds)
                    if outcome is AutomationWaitOutcome.STOPPED:
                        return
                    if outcome is AutomationWaitOutcome.RECONFIGURED:
                        self.config_event.clear()
                        continue

                if self.stop_event.is_set():
                    return
                needs_initial_delay = False
                result = await self.runner.run(
                    AutomationRunRequest(
                        task_id=self.definition.task_id,
                        task_type=self.definition.scheduled_task_type,
                        trigger="scheduler",
                        stop_event=self.stop_event,
                    )
                )
                if isinstance(result, AutomationRunResult) and result.status == "cancelled":
                    return
                if self.stop_event.is_set():
                    return
            except AutomationConfigurationError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("自动任务调度基础设施异常: %s", self.definition.task_id)
                outcome = await self._wait(self.infrastructure_retry_seconds)
                if outcome is AutomationWaitOutcome.STOPPED:
                    return
                if outcome is AutomationWaitOutcome.RECONFIGURED:
                    self.config_event.clear()

    async def _load_config(self) -> AutomationTaskConfig:
        config = await self.repository.ensure_config(self.definition)
        _validate_schedule_config(self.definition, config)
        return config

    async def _wait_initial_delay(self) -> AutomationWaitOutcome:
        deadline = self._monotonic() + self.definition.initial_delay_seconds
        remaining = float(self.definition.initial_delay_seconds)
        while True:
            outcome = await self._wait(remaining)
            if outcome is not AutomationWaitOutcome.RECONFIGURED:
                return outcome
            self.config_event.clear()
            config = await self._load_config()
            if not config.enabled:
                return AutomationWaitOutcome.RECONFIGURED
            remaining = max(0.0, deadline - self._monotonic())

    async def _wait(self, delay_seconds: float | None) -> AutomationWaitOutcome:
        raw_outcome = await self.waiter.wait(
            delay_seconds,
            stop_event=self.stop_event,
            config_event=self.config_event,
        )
        try:
            return AutomationWaitOutcome(raw_outcome)
        except ValueError as exc:
            raise AutomationConfigurationError(f"waiter 返回非法结果: {raw_outcome}") from exc


class AutomationService:
    """统一拥有 Scheduler 实例、长期 task handle 和协作式启停。"""

    def __init__(
        self,
        *,
        registry: AutomationRegistry,
        repository: AutomationRepository | None = None,
        runner: AutomationRunner | None = None,
        waiter_factory: Callable[[AutomationTaskDefinition], AutomationEventWaiter] | None = None,
        scheduler_factory: Callable[..., AutomationScheduler] | None = None,
    ):
        self.registry = registry
        self.repository = repository or AutomationRepository()
        self.runner = runner or AutomationRunner(registry, self.repository)
        self._waiter_factory = waiter_factory or (lambda _definition: AutomationEventWaiter())
        self._scheduler_factory = scheduler_factory or AutomationScheduler
        self._schedulers: dict[str, AutomationScheduler] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._scheduler_errors: dict[str, str] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._started = False

    @property
    def schedulers(self) -> dict[str, AutomationScheduler]:
        return dict(self._schedulers)

    @property
    def tasks(self) -> dict[str, asyncio.Task[None]]:
        return dict(self._tasks)

    @property
    def scheduler_errors(self) -> dict[str, str]:
        return dict(self._scheduler_errors)

    @property
    def is_started(self) -> bool:
        return self._started

    async def start(self) -> int:
        async with self._lifecycle_lock:
            if self._started:
                return 0
            if self._schedulers or self._tasks:
                await self._cleanup_owned_locked(
                    phase="startup-precondition",
                    cancel_tasks=True,
                    clear_errors=True,
                )
            self._scheduler_errors.clear()
            staged_schedulers: dict[str, AutomationScheduler] = {}
            staged_tasks: dict[str, asyncio.Task[None]] = {}
            try:
                recovered_count = await self.repository.recover_interrupted()
                definitions = tuple(
                    definition
                    for definition in self.registry.list_definitions()
                    if definition.allow_automatic_scheduling
                )
                for definition in definitions:
                    config = await self.repository.ensure_config(definition)
                    _validate_schedule_config(definition, config)
                for definition in definitions:
                    scheduler, task = self._create_scheduler_locked(definition)
                    staged_schedulers[definition.task_id] = scheduler
                    staged_tasks[definition.task_id] = task

                # Publish only after every scheduler and task has been created.
                # There is no await between this publication and _started=True.
                self._schedulers = staged_schedulers
                self._tasks = staged_tasks
                self._started = True
                return recovered_count
            except BaseException:
                try:
                    await self._cleanup_staged(
                        staged_schedulers,
                        staged_tasks,
                        phase="startup-failure",
                    )
                finally:
                    self._schedulers.clear()
                    self._tasks.clear()
                    self._scheduler_errors.clear()
                    self._started = False
                raise

    async def add_definition(
        self,
        definition: AutomationTaskDefinition,
    ) -> AutomationTaskConfig:
        """Register and, when started, schedule one dynamic task definition."""
        async with self._lifecycle_lock:
            self.registry.register(definition)
            try:
                config = await self.repository.ensure_config(definition)
                _validate_schedule_config(definition, config)
                if self._started and definition.allow_automatic_scheduling:
                    self._start_scheduler_locked(definition)
                return config
            except BaseException:
                self.registry.unregister(definition.task_id)
                raise

    async def remove_definition(
        self,
        task_id: str,
        *,
        delete_config: bool = False,
    ) -> bool:
        """Stop and unregister one dynamic task, optionally deleting its state."""
        async with self._lifecycle_lock:
            definition = self.registry.get(task_id)
            scheduler = self._schedulers.pop(task_id, None)
            task = self._tasks.pop(task_id, None)
            if scheduler is not None:
                scheduler.stop()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            self._scheduler_errors.pop(task_id, None)
            self.registry.unregister(definition.task_id)
            if delete_config:
                return await self.repository.delete_config(task_id)
            return True

    async def stop(self, *, timeout_seconds: float | None = None) -> None:
        async with self._lifecycle_lock:
            if not self._started and not self._schedulers and not self._tasks:
                return
            if timeout_seconds is not None and timeout_seconds <= 0:
                raise ValueError("timeout_seconds 必须大于 0")
            await self._cleanup_owned_locked(
                phase="stop",
                timeout_seconds=timeout_seconds,
                cancel_tasks=False,
            )

    def notify_config_changed(self, task_id: str) -> None:
        scheduler = self._schedulers.get(task_id)
        if scheduler is None:
            raise AutomationTaskNotFoundError(f"没有运行中的自动调度任务: {task_id}")
        scheduler.notify_config_changed()

    def _create_scheduler_locked(
        self,
        definition: AutomationTaskDefinition,
    ) -> tuple[AutomationScheduler, asyncio.Task[None]]:
        if definition.task_id in self._tasks:
            raise AutomationRegistrationError(f"自动任务 Scheduler 已存在: {definition.task_id}")
        waiter = self._waiter_factory(definition)
        scheduler = self._scheduler_factory(
            definition,
            self.runner,
            self.repository,
            waiter=waiter,
        )
        task: asyncio.Task[None] | None = None
        run_coro = scheduler.run()
        try:
            task = asyncio.create_task(
                run_coro,
                name=f"automation-scheduler:{definition.task_id}",
            )
            task.add_done_callback(
                lambda completed, task_id=definition.task_id: self._observe_task(task_id, completed)
            )
        except BaseException:
            if task is None:
                run_coro.close()
            if task is not None and not task.done():
                task.cancel()
            try:
                scheduler.stop()
            except BaseException:
                logger.exception(
                    "自动任务 Scheduler 创建失败后的清理失败: %s",
                    definition.task_id,
                )
            raise
        assert task is not None
        return scheduler, task

    def _start_scheduler_locked(self, definition: AutomationTaskDefinition) -> None:
        scheduler, task = self._create_scheduler_locked(definition)
        self._schedulers[definition.task_id] = scheduler
        self._tasks[definition.task_id] = task

    async def _cleanup_staged(
        self,
        schedulers: Mapping[str, AutomationScheduler],
        tasks: Mapping[str, asyncio.Task[None]],
        *,
        phase: str,
    ) -> None:
        cleanup_task = asyncio.ensure_future(
            self._cleanup_resources(
                schedulers,
                tasks,
                phase=phase,
                cancel_tasks=True,
            )
        )
        await self._await_cleanup_task(cleanup_task, phase=phase)

    async def _cleanup_owned_locked(
        self,
        *,
        phase: str,
        timeout_seconds: float | None = None,
        cancel_tasks: bool,
        clear_errors: bool = False,
    ) -> None:
        schedulers = dict(self._schedulers)
        tasks = dict(self._tasks)
        cleanup_task = asyncio.ensure_future(
            self._cleanup_resources(
                schedulers,
                tasks,
                phase=phase,
                timeout_seconds=timeout_seconds,
                cancel_tasks=cancel_tasks,
            )
        )
        try:
            await self._await_cleanup_task(cleanup_task, phase=phase)
        finally:
            self._schedulers.clear()
            self._tasks.clear()
            if clear_errors:
                self._scheduler_errors.clear()
            self._started = False

    async def _cleanup_resources(
        self,
        schedulers: Mapping[str, AutomationScheduler],
        tasks: Mapping[str, asyncio.Task[None]],
        *,
        phase: str,
        timeout_seconds: float | None = None,
        cancel_tasks: bool,
    ) -> None:
        stop_failed: set[str] = set()
        for task_id, scheduler in schedulers.items():
            try:
                scheduler.stop()
            except BaseException:
                stop_failed.add(task_id)
                logger.exception(
                    "自动任务 Scheduler 清理失败: phase=%s task=%s",
                    phase,
                    task_id,
                )

        for task_id, task in tasks.items():
            if cancel_tasks or task_id in stop_failed:
                if not task.done():
                    task.cancel()

        task_values = tuple(tasks.values())
        if not task_values:
            return
        if timeout_seconds is None:
            results = await asyncio.gather(*task_values, return_exceptions=True)
        else:
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*task_values, return_exceptions=True),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                for task in task_values:
                    if not task.done():
                        task.cancel()
                results = await asyncio.gather(*task_values, return_exceptions=True)

        for task_id, result in zip(tasks, results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.error(
                    "自动任务 Scheduler task 清理完成但带有异常: phase=%s task=%s error=%s",
                    phase,
                    task_id,
                    _sanitize_error(str(result)) or result.__class__.__name__,
                )

    async def _await_cleanup_task(
        self,
        cleanup_task: asyncio.Future,
        *,
        phase: str,
    ) -> None:
        caller_cancelled = False
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            caller_cancelled = True
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
        except BaseException:
            logger.exception("自动任务生命周期清理任务异常: phase=%s", phase)

        try:
            cleanup_task.result()
        except asyncio.CancelledError:
            logger.error("自动任务生命周期清理任务被取消: phase=%s", phase)
        except BaseException:
            logger.exception("自动任务生命周期清理任务异常: phase=%s", phase)
        if caller_cancelled:
            raise asyncio.CancelledError

    def _observe_task(self, task_id: str, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is None:
            return
        message = _sanitize_error(str(error)) or error.__class__.__name__
        self._scheduler_errors[task_id] = message
        logger.error("自动任务 Scheduler 异常退出: %s: %s", task_id, message)
