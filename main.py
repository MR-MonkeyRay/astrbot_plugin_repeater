import asyncio
import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

DEFAULT_INTERRUPT_TEXTS = (
    "复读机过热，已强制散热！",
    "检测到人类回声，正在切断循环。",
    "别复读了，再读群聊就要卡带了！",
)


@dataclass(slots=True)
class GroupRepeaterState:
    """一个群的复读状态。"""

    enabled_override: bool | None = None
    interrupt_enabled_override: bool | None = None
    last_fingerprint: str = ""
    repeated_users: set[str] = field(default_factory=set)
    repeated_fingerprints: set[str] = field(default_factory=set)
    pending_fingerprints: set[str] = field(default_factory=set)
    last_message_id: str = ""

    @classmethod
    def from_dict(cls, raw_state: dict[str, Any]) -> "GroupRepeaterState":
        """从持久化字典恢复状态。"""
        enabled_override = raw_state.get("enabled_override")
        return cls(
            enabled_override=(
                enabled_override if isinstance(enabled_override, bool) else None
            ),
            interrupt_enabled_override=(
                raw_state.get("interrupt_enabled_override")
                if isinstance(raw_state.get("interrupt_enabled_override"), bool)
                else None
            ),
            last_fingerprint=str(raw_state.get("last_fingerprint", "")),
            repeated_users=cls._load_string_set(
                raw_state.get("repeated_users", []),
            ),
            repeated_fingerprints=cls._load_string_set(
                raw_state.get("repeated_fingerprints", []),
            ),
            pending_fingerprints=cls._load_string_set(
                raw_state.get("pending_fingerprints", []),
            ),
            last_message_id=str(raw_state.get("last_message_id", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定、可序列化的持久化字典。"""
        return {
            "enabled_override": self.enabled_override,
            "interrupt_enabled_override": self.interrupt_enabled_override,
            "last_fingerprint": self.last_fingerprint,
            "repeated_users": sorted(self.repeated_users),
            "repeated_fingerprints": sorted(self.repeated_fingerprints),
            "pending_fingerprints": sorted(self.pending_fingerprints),
            "last_message_id": self.last_message_id,
        }

    @staticmethod
    def _load_string_set(value: Any) -> set[str]:
        if not isinstance(value, list):
            raise TypeError("集合字段必须是列表")
        return {str(item) for item in value if isinstance(item, (str, int))}


@dataclass(frozen=True, slots=True)
class RepeatAttempt:
    """已持久化、等待发送的复读尝试。"""

    fingerprint: str
    message_id: str
    previous_message_id: str
    response_text: str
    interrupted: bool


class RepeaterPlugin(Star):
    """按群统计独立用户并自动复读的娱乐插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}

        threshold = self.config.get("repeat_threshold", 3)
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or threshold < 2
        ):
            logger.warning(
                f"[repeater] repeat_threshold 非法({threshold})，回退为 3",
            )
            threshold = 3
        self.repeat_threshold: int = threshold

        probability = self.config.get("repeat_probability", 0.3)
        if (
            not isinstance(probability, (int, float))
            or isinstance(probability, bool)
            or not 0.0 <= probability <= 1.0
        ):
            logger.warning(
                f"[repeater] repeat_probability 非法({probability})，回退为 0.3",
            )
            probability = 0.3
        self.repeat_probability = float(probability)

        default_enabled = self.config.get("default_enabled", True)
        if not isinstance(default_enabled, bool):
            logger.warning(
                f"[repeater] default_enabled 非法({default_enabled})，回退为 True",
            )
            default_enabled = True
        self.default_enabled = default_enabled

        interrupt_probability = self.config.get("interrupt_probability", 0.1)
        if (
            not isinstance(interrupt_probability, (int, float))
            or isinstance(interrupt_probability, bool)
            or not 0.0 <= interrupt_probability <= 1.0
        ):
            logger.warning(
                "[repeater] interrupt_probability "
                f"非法({interrupt_probability})，回退为 0.1",
            )
            interrupt_probability = 0.1
        self.interrupt_probability = float(interrupt_probability)

        raw_interrupt_texts = self.config.get(
            "interrupt_texts",
            DEFAULT_INTERRUPT_TEXTS,
        )
        if isinstance(raw_interrupt_texts, (list, tuple)):
            interrupt_texts = tuple(
                item.strip()
                for item in raw_interrupt_texts
                if isinstance(item, str) and item.strip()
            )
        else:
            interrupt_texts = ()
        if not interrupt_texts:
            logger.warning(
                "[repeater] interrupt_texts 非法或为空，回退为默认打断文本",
            )
            interrupt_texts = DEFAULT_INTERRUPT_TEXTS
        self.interrupt_texts = interrupt_texts

        interrupt_default_enabled = self.config.get(
            "interrupt_default_enabled",
            True,
        )
        if not isinstance(interrupt_default_enabled, bool):
            logger.warning(
                "[repeater] interrupt_default_enabled "
                f"非法({interrupt_default_enabled})，回退为 True",
            )
            interrupt_default_enabled = True
        self.interrupt_default_enabled = interrupt_default_enabled
        self.group_states: dict[str, GroupRepeaterState] = {}
        self.group_locks: dict[str, asyncio.Lock] = {}
        self.save_lock = asyncio.Lock()
        self.shutting_down = False
        self.active_handler_tasks: set[asyncio.Task] = set()

    async def initialize(self) -> None:
        """从插件 KV 中恢复各群状态。"""
        raw_states = await self.get_kv_data("group_states", {})
        if not isinstance(raw_states, dict):
            logger.warning("[repeater] group_states 数据异常，使用空状态")
            raw_states = {}

        for group_key, raw_state in raw_states.items():
            if not isinstance(group_key, str) or not isinstance(raw_state, dict):
                continue
            try:
                self.group_states[group_key] = GroupRepeaterState.from_dict(raw_state)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    f"[repeater] 群 {group_key} 状态加载失败: {exc}",
                )

        logger.info(f"[repeater] 已加载 {len(self.group_states)} 个群的复读状态")


    def _lock_for(self, group_key: str) -> asyncio.Lock:
        lock = self.group_locks.get(group_key)
        if lock is None:
            lock = asyncio.Lock()
            self.group_locks[group_key] = lock
        return lock

    def _state_for(self, group_key: str) -> GroupRepeaterState:
        state = self.group_states.get(group_key)
        if state is None:
            state = GroupRepeaterState()
            self.group_states[group_key] = state
        return state

    def _is_enabled(self, state: GroupRepeaterState) -> bool:
        if state.enabled_override is None:
            return self.default_enabled
        return state.enabled_override

    def _is_interrupt_enabled(self, state: GroupRepeaterState) -> bool:
        if state.interrupt_enabled_override is None:
            return self.interrupt_default_enabled
        return state.interrupt_enabled_override

    async def _save_locked(self) -> None:
        """在持有 save_lock 时保存一致快照。"""
        payload = {
            group_key: state.to_dict()
            for group_key, state in self.group_states.items()
        }
        await self.put_kv_data("group_states", payload)

    async def _save(self) -> None:
        """串行保存所有群状态，避免并发快照覆盖。"""
        async with self.save_lock:
            await self._save_locked()

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        return f"{event.get_platform_id()}:{event.get_group_id()}"

    @staticmethod
    def _fingerprint(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _begin_handler(self) -> asyncio.Task | None:
        if self.shutting_down:
            return None
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("消息处理器必须在 asyncio Task 中运行")
        self.active_handler_tasks.add(task)
        return task

    def _finish_handler(self, task: asyncio.Task) -> None:
        self.active_handler_tasks.discard(task)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """被动监听普通群消息，不唤起默认 LLM。"""
        task = self._begin_handler()
        if task is None:
            return
        try:
            await self._handle_group_message(event)
        finally:
            self._finish_handler(task)

    async def _handle_group_message(self, event: AstrMessageEvent) -> None:
        if not event.get_group_id() or event.is_at_or_wake_command:
            return
        if event.get_sender_id() == event.get_self_id():
            return

        text = event.get_message_str().strip()
        if not text:
            return

        group_key = self._group_key(event)
        if (
            group_key not in self.group_states
            and not self.default_enabled
            and not self.interrupt_default_enabled
        ):
            return
        async with self._lock_for(group_key):
            attempt = await self._process_message(
                event,
                group_key,
                event.get_sender_id(),
                text,
            )

        if attempt is None:
            return

        try:
            await event.send(event.plain_result(attempt.response_text))
        except Exception:
            try:
                await self._rollback_attempt(group_key, attempt)
            except Exception:
                logger.exception(
                    f"[repeater] {group_key} 复读发送失败，回滚保存也失败；"
                    "已保留 pending 抑制",
                )
            else:
                logger.exception(f"[repeater] {group_key} 复读发送失败，状态已回滚")
            raise

        event.stop_event()
        await self._commit_attempt(group_key, attempt)
        action = "打断复读" if attempt.interrupted else "复读"
        logger.info(
            f"[repeater] {group_key} 触发{action}: {attempt.response_text[:20]}",
        )

    async def _process_message(
        self,
        event: AstrMessageEvent,
        group_key: str,
        sender_id: str,
        text: str,
    ) -> RepeatAttempt | None:
        async with self.save_lock:
            state = self._state_for(group_key)
            repeat_enabled = self._is_enabled(state)
            interrupt_enabled = self._is_interrupt_enabled(state)
            if not repeat_enabled and not interrupt_enabled:
                return None

            previous_message_id = state.last_message_id
            message_id = str(getattr(event.message_obj, "message_id", "") or "")
            if message_id and message_id == previous_message_id:
                return None
            if message_id:
                state.last_message_id = message_id

            fingerprint = self._fingerprint(text)
            if fingerprint != state.last_fingerprint:
                previous_fingerprint = state.last_fingerprint
                previous_users = state.repeated_users
                state.last_fingerprint = fingerprint
                state.repeated_users = {sender_id}
                try:
                    await self._save_locked()
                except (asyncio.CancelledError, Exception):
                    state.last_message_id = previous_message_id
                    state.last_fingerprint = previous_fingerprint
                    state.repeated_users = previous_users
                    raise
                return None

            if (
                fingerprint in state.repeated_fingerprints
                or fingerprint in state.pending_fingerprints
            ):
                return None

            sender_was_counted = sender_id in state.repeated_users
            state.repeated_users.add(sender_id)
            threshold_reached = len(state.repeated_users) >= self.repeat_threshold
            interrupted = (
                threshold_reached
                and interrupt_enabled
                and random.random() < self.interrupt_probability
            )
            should_repeat = (
                threshold_reached
                and not interrupted
                and repeat_enabled
                and random.random() < self.repeat_probability
            )
            if interrupted or should_repeat:
                response_text = (
                    random.choice(self.interrupt_texts) if interrupted else text
                )
                state.pending_fingerprints.add(fingerprint)
                try:
                    await self._save_locked()
                except (asyncio.CancelledError, Exception):
                    state.pending_fingerprints.discard(fingerprint)
                    if not sender_was_counted:
                        state.repeated_users.discard(sender_id)
                    state.last_message_id = previous_message_id
                    raise
                return RepeatAttempt(
                    fingerprint=fingerprint,
                    message_id=message_id,
                    previous_message_id=previous_message_id,
                    response_text=response_text,
                    interrupted=interrupted,
                )

            try:
                await self._save_locked()
            except (asyncio.CancelledError, Exception):
                if not sender_was_counted:
                    state.repeated_users.discard(sender_id)
                state.last_message_id = previous_message_id
                raise
            return None

    async def _rollback_attempt(
        self,
        group_key: str,
        attempt: RepeatAttempt,
    ) -> None:
        async with self._lock_for(group_key):
            async with self.save_lock:
                state = self._state_for(group_key)
                was_pending = attempt.fingerprint in state.pending_fingerprints
                previous_message_id = state.last_message_id
                state.pending_fingerprints.discard(attempt.fingerprint)
                if state.last_message_id == attempt.message_id:
                    state.last_message_id = attempt.previous_message_id
                try:
                    await self._save_locked()
                except (asyncio.CancelledError, Exception):
                    if was_pending:
                        state.pending_fingerprints.add(attempt.fingerprint)
                    state.last_message_id = previous_message_id
                    raise

    async def _commit_attempt(
        self,
        group_key: str,
        attempt: RepeatAttempt,
    ) -> None:
        async with self._lock_for(group_key):
            async with self.save_lock:
                state = self._state_for(group_key)
                was_pending = attempt.fingerprint in state.pending_fingerprints
                was_repeated = attempt.fingerprint in state.repeated_fingerprints
                previous_users = state.repeated_users
                state.pending_fingerprints.discard(attempt.fingerprint)
                state.repeated_fingerprints.add(attempt.fingerprint)
                clears_current_sequence = (
                    state.last_fingerprint == attempt.fingerprint
                )
                if clears_current_sequence:
                    state.repeated_users = set()
                try:
                    await self._save_locked()
                except (asyncio.CancelledError, Exception):
                    if was_pending:
                        state.pending_fingerprints.add(attempt.fingerprint)
                    if not was_repeated:
                        state.repeated_fingerprints.discard(attempt.fingerprint)
                    if clears_current_sequence:
                        state.repeated_users = previous_users
                    raise

    @filter.command("自动复读", alias={"repeatMsg"})
    async def repeater_command(
        self,
        event: AstrMessageEvent,
        action: str = "帮助",
    ):
        """查看或修改本群的自动复读开关。"""
        task = self._begin_handler()
        if task is None:
            return
        try:
            reply = await self._handle_command(event, action.strip())
        finally:
            self._finish_handler(task)
        yield event.plain_result(reply)

    async def _handle_command(self, event: AstrMessageEvent, action: str) -> str:
        if not event.get_group_id():
            return "该指令仅在群聊中可用。"

        group_key = self._group_key(event)
        if action == "查看":
            lock = self.group_locks.get(group_key)
            if lock is None:
                state = self.group_states.get(group_key)
                enabled = self.default_enabled if state is None else self._is_enabled(state)
            else:
                async with lock:
                    state = self.group_states.get(group_key)
                    enabled = (
                        self.default_enabled
                        if state is None
                        else self._is_enabled(state)
                    )
            status = "开启" if enabled else "关闭"
            return (
                f"本群自动复读：{status}\n"
                f"触发阈值：{self.repeat_threshold} 名独立用户\n"
                f"触发概率：{self.repeat_probability * 100:g}%"
            )

        if action == "开启":
            async with self._lock_for(group_key):
                async with self.save_lock:
                    state = self._state_for(group_key)
                    already_enabled = self._is_enabled(state)
                    previous_override = state.enabled_override
                    state.enabled_override = True
                    try:
                        await self._save_locked()
                    except (asyncio.CancelledError, Exception):
                        state.enabled_override = previous_override
                        raise
            return (
                "本群自动复读已经是开启状态。"
                if already_enabled
                else "已在本群开启自动复读。"
            )

        if action == "关闭":
            async with self._lock_for(group_key):
                async with self.save_lock:
                    state = self._state_for(group_key)
                    already_disabled = not self._is_enabled(state)
                    previous_override = state.enabled_override
                    previous_users = state.repeated_users
                    previous_fingerprint = state.last_fingerprint
                    state.enabled_override = False
                    state.repeated_users = set()
                    state.last_fingerprint = ""
                    try:
                        await self._save_locked()
                    except (asyncio.CancelledError, Exception):
                        state.enabled_override = previous_override
                        state.repeated_users = previous_users
                        state.last_fingerprint = previous_fingerprint
                        raise
            return (
                "本群自动复读已经是关闭状态。"
                if already_disabled
                else "已在本群关闭自动复读。"
            )

        if action == "帮助":
            return (
                "指令用法：\n"
                "自动复读 查看 —— 查看本群是否开启该功能\n"
                "自动复读 开启 —— 在本群开启该功能\n"
                "自动复读 关闭 —— 在本群关闭该功能\n"
                "自动复读 帮助 —— 查看命令帮助与用法"
            )

        return f"未知子命令：{action}\n发送「自动复读 帮助」查看用法。"

    @filter.command("打断复读", alias={"interruptRepeat"})
    async def interrupt_command(
        self,
        event: AstrMessageEvent,
        action: str = "帮助",
    ):
        """查看或修改本群的打断复读开关。"""
        task = self._begin_handler()
        if task is None:
            return
        try:
            reply = await self._handle_interrupt_command(event, action.strip())
        finally:
            self._finish_handler(task)
        yield event.plain_result(reply)

    async def _handle_interrupt_command(
        self,
        event: AstrMessageEvent,
        action: str,
    ) -> str:
        if not event.get_group_id():
            return "该指令仅在群聊中可用。"

        group_key = self._group_key(event)
        if action == "查看":
            lock = self.group_locks.get(group_key)
            if lock is None:
                state = self.group_states.get(group_key)
                enabled = (
                    self.interrupt_default_enabled
                    if state is None
                    else self._is_interrupt_enabled(state)
                )
            else:
                async with lock:
                    state = self.group_states.get(group_key)
                    enabled = (
                        self.interrupt_default_enabled
                        if state is None
                        else self._is_interrupt_enabled(state)
                    )
            status = "开启" if enabled else "关闭"
            return (
                f"本群打断复读：{status}\n"
                f"打断概率：{self.interrupt_probability * 100:g}%\n"
                f"可选文本：{len(self.interrupt_texts)} 条"
            )

        if action == "开启":
            async with self._lock_for(group_key):
                async with self.save_lock:
                    state = self._state_for(group_key)
                    already_enabled = self._is_interrupt_enabled(state)
                    previous_override = state.interrupt_enabled_override
                    state.interrupt_enabled_override = True
                    try:
                        await self._save_locked()
                    except (asyncio.CancelledError, Exception):
                        state.interrupt_enabled_override = previous_override
                        raise
            return (
                "本群打断复读已经是开启状态。"
                if already_enabled
                else "已在本群开启打断复读。"
            )

        if action == "关闭":
            async with self._lock_for(group_key):
                async with self.save_lock:
                    state = self._state_for(group_key)
                    already_disabled = not self._is_interrupt_enabled(state)
                    previous_override = state.interrupt_enabled_override
                    state.interrupt_enabled_override = False
                    try:
                        await self._save_locked()
                    except (asyncio.CancelledError, Exception):
                        state.interrupt_enabled_override = previous_override
                        raise
            return (
                "本群打断复读已经是关闭状态。"
                if already_disabled
                else "已在本群关闭打断复读。"
            )

        if action == "帮助":
            return (
                "指令用法：\n"
                "打断复读 查看 —— 查看本群是否开启该功能\n"
                "打断复读 开启 —— 在本群开启该功能\n"
                "打断复读 关闭 —— 在本群关闭该功能\n"
                "打断复读 帮助 —— 查看命令帮助与用法"
            )

        return f"未知子命令：{action}\n发送「打断复读 帮助」查看用法。"

    async def terminate(self) -> None:
        """停止接收新事件，等待活动处理器后保存最终状态。"""
        self.shutting_down = True
        current_task = asyncio.current_task()
        active_tasks = tuple(
            task for task in self.active_handler_tasks if task is not current_task
        )
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await self._save()
