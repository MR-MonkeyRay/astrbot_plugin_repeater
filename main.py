"""将 AstrBot 群事件适配为可持久化的复读事务。

本模块处理框架事件、权限检查和消息发送；连续消息判定及状态更新由
`RepeaterStateService` 负责。
"""

import asyncio
from copy import deepcopy
import inspect
import random
import time
from dataclasses import dataclass
from typing import Any, Literal

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

if __package__:
    from .intelligent_history import (
        IntelligentActionRecord,
        IntelligentHistoryStore,
    )
    from .repeater_config import (
        INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH,
        INTELLIGENT_INTERRUPT_MANUAL_API_KEY_MAX_LENGTH,
        INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
        INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE,
        RepeaterSettings,
        build_settings,
        normalize_intelligent_interrupt_manual_api_base,
        normalize_intelligent_interrupt_manual_api_key,
        normalize_intelligent_interrupt_provider_mode,
    )
    from .repeater_messages import RepeatableMessage, repeatable_message
    from .repeater_service import RepeatAttempt, RepeaterStateService
else:
    from intelligent_history import IntelligentActionRecord, IntelligentHistoryStore
    from repeater_config import (
        INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH,
        INTELLIGENT_INTERRUPT_MANUAL_API_KEY_MAX_LENGTH,
        INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
        INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE,
        RepeaterSettings,
        build_settings,
        normalize_intelligent_interrupt_manual_api_base,
        normalize_intelligent_interrupt_manual_api_key,
        normalize_intelligent_interrupt_provider_mode,
    )
    from repeater_messages import RepeatableMessage, repeatable_message
    from repeater_service import RepeatAttempt, RepeaterStateService


PERMISSION_ERROR = (
    "权限错误：仅 AstrBot 管理员、群主或群管理员可以开启或关闭。"
    "请向本群管理员或群主求助。"
)

PLUGIN_NAME = "astrbot_plugin_repeater"
HISTORY_CLEANUP_INTERVAL_SECONDS = 60 * 60
MAX_CONFIG_TEXT_LENGTH = INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH
MAX_HISTORY_PAGE = 10_000
MANUAL_OPENAI_COMPATIBLE_PROVIDER_ID = "manual-openai-compatible"


@dataclass(frozen=True, slots=True)
class IntelligentGenerationResult:
    """A completion result safe to expose to history and the Page."""

    completion: str | None
    provider_id: str | None
    model: str
    latency_ms: int
    result_code: Literal[
        "success",
        "provider_resolution_failed",
        "request_failed",
        "invalid_response",
    ]


