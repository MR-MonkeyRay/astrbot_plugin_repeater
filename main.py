"""将 AstrBot 群事件适配为可持久化的复读事务。

本模块处理框架事件、权限检查和消息发送；连续消息判定及状态更新由
`RepeaterStateService` 负责。
"""

import asyncio
import random
import time
from typing import Literal

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

if __package__:
    from .intelligent_history import (
        IntelligentActionRecord,
        IntelligentHistoryStore,
    )
    from .llm_client import (
        MANUAL_OPENAI_COMPATIBLE_PROVIDER_ID,
        IntelligentGenerationResult,
        IntelligentTextClient,
        build_interrupt_prompt,
        build_mute_prompt,
    )
    from .repeater_config import RepeaterSettings, build_settings
    from .repeater_messages import RepeatableMessage, repeatable_message
    from .repeater_service import RepeatAttempt, RepeaterStateService
    from .web_console import IntelligentConsoleApi
else:
    from intelligent_history import IntelligentActionRecord, IntelligentHistoryStore
    from llm_client import (
        MANUAL_OPENAI_COMPATIBLE_PROVIDER_ID,
        IntelligentGenerationResult,
        IntelligentTextClient,
        build_interrupt_prompt,
        build_mute_prompt,
    )
    from repeater_config import RepeaterSettings, build_settings
    from repeater_messages import RepeatableMessage, repeatable_message
    from repeater_service import RepeatAttempt, RepeaterStateService
    from web_console import IntelligentConsoleApi

__all__ = ["MANUAL_OPENAI_COMPATIBLE_PROVIDER_ID", "PERMISSION_ERROR", "RepeaterPlugin"]


PERMISSION_ERROR = (
    "权限错误：仅 AstrBot 管理员、群主或群管理员可以开启或关闭。"
    "请向本群管理员或群主求助。"
)