class RepeaterPlugin(Star):
    """将 AstrBot 群事件委托给复读状态服务。

    Attributes:
        state_service: 维护各群状态、配置和发送事务的服务。
        shutting_down: 是否拒绝登记新的消息或命令处理器。
        active_handler_tasks: 终止时需要等待的活动处理任务。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """初始化插件依赖和可恢复的状态服务。

        Args:
            context: AstrBot 提供的插件上下文。
            config: 插件配置；为 None 时使用空字典。
        """
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self.state_service = RepeaterStateService(
            build_settings(self.config, logger),
            load_states=lambda: self.get_kv_data("group_states", {}),
            save_states=lambda states: self.put_kv_data("group_states", states),
            logger=logger,
        )
        self.shutting_down = False
        self.active_handler_tasks: set[asyncio.Task] = set()
        self.history_store: IntelligentHistoryStore | None = None
        self._history_storage_error: str | None = None
        self._history_cleanup_task: asyncio.Task[None] | None = None
        self._history_write_tasks: set[asyncio.Task[None]] = set()

    async def initialize(self) -> None:
        """恢复群状态、注册 Page API，并初始化可选历史存储。"""
        await self.state_service.initialize()
        self._register_intelligent_console_routes()
        await self._initialize_history_store()

    def _plugin_route_prefix(self) -> str:
        """Return the extension route prefix for the loaded plugin."""
        plugin_name = getattr(self, "name", None)
        if not isinstance(plugin_name, str) or not plugin_name.strip():
            plugin_name = PLUGIN_NAME
        return f"/{plugin_name.strip()}"

    def _register_intelligent_console_routes(self) -> None:
        """Register authenticated, drainable Page API handlers."""
        register_web_api = getattr(
            getattr(self, "context", None),
            "register_web_api",
            None,
        )
        if not callable(register_web_api):
            return
        prefix = f"{self._plugin_route_prefix()}/intelligent-console"
        track = self._track_intelligent_console_handler
        register_web_api(
            f"{prefix}/config",
            track(self._web_get_intelligent_console_config),
            ["GET"],
            "读取智能文案测试配置",
        )
        register_web_api(
            f"{prefix}/models",
            track(self._web_get_intelligent_console_models),
            ["GET"],
            "读取智能文案模型列表",
        )
        register_web_api(
            f"{prefix}/config",
            track(self._web_save_intelligent_console_config),
            ["POST"],
            "保存智能文案测试配置",
        )
        register_web_api(
            f"{prefix}/test/repeat",
            track(self._web_test_intelligent_repeat),
            ["POST"],
            "测试智能打断文案",
        )
        register_web_api(
            f"{prefix}/test/mute",
            track(self._web_test_intelligent_mute),
            ["POST"],
            "测试智能禁言提示文案",
        )
        register_web_api(
            f"{prefix}/history",
            track(self._web_get_intelligent_history),
            ["GET"],
            "读取智能文案生成记录",
        )

    def _track_intelligent_console_handler(self, handler):
        """Bind a Page request to the same shutdown drain as event handlers."""

        async def tracked_handler():
            task = self._begin_handler()
            if task is None:
                return self._intelligent_console_shutdown_response()
            try:
                return await handler()
            finally:
                self._finish_handler(task)

        return tracked_handler

    async def _initialize_history_store(self) -> None:
        """Initialize history only when production metadata supplies a plugin name."""
        if self.history_store is None:
            plugin_name = getattr(self, "name", None)
            if not isinstance(plugin_name, str) or not plugin_name.strip():
                return
            try:
                data_dir = StarTools.get_data_dir(plugin_name.strip())
            except Exception:
                logger.exception("[repeater] 智能记录数据目录不可用")
                self._history_storage_error = "智能记录存储不可用。"
                return
            self.history_store = IntelligentHistoryStore(
                data_dir / "intelligent_history.sqlite3",
            )
        try:
            await self.history_store.initialize()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[repeater] 智能记录数据库初始化失败")
            self._history_storage_error = "智能记录存储不可用。"
            return
        self._history_storage_error = None
        if self._history_cleanup_task is None or self._history_cleanup_task.done():
            self._history_cleanup_task = asyncio.create_task(
                self._history_cleanup_loop(),
                name="repeater-intelligent-history-cleanup",
            )

    async def _history_cleanup_loop(self) -> None:
        """Purge expired telemetry hourly until plugin termination."""
        try:
            while not self.shutting_down:
                await asyncio.sleep(HISTORY_CLEANUP_INTERVAL_SECONDS)
                if self.shutting_down:
                    return
                await self._purge_history()
        except asyncio.CancelledError:
            raise

    async def _purge_history(self) -> None:
        """Run one best-effort retention cleanup without affecting plugin behavior."""
        if self.history_store is None:
            return
        try:
            deleted = await self.history_store.purge_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[repeater] 智能记录清理失败")
            self._history_storage_error = "智能记录存储不可用。"
            return
        self._history_storage_error = None
        if deleted:
            logger.info(f"[repeater] 已清理 {deleted} 条过期智能记录")

    @property
    def _history_available(self) -> bool:
        return self.history_store is not None and self._history_storage_error is None

    async def _append_history_record(
        self,
        record: IntelligentActionRecord,
    ) -> None:
        """Persist one record while keeping history failures off the user path."""
        if self.history_store is None:
            return
        try:
            await self.history_store.append(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[repeater] 智能记录写入失败")
            self._history_storage_error = "智能记录存储不可用。"
            return
        self._history_storage_error = None

    def _schedule_history_record(self, record: IntelligentActionRecord) -> None:
        """Write runtime telemetry asynchronously after generation completes."""
        if self.history_store is None:
            return
        task = asyncio.create_task(self._append_history_record(record))
        self._history_write_tasks.add(task)
        task.add_done_callback(self._history_write_tasks.discard)

    async def _stop_history_cleanup_task(self) -> None:
        """Cancel periodic retention work before active handlers are drained."""
        current_task = asyncio.current_task()
        cleanup_task = self._history_cleanup_task
        self._history_cleanup_task = None
        if cleanup_task is not None and cleanup_task is not current_task:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)

    async def _drain_history_write_tasks(self) -> None:
        """Await telemetry scheduled by handlers that finish during shutdown."""
        current_task = asyncio.current_task()
        write_tasks = tuple(
            task for task in self._history_write_tasks if task is not current_task
        )
        if write_tasks:
            await asyncio.gather(*write_tasks, return_exceptions=True)
        self._history_write_tasks.difference_update(write_tasks)
    async def _chat_provider_catalog(
        self,
    ) -> tuple[list[dict[str, str]], dict[str, Any], bool]:
        """Return configured chat providers without exposing provider secrets."""
        get_all_providers = getattr(
            getattr(self, "context", None),
            "get_all_providers",
            None,
        )
        if not callable(get_all_providers):
            return [], {}, False
        try:
            providers = get_all_providers()
            if inspect.isawaitable(providers):
                providers = await providers
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[repeater] chat provider catalog lookup failed ("
                f"{type(exc).__name__})",
            )
            return [], {}, False
        if not isinstance(providers, (list, tuple)):
            return [], {}, False
        options: list[dict[str, str]] = []
        provider_by_id: dict[str, Any] = {}
        for provider in providers:
            try:
                metadata = provider.meta()
                provider_id = getattr(metadata, "id", "")
                current_model = getattr(metadata, "model", "")
            except Exception:
                continue
            if not isinstance(provider_id, str) or not provider_id.strip():
                continue
            normalized_id = provider_id.strip()
            if normalized_id in provider_by_id:
                continue
            provider_by_id[normalized_id] = provider
            options.append(
                {
                    "id": normalized_id,
                    "label": normalized_id,
                    "current_model": (
                        current_model.strip()
                        if isinstance(current_model, str)
                        else ""
                    ),
                },
            )
        options.sort(key=lambda item: item["id"].casefold())
        return options, provider_by_id, True

    @staticmethod
    def _normalize_page_text(value: object, field_name: str) -> str:
        """Validate one bounded, optional Page configuration value."""
        if not isinstance(value, str):
            raise ValueError(f"{field_name} 必须是字符串")
        normalized = value.strip()
        if len(normalized) > MAX_CONFIG_TEXT_LENGTH:
            raise ValueError(
                f"{field_name} 不能超过 {MAX_CONFIG_TEXT_LENGTH} 个字符"
            )
        return normalized

    @staticmethod
    def _parse_page_number(value: object, field_name: str) -> int:
        """Parse a positive bounded-page query parameter."""
        if not isinstance(value, str) or not value.isdecimal():
            raise ValueError(f"{field_name} 必须是正整数")
        number = int(value)
        if number < 1:
            raise ValueError(f"{field_name} 必须是正整数")
        return number

    def _intelligent_console_shutdown_response(self):
        """Reject Page requests after the plugin has begun termination."""
        if not self.shutting_down:
            return None
        return error_response("插件正在停止，智能文案测试暂不可用。", status_code=503)

    async def _web_get_intelligent_console_config(self):
        """Return saved settings, runtime switches, and available provider choices."""
        shutdown_response = self._intelligent_console_shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        settings = self.state_service.settings
        provider_mode = settings.intelligent_interrupt_provider_mode
        provider_id = settings.intelligent_interrupt_provider_id
        load_provider_catalog = (
            provider_mode == INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT
            or request.query.get("include_provider_catalog") == "1"
        )
        if load_provider_catalog:
            options, provider_by_id, catalog_available = await self._chat_provider_catalog()
        else:
            options, provider_by_id, catalog_available = [], {}, False
        return json_response(
            {
                "status": "ok",
                "data": {
                    "provider_mode": provider_mode,
                    "provider_id": provider_id,
                    "model": settings.intelligent_interrupt_model,
                    "manual_api_base": settings.intelligent_interrupt_manual_api_base,
                    "manual_api_key_configured": bool(
                        settings.intelligent_interrupt_manual_api_key,
                    ),
                    "provider_exists": (
                        provider_mode
                        == INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE
                        or not provider_id
                        or (catalog_available and provider_id in provider_by_id)
                    ),
                    "provider_catalog_available": catalog_available,
                    "providers": options,
                    "features": {
                        "intelligent_repeat_enabled": (
                            settings.intelligent_interrupt_enabled
                        ),
                        "intelligent_mute_enabled": (
                            settings.intelligent_interrupt_mute_enabled
                        ),
                    },
                    "history": {
                        "available": self._history_available,
                        "message": self._history_storage_error,
                    },
                },
            },
        )

    async def _web_get_intelligent_console_models(self):
        """Return model candidates for one explicit configured chat provider."""
        shutdown_response = self._intelligent_console_shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        raw_provider_id = request.query.get("provider_id", "")
        try:
            provider_id = self._normalize_page_text(
                raw_provider_id,
                "provider_id",
            )
        except ValueError as exc:
            return error_response(str(exc))
        if not provider_id:
            return error_response("留空供应商时无法读取模型列表。")
        _, provider_by_id, catalog_available = await self._chat_provider_catalog()
        if not catalog_available:
            return error_response("聊天供应商列表暂不可用。", status_code=503)
        provider = provider_by_id.get(provider_id)
        if provider is None:
            return error_response("指定的聊天供应商不存在。", status_code=404)
        try:
            models = provider.get_models()
            if inspect.isawaitable(models):
                models = await models
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[repeater] 读取智能文案模型列表失败")
            return error_response(
                "无法读取模型列表，仍可手动输入模型 ID。",
                status_code=503,
            )
        if not isinstance(models, (list, tuple, set)):
            return error_response(
                "模型列表格式无效，仍可手动输入模型 ID。",
                status_code=503,
            )
        model_ids = {
            model.strip()
            for model in models
            if isinstance(model, str)
            and model.strip()
            and len(model.strip()) <= MAX_CONFIG_TEXT_LENGTH
        }
        settings = self.state_service.settings
        saved_model = ""
        if (
            settings.intelligent_interrupt_provider_id == provider_id
            and settings.intelligent_interrupt_model
        ):
            saved_model = settings.intelligent_interrupt_model
        candidates = sorted(model_ids, key=str.casefold)[:500]
        if saved_model and saved_model not in candidates:
            if len(candidates) == 500:
                candidates[-1] = saved_model
            else:
                candidates.append(saved_model)
            candidates.sort(key=str.casefold)
        return json_response(
            {
                "status": "ok",
                "data": {"provider_id": provider_id, "models": candidates},
            },
        )

    @staticmethod
    def _save_astrbot_config_snapshot(
        config: Any,
        updates: dict[str, str],
    ) -> tuple[bool, dict[str, Any]]:
        """Write one AstrBotConfig snapshot without exposing a revision race."""
        state_lock = config._save_state_lock
        with state_lock:
            previous_snapshot = deepcopy(dict(config))
            try:
                config.update(updates)
                snapshot = deepcopy(dict(config))
                revision = config._save_revision + 1
                object.__setattr__(config, "_save_revision", revision)
                committed = config._write_config_snapshot(snapshot, revision, 2)
                if not committed:
                    config.clear()
                    config.update(previous_snapshot)
            except BaseException:
                config.clear()
                config.update(previous_snapshot)
                raise
        return committed, snapshot

    @staticmethod
    async def _await_settled_config_save(awaitable):
        """Finish a config write before deciding whether to propagate cancellation."""
        task = asyncio.ensure_future(awaitable)
        was_cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                was_cancelled = True
        return task.result(), was_cancelled

    async def _save_intelligent_console_config(
        self,
        *,
        provider_id: str,
        model: str,
        provider_mode: str = INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
        manual_api_base: str = "",
        manual_api_key: str | None = None,
    ) -> bool:
        """Atomically persist shared intelligent-text provider settings."""
        async with self.state_service.save_lock:
            effective_manual_api_key = (
                self.state_service.settings.intelligent_interrupt_manual_api_key
                if manual_api_key is None
                else manual_api_key
            )
            updates = {
                "intelligent_interrupt_provider_mode": provider_mode,
                "intelligent_interrupt_provider_id": provider_id,
                "intelligent_interrupt_manual_api_base": manual_api_base,
                "intelligent_interrupt_manual_api_key": effective_manual_api_key,
                "intelligent_interrupt_model": model,
            }
            state_lock = getattr(self.config, "_save_state_lock", None)
            write_snapshot = getattr(self.config, "_write_config_snapshot", None)
            if (
                callable(write_snapshot)
                and hasattr(state_lock, "__enter__")
                and hasattr(state_lock, "__exit__")
            ):
                (committed, snapshot), was_cancelled = (
                    await self._await_settled_config_save(
                        asyncio.to_thread(
                            self._save_astrbot_config_snapshot,
                            self.config,
                            updates,
                        ),
                    )
                )
                if committed:
                    settings = build_settings(snapshot, logger)
                    settings.config = self.config
                    self.state_service.settings = settings
                if was_cancelled:
                    raise asyncio.CancelledError
                return committed

            previous_snapshot = deepcopy(dict(self.config))
            save_config_async = getattr(self.config, "save_config_async", None)
            try:
                if callable(save_config_async):
                    save_result, was_cancelled = (
                        await self._await_settled_config_save(
                            save_config_async(updates),
                        )
                    )
                    if save_result is False:
                        self.config.clear()
                        self.config.update(previous_snapshot)
                        if was_cancelled:
                            raise asyncio.CancelledError
                        return False
                else:
                    was_cancelled = False
                    self.config.update(updates)
                    save_config = getattr(self.config, "save_config", None)
                    if callable(save_config):
                        save_result = save_config()
                        if inspect.isawaitable(save_result):
                            _, was_cancelled = await self._await_settled_config_save(
                                save_result,
                            )
            except Exception:
                self.config.clear()
                self.config.update(previous_snapshot)
                raise
            self.state_service.settings = build_settings(self.config, logger)
            if was_cancelled:
                raise asyncio.CancelledError
        return True

    async def _web_save_intelligent_console_config(self):
        """Validate and persist the Page's shared provider settings."""
        shutdown_response = self._intelligent_console_shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        body = await request.json(default=None)
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象。")
        try:
            raw_provider_mode = body.get(
                "provider_mode",
                INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
            )
            if not isinstance(raw_provider_mode, str):
                raise ValueError(
                    "provider_mode 必须是 astrbot 或 openai_compatible",
                )
            provider_mode = normalize_intelligent_interrupt_provider_mode(
                raw_provider_mode,
            )
            if raw_provider_mode.strip() != provider_mode:
                raise ValueError(
                    "provider_mode 必须是 astrbot 或 openai_compatible",
                )
            provider_id = self._normalize_page_text(
                body.get("provider_id", ""),
                "provider_id",
            )
            model = self._normalize_page_text(body.get("model", ""), "model")
            manual_api_base = normalize_intelligent_interrupt_manual_api_base(
                body.get("manual_api_base", ""),
            )
            if manual_api_base is None:
                raise ValueError(
                    "manual_api_base 必须是长度不超过 "
                    f"{INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH} 个字符的 "
                    "有效 http 或 https URL，且不能包含账号、密码、查询串或片段",
                )
            manual_api_key: str | None = None
            if (
                provider_mode
                == INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE
                and "manual_api_key" in body
            ):
                manual_api_key = normalize_intelligent_interrupt_manual_api_key(
                    body["manual_api_key"],
                )
                if manual_api_key is None:
                    raise ValueError(
                        "manual_api_key 必须是长度不超过 "
                        f"{INTELLIGENT_INTERRUPT_MANUAL_API_KEY_MAX_LENGTH} "
                        "个字符的字符串",
                    )
        except ValueError as exc:
            return error_response(str(exc))
        if (
            provider_mode == INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT
            and provider_id
        ):
            _, provider_by_id, catalog_available = await self._chat_provider_catalog()
            if not catalog_available:
                return error_response("聊天供应商列表暂不可用。", status_code=503)
            if provider_id not in provider_by_id:
                return error_response("指定的聊天供应商不存在。", status_code=404)
        try:
            committed = await self._save_intelligent_console_config(
                provider_id=provider_id,
                model=model,
                provider_mode=provider_mode,
                manual_api_base=manual_api_base,
                manual_api_key=manual_api_key,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[repeater] 保存智能文案 Page 配置失败（"
                f"{type(exc).__name__}）",
            )
            return error_response("保存智能文案配置失败。", status_code=500)
        if not committed:
            return error_response("配置正在被其他操作更新，请刷新后重试。", status_code=409)
        saved_settings = self.state_service.settings
        return json_response(
            {
                "status": "ok",
                "data": {
                    "provider_mode": provider_mode,
                    "provider_id": provider_id,
                    "manual_api_base": manual_api_base,
                    "model": model,
                    "manual_api_key_configured": bool(
                        saved_settings.intelligent_interrupt_manual_api_key,
                    ),
                },
            },
        )

    async def _record_manual_generation(
        self,
        *,
        kind: Literal["repeat", "mute"],
        result: IntelligentGenerationResult,
    ) -> None:
        """Synchronously retain a Page test so an immediate history refresh sees it."""
        outcome: Literal["success", "fallback", "failed"] = (
            "success" if result.result_code == "success" else "failed"
        )
        await self._append_history_record(
            self._history_record_for_generation(
                result,
                kind=kind,
                source="manual_test",
                outcome=outcome,
            ),
        )

    async def _web_run_intelligent_console_test(
        self,
        *,
        kind: Literal["repeat", "mute"],
    ):
        """Run a fixed, non-destructive generation test for one intelligent feature."""
        shutdown_response = self._intelligent_console_shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        settings = self.state_service.settings
        provider_mode = settings.intelligent_interrupt_provider_mode
        provider_id = settings.intelligent_interrupt_provider_id
        model = settings.intelligent_interrupt_model
        if provider_mode == INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT:
            if not provider_id:
                result = IntelligentGenerationResult(
                    completion=None,
                    provider_id=None,
                    model=model,
                    latency_ms=0,
                    result_code="provider_resolution_failed",
                )
                await self._record_manual_generation(kind=kind, result=result)
                return error_response(
                    "留空供应商会跟随触发会话；页面测试需要先保存一个明确的聊天供应商。",
                    status_code=409,
                    data={"code": result.result_code},
                )
            _, provider_by_id, catalog_available = await self._chat_provider_catalog()
            if not catalog_available or provider_id not in provider_by_id:
                result = IntelligentGenerationResult(
                    completion=None,
                    provider_id=provider_id if catalog_available else None,
                    model=model,
                    latency_ms=0,
                    result_code="provider_resolution_failed",
                )
                await self._record_manual_generation(kind=kind, result=result)
                return error_response(
                    "保存的聊天供应商当前不可用，请重新选择后保存。",
                    status_code=409 if catalog_available else 503,
                    data={"code": result.result_code},
                )
        if kind == "repeat":
            prompt = "被复读的内容：这是智能打断测试使用的固定示例消息。"
            system_prompt = settings.intelligent_interrupt_prompt
            feature_name = "智能打断测试"
        else:
            prompt = "被禁言用户：测试用户\n禁言时长：60秒"
            system_prompt = settings.intelligent_interrupt_mute_prompt
            feature_name = "智能禁言提示测试"
        result = await self._run_intelligent_generation(
            prompt=prompt,
            system_prompt=system_prompt,
            settings=settings,
            unified_msg_origin=None,
            feature_name=feature_name,
        )
        await self._record_manual_generation(kind=kind, result=result)
        if result.result_code != "success":
            if (
                provider_mode
                == INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE
                and result.result_code == "provider_resolution_failed"
            ):
                return error_response(
                    "自定义 OpenAI兼容直连模式需要保存 API Base URL、API Key 和模型。",
                    status_code=409,
                    data={"code": result.result_code},
                )
            return error_response(
                "智能文案生成失败，请检查供应商和模型配置。",
                status_code=502,
                data={"code": result.result_code},
            )
        return json_response(
            {
                "status": "ok",
                "data": {
                    "text": result.completion,
                    "provider_id": result.provider_id,
                    "model": result.model,
                    "latency_ms": result.latency_ms,
                },
            },
        )

    async def _web_test_intelligent_repeat(self):
        """Run only the repeat prompt; never send a group message."""
        return await self._web_run_intelligent_console_test(kind="repeat")

    async def _web_test_intelligent_mute(self):
        """Run only the mute-notice prompt; never call moderation APIs."""
        return await self._web_run_intelligent_console_test(kind="mute")

    async def _web_get_intelligent_history(self):
        """Serve bounded, time-windowed metadata-only intelligent history."""
        shutdown_response = self._intelligent_console_shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        if not self._history_available:
            return error_response(
                self._history_storage_error or "智能记录存储不可用。",
                status_code=503,
            )
        raw_window = request.query.get("window", "day")
        raw_kind = request.query.get("kind", "all")
        try:
            if raw_window not in {"day", "24h", "2d", "3d", "7d"}:
                raise ValueError("window 必须是 day、24h、2d、3d 或 7d。")
            if raw_kind not in {"all", "repeat", "mute"}:
                raise ValueError("kind 必须是 all、repeat 或 mute。")
            page = self._parse_page_number(request.query.get("page", "1"), "page")
            if page > MAX_HISTORY_PAGE:
                raise ValueError(f"page 不能超过 {MAX_HISTORY_PAGE}。")
            page_size = self._parse_page_number(
                request.query.get("page_size", "50"),
                "page_size",
            )
            if page_size > 100:
                raise ValueError("page_size 不能超过 100。")
        except ValueError as exc:
            return error_response(str(exc))
        try:
            history_page = await self.history_store.query(
                window=raw_window,
                kind=None if raw_kind == "all" else raw_kind,
                page=page,
                page_size=page_size,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[repeater] 读取智能记录失败")
            return error_response("智能记录存储不可用。", status_code=503)
        return json_response({"status": "ok", "data": history_page.to_dict()})

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        """返回事件所属群的稳定字符串键。

        Args:
            event: 接收到的 AstrBot 群消息事件。

        Returns:
            用于索引群状态的群 ID 字符串。
        """
        return str(event.get_group_id())

    @staticmethod
    async def _can_manage_group(event: AstrMessageEvent) -> bool:
        """判断事件发送者是否有管理本群开关的权限。

        Args:
            event: 需要检查权限的群消息事件。

        Returns:
            发送者为 AstrBot 管理员、群主或群管理员时为 True。
        """
        if event.is_admin():
            return True
        try:
            group = await event.get_group()
        except Exception as exc:
            logger.warning(f"[repeater] 获取群成员权限失败: {exc}")
            return False
        if group is None:
            return False
        sender_id = str(event.get_sender_id())
        if sender_id == str(group.group_owner or ""):
            return True
        return sender_id in {str(user_id) for user_id in group.group_admins or []}

    @staticmethod
    async def _is_bot_admin(event: AstrMessageEvent) -> bool:
        """判断 bot 是否在本群具有管理员权限。

        Args:
            event: 群消息事件。

        Returns:
            bot 为群主或群管理员时为 True。
        """
        try:
            group = await event.get_group()
        except Exception as exc:
            logger.warning(f"[repeater] 获取群信息失败: {exc}")
            return False
        if group is None:
            return False
        bot_id = str(event.get_self_id())
        if bot_id == str(group.group_owner or ""):
            return True
        return bot_id in {str(user_id) for user_id in group.group_admins or []}

    def _begin_handler(self) -> asyncio.Task | None:
        """登记当前处理协程，或在终止期间拒绝它。

        Returns:
            当前 asyncio 任务；插件正在终止时返回 None。

        Raises:
            RuntimeError: 当前代码未在 asyncio 任务中执行。
        """
        if self.shutting_down:
            return None
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("消息处理器必须在 asyncio Task 中运行")
        self.active_handler_tasks.add(task)
        return task

    def _finish_handler(self, task: asyncio.Task) -> None:
        """取消登记一个已经结束的处理任务。

        Args:
            task: 由 _begin_handler 返回的处理任务。
        """
        self.active_handler_tasks.discard(task)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """处理一条普通群消息，且不唤起默认 LLM。

        Args:
            event: AstrBot 分发的群消息事件。
        """
        task = self._begin_handler()
        if task is None:
            return
        try:
            await self._handle_group_message(event)
        finally:
            self._finish_handler(task)

    async def _handle_group_message(self, event: AstrMessageEvent) -> None:
        """处理单条符合条件的群消息，并按发送结果提交或回滚状态。

        Args:
            event: 已通过群消息过滤器的 AstrBot 事件。

        Raises:
            asyncio.CancelledError: 消息处理协程在状态保存或发送时被取消。
            Exception: 状态保存、消息发送或发送后的提交失败；仅发送失败会先回滚。
        """
        if not event.get_group_id() or event.is_at_or_wake_command:
            return
        if event.get_sender_id() == event.get_self_id():
            return

        group_key = self._group_key(event)
        if not await self.state_service.is_any_repeat_mode_enabled(group_key):
            return

        message = repeatable_message(event)
        if message is None:
            return
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        attempt = await self.state_service.process_message(
            group_key,
            event.get_sender_id(),
            message_id,
            message,
        )
        if attempt is None:
            return

        response_text = attempt.response_text
        settings = self.state_service.settings
        if (
            attempt.interrupted
            and settings.intelligent_interrupt_enabled
        ):
            try:
                response_text = await self._generate_intelligent_interrupt(
                    event,
                    message,
                    attempt.response_text,
                    settings=settings,
                )
            except asyncio.CancelledError:
                try:
                    await self.state_service.rollback_attempt(group_key, attempt)
                except asyncio.CancelledError:
                    logger.exception(
                        f"[repeater] {group_key} 智能打断生成取消，回滚保存也被取消；"
                        "已保留 pending 抑制",
                    )
                except Exception:
                    logger.exception(
                        f"[repeater] {group_key} 智能打断生成取消，回滚保存失败；"
                        "已保留 pending 抑制",
                    )
                else:
                    logger.info(
                        f"[repeater] {group_key} 智能打断生成取消，状态已回滚",
                    )
                raise

        if attempt.interrupted:
            result = event.plain_result(response_text)
        else:
            result = (
                event.chain_result(list(attempt.response_chain))
                if attempt.response_chain
                else event.plain_result(response_text)
            )
        try:
            await event.send(result)
        except Exception:
            try:
                await self.state_service.rollback_attempt(group_key, attempt)
            except Exception:
                logger.exception(
                    f"[repeater] {group_key} 复读发送失败，回滚保存也失败；"
                    "已保留 pending 抑制",
                )
            else:
                logger.exception(f"[repeater] {group_key} 复读发送失败，状态已回滚")
            raise

        event.stop_event()
        await self.state_service.commit_attempt(group_key, attempt)
        action = "打断复读" if attempt.interrupted else "复读"
        logger.info(
            f"[repeater] {group_key} 触发{action}: {response_text[:20]}",
        )
        if attempt.interrupted:
            await self._handle_interrupt_mute(event, group_key, attempt)

    async def _request_manual_openai_compatible_completion(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        system_prompt: str,
        prompt: str,
    ) -> str | None:
        """Request one non-streaming OpenAI-compatible chat completion."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=120,
            max_retries=0,
        )
        request_error: BaseException | None = None
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )
            choices = getattr(response, "choices", None)
            if not isinstance(choices, (list, tuple)) or not choices:
                return None
            message = getattr(choices[0], "message", None)
            if getattr(message, "role", None) != "assistant":
                return None
            content = getattr(message, "content", None)
            if not isinstance(content, str):
                return None
            completion = content.strip()
            if not completion:
                return None
            if api_key in completion:
                logger.warning(
                    "[repeater] manual OpenAI-compatible response contained API key",
                )
                return None
            return completion
        except BaseException as exc:
            request_error = exc
            raise
        finally:
            try:
                await client.close()
            except asyncio.CancelledError:
                if request_error is None:
                    raise
                logger.warning(
                    "[repeater] manual OpenAI-compatible client close cancelled",
                )
            except Exception as exc:
                logger.warning(
                    "[repeater] manual OpenAI-compatible client close failed ("
                    f"{type(exc).__name__})",
                )


    async def _run_intelligent_generation(
        self,
        *,
        prompt: str,
        system_prompt: str,
        settings: RepeaterSettings,
        unified_msg_origin: str | None,
        feature_name: str,
    ) -> IntelligentGenerationResult:
        """Generate one completion without fallback text or side effects."""
        started_at = time.perf_counter_ns()
        requested_model = settings.intelligent_interrupt_model.strip()
        if (
            settings.intelligent_interrupt_provider_mode
            == INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE
        ):
            manual_api_base = settings.intelligent_interrupt_manual_api_base
            manual_api_key = settings.intelligent_interrupt_manual_api_key
            if not (manual_api_base and manual_api_key and requested_model):
                return IntelligentGenerationResult(
                    completion=None,
                    provider_id=MANUAL_OPENAI_COMPATIBLE_PROVIDER_ID,
                    model=requested_model,
                    latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                    result_code="provider_resolution_failed",
                )
            try:
                completion_text = await self._request_manual_openai_compatible_completion(
                    api_base=manual_api_base,
                    api_key=manual_api_key,
                    model=requested_model,
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[repeater] {feature_name} manual OpenAI-compatible "
                    f"request failed ({type(exc).__name__})",
                )
                return IntelligentGenerationResult(
                    completion=None,
                    provider_id=MANUAL_OPENAI_COMPATIBLE_PROVIDER_ID,
                    model=requested_model,
                    latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                    result_code="request_failed",
                )
            if completion_text is None:
                logger.warning(
                    f"[repeater] {feature_name} manual OpenAI-compatible "
                    "response was invalid",
                )
                return IntelligentGenerationResult(
                    completion=None,
                    provider_id=MANUAL_OPENAI_COMPATIBLE_PROVIDER_ID,
                    model=requested_model,
                    latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                    result_code="invalid_response",
                )
            return IntelligentGenerationResult(
                completion=completion_text,
                provider_id=MANUAL_OPENAI_COMPATIBLE_PROVIDER_ID,
                model=requested_model,
                latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                result_code="success",
            )

        chat_provider_id = settings.intelligent_interrupt_provider_id.strip()
        if not chat_provider_id:
            if not unified_msg_origin:
                return IntelligentGenerationResult(
                    completion=None,
                    provider_id=None,
                    model=requested_model,
                    latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                    result_code="provider_resolution_failed",
                )
            try:
                current_provider_id = self.context.get_current_chat_provider_id(
                    unified_msg_origin,
                )
                if inspect.isawaitable(current_provider_id):
                    current_provider_id = await current_provider_id
                if not isinstance(current_provider_id, str):
                    raise ValueError("current provider ID is not a string")
                chat_provider_id = current_provider_id.strip()
                if not chat_provider_id:
                    raise ValueError("current provider ID is empty")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[repeater] {feature_name} provider resolution failed "
                    f"({type(exc).__name__})",
                )
                return IntelligentGenerationResult(
                    completion=None,
                    provider_id=None,
                    model=requested_model,
                    latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                    result_code="provider_resolution_failed",
                )
        model_kwargs = {"model": requested_model} if requested_model else {}
        try:
            response = await self.context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                **model_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"[repeater] {feature_name} generation request failed "
                f"({type(exc).__name__})",
            )
            return IntelligentGenerationResult(
                completion=None,
                provider_id=chat_provider_id,
                model=requested_model,
                latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                result_code="request_failed",
            )
        if getattr(response, "role", None) != "assistant":
            logger.warning(f"[repeater] {feature_name} received non-assistant response")
            return IntelligentGenerationResult(
                completion=None,
                provider_id=chat_provider_id,
                model=requested_model,
                latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                result_code="invalid_response",
            )
        completion_text = str(getattr(response, "completion_text", "") or "").strip()
        if not completion_text:
            logger.warning(f"[repeater] {feature_name} received empty response")
            return IntelligentGenerationResult(
                completion=None,
                provider_id=chat_provider_id,
                model=requested_model,
                latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
                result_code="invalid_response",
            )
        return IntelligentGenerationResult(
            completion=completion_text,
            provider_id=chat_provider_id,
            model=requested_model,
            latency_ms=(time.perf_counter_ns() - started_at) // 1_000_000,
            result_code="success",
        )

    @staticmethod
    def _history_record_for_generation(
        result: IntelligentGenerationResult,
        *,
        kind: Literal["repeat", "mute"],
        source: Literal["runtime", "manual_test"],
        outcome: Literal["success", "fallback", "failed"],
        group_id: str | None = None,
        mute_duration_seconds: int | None = None,
    ) -> IntelligentActionRecord:
        """Build the content-free telemetry row for a completed generation."""
        return IntelligentActionRecord(
            occurred_at_ms=time.time_ns() // 1_000_000,
            kind=kind,
            source=source,
            outcome=outcome,
            provider_id=result.provider_id,
            model=result.model,
            group_id=group_id,
            mute_duration_seconds=mute_duration_seconds,
            latency_ms=result.latency_ms,
            failure_code=(
                None if result.result_code == "success" else result.result_code
            ),
        )

    async def _generate_intelligent_text(
        self,
        event: AstrMessageEvent,
        settings: RepeaterSettings,
        *,
        prompt: str,
        system_prompt: str,
        fallback_text: str,
        feature_name: str,
        history_kind: Literal["repeat", "mute"],
        mute_duration_seconds: int | None = None,
    ) -> str:
        """Generate runtime text and asynchronously retain its safe outcome."""
        result = await self._run_intelligent_generation(
            prompt=prompt,
            system_prompt=system_prompt,
            settings=settings,
            unified_msg_origin=getattr(event, "unified_msg_origin", None),
            feature_name=feature_name,
        )
        outcome: Literal["success", "fallback", "failed"] = (
            "success" if result.result_code == "success" else "fallback"
        )
        self._schedule_history_record(
            self._history_record_for_generation(
                result,
                kind=history_kind,
                source="runtime",
                outcome=outcome,
                group_id=self._group_key(event),
                mute_duration_seconds=mute_duration_seconds,
            ),
        )
        return result.completion if result.completion is not None else fallback_text

    async def _generate_intelligent_interrupt(
        self,
        event: AstrMessageEvent,
        message: RepeatableMessage,
        fallback_text: str,
        *,
        settings: RepeaterSettings,
    ) -> str:
        """Generate intelligent interrupt text or retain the existing fallback."""
        return await self._generate_intelligent_text(
            event,
            settings=settings,
            prompt=f"被复读的内容：{message.text or message.summary}",
            system_prompt=settings.intelligent_interrupt_prompt,
            fallback_text=fallback_text,
            feature_name="智能打断",
            history_kind="repeat",
        )

    async def _generate_intelligent_interrupt_mute_notice(
        self,
        event: AstrMessageEvent,
        *,
        sender_name: str,
        duration: int,
        fallback_text: str,
        settings: RepeaterSettings,
    ) -> str:
        """Generate intelligent mute notice or retain the existing fallback."""
        return await self._generate_intelligent_text(
            event,
            settings=settings,
            prompt=f"被禁言用户：{sender_name}\n禁言时长：{duration}秒",
            system_prompt=settings.intelligent_interrupt_mute_prompt,
            fallback_text=fallback_text,
            feature_name="智能禁言提示",
            history_kind="mute",
            mute_duration_seconds=duration,
        )

    async def _handle_interrupt_mute(
        self,
        event: AstrMessageEvent,
        group_key: str,
        attempt: RepeatAttempt,
    ) -> None:
        """处理打断复读后的禁言逻辑。

        Args:
            event: 触发打断的群消息事件。
            group_key: 群状态键。
            attempt: 已提交的打断复读尝试。
        """
        if not self.state_service.is_interrupt_mute_enabled(group_key):
            return
        if not await self._is_bot_admin(event):
            return

        settings = self.state_service.settings
        if random.random() >= settings.interrupt_mute_probability:
            return

        duration = random.randint(
            settings.interrupt_mute_duration_min,
            settings.interrupt_mute_duration_max,
        )
        try:
            bot = getattr(event, "bot", None)
            call_action = getattr(bot, "call_action", None)
            if not callable(call_action):
                logger.warning(
                    f"[repeater] {group_key} 事件无 aiocqhttp bot 客户端，无法执行禁言",
                )
                return
            payload = {
                "group_id": int(group_key),
                "user_id": int(attempt.sender_id),
                "duration": duration,
            }
            self_id = getattr(getattr(event, "message_obj", None), "self_id", None)
            if self_id:
                payload["self_id"] = self_id
            await call_action("set_group_ban", **payload)
        except Exception as exc:
            logger.warning(f"[repeater] {group_key} 禁言失败: {exc}")
            return

        fallback_text = random.choice(settings.interrupt_mute_texts)
        sender_name = str(event.get_sender_name() or attempt.sender_id)
        fallback_text = fallback_text.replace("{user}", sender_name).replace(
            "{time}",
            str(duration),
        )
        mute_text = fallback_text
        if settings.intelligent_interrupt_mute_enabled:
            mute_text = await self._generate_intelligent_interrupt_mute_notice(
                event,
                sender_name=sender_name,
                duration=duration,
                fallback_text=fallback_text,
                settings=settings,
            )
        try:
            await event.send(event.plain_result(mute_text))
        except Exception:
            logger.exception(f"[repeater] {group_key} 禁言提示发送失败")

        logger.info(
            f"[repeater] {group_key} 打断复读禁言: 用户 {attempt.sender_id} "
            f"禁言 {duration}s",
        )

    @filter.command("自动复读", alias={"repeatMsg"})
    async def repeater_command(
        self,
        event: AstrMessageEvent,
        action: str = "帮助",
    ):
        """查看或修改本群的自动复读开关。

        Args:
            event: 触发命令的 AstrBot 事件。
            action: 子命令，可为查看、开启、关闭或帮助。

        Yields:
            由事件构造的纯文本命令响应。
        """
        task = self._begin_handler()
        if task is None:
            return
        try:
            reply = await self._handle_toggle_command(
                event,
                action.strip(),
                interrupt=False,
            )
        finally:
            self._finish_handler(task)
        yield event.plain_result(reply)

    @filter.command("打断复读", alias={"interruptRepeat"})
    async def interrupt_command(
        self,
        event: AstrMessageEvent,
        action: str = "帮助",
    ):
        """查看或修改本群的打断复读开关。

        Args:
            event: 触发命令的 AstrBot 事件。
            action: 子命令，可为查看、开启、关闭或帮助。

        Yields:
            由事件构造的纯文本命令响应。
        """
        task = self._begin_handler()
        if task is None:
            return
        try:
            reply = await self._handle_toggle_command(
                event,
                action.strip(),
                interrupt=True,
            )
        finally:
            self._finish_handler(task)
        yield event.plain_result(reply)

    async def _handle_toggle_command(
        self,
        event: AstrMessageEvent,
        action: str,
        *,
        interrupt: bool,
    ) -> str:
        """执行复读或打断复读的开关子命令。

        Args:
            event: 触发命令的 AstrBot 事件。
            action: 已去除首尾空白的子命令。
            interrupt: 为 True 时操作打断复读，否则操作普通复读。

        Returns:
            将发送给用户的状态、帮助或错误文本。
        """
        if not event.get_group_id():
            return "该指令仅在群聊中可用。"

        group_key = self._group_key(event)
        if action in {"开启", "关闭"} and not await self._can_manage_group(event):
            return PERMISSION_ERROR

        settings = self.state_service.settings
        noun = "打断复读" if interrupt else "自动复读"
        if action == "查看":
            enabled = (
                await self.state_service.interrupt_enabled_for(group_key)
                if interrupt
                else await self.state_service.repeat_enabled_for(group_key)
            )
            status = "开启" if enabled else "关闭"
            if interrupt:
                base_info = (
                    f"本群{noun}：{status}\n"
                    f"打断概率：{settings.interrupt_probability * 100:g}%\n"
                    f"可选文本：{len(settings.interrupt_texts)} 条"
                )
                if enabled and self.state_service.is_interrupt_mute_enabled(group_key):
                    mute_info = (
                        "\n\n打断复读禁言：开启\n"
                        f"禁言概率：{settings.interrupt_mute_probability * 100:g}%\n"
                        "禁言时长："
                        f"{settings.interrupt_mute_duration_min}-"
                        f"{settings.interrupt_mute_duration_max}秒\n"
                        f"提示文本：{len(settings.interrupt_mute_texts)} 条"
                    )
                else:
                    mute_info = "\n\n打断复读禁言：关闭"
                return base_info + mute_info
            return (
                f"本群{noun}：{status}\n"
                f"触发阈值：{settings.repeat_threshold} 名独立用户\n"
                f"触发概率：{settings.repeat_probability * 100:g}%"
            )

        if action in {"开启", "关闭"}:
            enabled = action == "开启"
            already_enabled = (
                await self.state_service.set_interrupt_enabled(group_key, enabled)
                if interrupt
                else await self.state_service.set_repeat_enabled(group_key, enabled)
            )
            status = "开启" if enabled else "关闭"
            return (
                f"本群{noun}已经是{status}状态。"
                if already_enabled
                else f"已在本群{status}{noun}。"
            )

        if action == "帮助":
            return (
                "指令用法：\n"
                f"{noun} 查看 —— 查看本群是否开启该功能\n"
                f"{noun} 开启 —— 在本群开启该功能\n"
                f"{noun} 关闭 —— 在本群关闭该功能\n"
                "开启/关闭仅限 AstrBot 管理员、群主或群管理员\n"
                f"{noun} 帮助 —— 查看命令帮助与用法"
            )

        return f"未知子命令：{action}\n发送「{noun} 帮助」查看用法。"

    async def terminate(self) -> None:
        """Stop cleanup, drain handlers and telemetry, then persist group state."""
        self.shutting_down = True
        await self._stop_history_cleanup_task()
        current_task = asyncio.current_task()
        active_tasks = tuple(
            task for task in self.active_handler_tasks if task is not current_task
        )
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await self._drain_history_write_tasks()
        await self.state_service.save()