PLUGIN_NAME = "astrbot_plugin_repeater"
HISTORY_CLEANUP_INTERVAL_SECONDS = 60 * 60
MUTE_SUPPORTED_PLATFORM = "aiocqhttp"


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
        self.llm_client = IntelligentTextClient(context)
        self.console = IntelligentConsoleApi(self)

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
        console = self.console
        register_web_api(
            f"{prefix}/config",
            track(console.get_config),
            ["GET"],
            "读取LLM调用测试配置",
        )
        register_web_api(
            f"{prefix}/models",
            track(console.get_models),
            ["GET"],
            "读取LLM供应商模型列表",
        )
        register_web_api(
            f"{prefix}/config",
            track(console.save_config),
            ["POST"],
            "保存LLM调用测试配置",
        )
        register_web_api(
            f"{prefix}/test/repeat",
            track(console.test_repeat),
            ["POST"],
            "执行智能打断LLM调用测试",
        )
        register_web_api(
            f"{prefix}/test/mute",
            track(console.test_mute),
            ["POST"],
            "执行智能禁言提示LLM调用测试",
        )
        register_web_api(
            f"{prefix}/history",
            track(console.get_history),
            ["GET"],
            "读取调用记录",
        )
        register_web_api(
            f"{prefix}/history/clear",
            track(console.clear_history),
            ["POST"],
            "清理调用记录",
        )

    def _track_intelligent_console_handler(self, handler):
        """Bind a Page request to the same shutdown drain as event handlers."""

        async def tracked_handler():
            task = self._begin_handler()
            if task is None:
                return self.console.shutdown_response()
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
                logger.exception("[repeater] 调用记录数据目录不可用")
                self._history_storage_error = "调用记录存储不可用。"
                return
            self.history_store = IntelligentHistoryStore(data_dir)
        try:
            await self.history_store.initialize()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[repeater] 调用记录存储初始化失败")
            self._history_storage_error = "调用记录存储不可用。"
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
            logger.exception("[repeater] 调用记录清理失败")
            self._history_storage_error = "调用记录存储不可用。"
            return
        self._history_storage_error = None
        if deleted:
            logger.info(f"[repeater] 已清理 {deleted} 条过期调用记录")

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
            logger.exception("[repeater] 调用记录写入失败")
            self._history_storage_error = "调用记录存储不可用。"
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
        if attempt.interrupted and settings.intelligent_interrupt_enabled:
            try:
                response_text = await self._generate_intelligent_interrupt(
                    event,
                    message,
                    attempt.response_text,
                    settings=settings,
                    repeat_user_count=attempt.repeat_user_count,
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
            await self._handle_interrupt_mute(event, group_key, attempt, message)

    @staticmethod
    def _history_record_for_generation(
        result: IntelligentGenerationResult,
        *,
        kind: Literal["repeat", "mute"],
        source: Literal["runtime", "manual_test"],
        outcome: Literal["success", "fallback", "failed"],
        group_id: str | None = None,
        mute_duration_seconds: int | None = None,
        message_text: str | None = None,
        prompt: str | None = None,
        repeat_user_count: int | None = None,
    ) -> IntelligentActionRecord:
        """Build one history record for a completed generation."""
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
            message_text=message_text,
            prompt=prompt,
            completion=result.completion,
            repeat_user_count=repeat_user_count,
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
        message_text: str | None = None,
        repeat_user_count: int | None = None,
    ) -> str:
        """Generate runtime text and asynchronously retain its call details."""
        result = await self.llm_client.generate(
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
                message_text=message_text,
                prompt=prompt,
                repeat_user_count=repeat_user_count,
            ),
        )
        return result.completion if result.completion is not None else fallback_text

    async def _generate_intelligent_interrupt(
        self,
        event: AstrMessageEvent,
        message: RepeatableMessage,
        fallback_text: str,
        repeat_user_count: int,
        *,
        settings: RepeaterSettings,
    ) -> str:
        """Generate intelligent interrupt text or retain the existing fallback."""
        message_text = message.text or message.summary
        return await self._generate_intelligent_text(
            event,
            settings=settings,
            prompt=build_interrupt_prompt(message_text),
            system_prompt=settings.intelligent_interrupt_prompt,
            fallback_text=fallback_text,
            feature_name="智能打断",
            history_kind="repeat",
            message_text=message_text,
            repeat_user_count=repeat_user_count,
        )

    async def _generate_intelligent_interrupt_mute_notice(
        self,
        event: AstrMessageEvent,
        *,
        sender_name: str,
        message_text: str,
        repeat_user_count: int,
        duration: int,
        fallback_text: str,
        settings: RepeaterSettings,
    ) -> str:
        """Generate intelligent mute notice or retain the existing fallback."""
        return await self._generate_intelligent_text(
            event,
            settings=settings,
            prompt=build_mute_prompt(sender_name, duration),
            system_prompt=settings.intelligent_interrupt_mute_prompt,
            fallback_text=fallback_text,
            feature_name="智能禁言提示",
            history_kind="mute",
            mute_duration_seconds=duration,
            message_text=message_text,
            repeat_user_count=repeat_user_count,
        )

    async def _handle_interrupt_mute(
        self,
        event: AstrMessageEvent,
        group_key: str,
        attempt: RepeatAttempt,
        message: RepeatableMessage,
    ) -> None:
        """处理打断复读后的禁言逻辑。

        Args:
            event: 触发打断的群消息事件。
            group_key: 群状态键。
            attempt: 已提交的打断复读尝试。
        """
        if not self.state_service.is_interrupt_mute_enabled(group_key):
            return
        if event.get_platform_name() != MUTE_SUPPORTED_PLATFORM:
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
            call_action = getattr(getattr(event, "bot", None), "call_action", None)
            if not callable(call_action):
                logger.warning(f"[repeater] {group_key} 缺少 OneBot 客户端，无法禁言")
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
                message_text=message.text or message.summary,
                repeat_user_count=attempt.repeat_user_count,
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
                    f"最小打断触发人数：{settings.interrupt_threshold} 名独立用户\n"
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
                f"最小复读触发人数：{settings.repeat_threshold} 名独立用户\n"
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
