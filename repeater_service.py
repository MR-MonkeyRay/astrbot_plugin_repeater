"""复读状态机、配置验证及其持久化发送事务。

状态服务以单群锁和全局保存锁协调并发消息，确保发送前的 pending 标记和
发送结果的提交或回滚保持一致。
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

if __package__:
    from .repeater_messages import RepeatableMessage
else:
    from repeater_messages import RepeatableMessage


DEFAULT_INTERRUPT_TEXT = "打断！"


@dataclass(slots=True)
class GroupRepeaterState:
    """一个群的可持久化复读状态。

    Attributes:
        enabled_override: 普通复读的群级覆盖值；None 时使用全局默认值。
        interrupt_enabled_override: 打断复读的群级覆盖值；None 时使用全局默认值。
        last_fingerprint: 当前连续消息序列的规范化指纹。
        repeated_users: 当前序列中已经计入阈值的发送者 ID。
        repeated_fingerprints: 已成功发送过、不可再次复读的消息指纹。
        pending_fingerprints: 已持久化但尚未确认发送结果的消息指纹。
        last_message_id: 最近处理的消息 ID，用于重复事件去重。
    """

    enabled_override: bool | None = None
    interrupt_enabled_override: bool | None = None
    last_fingerprint: str = ""
    repeated_users: set[str] = field(default_factory=set)
    repeated_fingerprints: set[str] = field(default_factory=set)
    pending_fingerprints: set[str] = field(default_factory=set)
    last_message_id: str = ""

    @classmethod
    def from_dict(cls, raw_state: dict[str, Any]) -> "GroupRepeaterState":
        """从持久化字典恢复单群状态。

        Args:
            raw_state: 从插件 KV 存储读出的单群状态。

        Returns:
            经过类型规范化的群复读状态。

        Raises:
            TypeError: 集合字段不是持久化要求的列表。
        """
        return cls(
            enabled_override=(
                True if raw_state.get("enabled_override") is True else None
            ),
            interrupt_enabled_override=(
                True if raw_state.get("interrupt_enabled_override") is True else None
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
        """将状态转换为稳定、可序列化的持久化字典。

        Returns:
            集合已排序、可直接写入插件 KV 存储的状态字典。
        """
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
        """将持久化列表解析为字符串集合。

        Args:
            value: 预期由字符串或整数构成的列表。

        Returns:
            过滤无效元素后的字符串集合。

        Raises:
            TypeError: value 不是列表。
        """
        if not isinstance(value, list):
            raise TypeError("集合字段必须是列表")
        return {str(item) for item in value if isinstance(item, (str, int))}


@dataclass(frozen=True, slots=True)
class RepeatAttempt:
    """一个已持久化、等待消息发送层处理的复读尝试。

    Attributes:
        fingerprint: 要提交或回滚的消息指纹。
        message_id: 触发尝试的消息 ID。
        previous_message_id: 处理该消息前记录的消息 ID。
        response_text: 要发送的纯文本或消息摘要。
        response_chain: 普通复读时要原样回发的消息链。
        interrupted: 该尝试是否为打断复读。
    """

    fingerprint: str
    message_id: str
    previous_message_id: str
    response_text: str
    response_chain: tuple[Any, ...]
    interrupted: bool


@dataclass(slots=True)
class RepeaterSettings:
    """经验证后供复读状态机使用的全局策略。

    Attributes:
        config: 可写回插件配置的原始配置对象。
        repeat_disabled_group_ids: 被配置显式关闭普通复读的群 ID。
        interrupt_disabled_group_ids: 被配置显式关闭打断复读的群 ID。
        repeat_threshold: 触发复读所需的独立发送者数量。
        repeat_probability: 达到阈值后普通复读的触发概率。
        default_enabled: 普通复读的默认开关。
        interrupt_probability: 达到阈值后优先打断的触发概率。
        interrupt_texts: 打断命中时可随机选择的文本。
        interrupt_default_enabled: 打断复读的默认开关。
    """

    config: dict[str, Any]
    repeat_disabled_group_ids: set[str]
    interrupt_disabled_group_ids: set[str]
    repeat_threshold: int
    repeat_probability: float
    default_enabled: bool
    interrupt_probability: float
    interrupt_texts: tuple[str, ...]
    interrupt_default_enabled: bool

    def save_config(self) -> None:
        """将禁用群列表写回配置，并触发配置对象的保存钩子。"""
        self.config["repeat_disabled_group_ids"] = sorted(
            self.repeat_disabled_group_ids,
        )
        self.config["interrupt_disabled_group_ids"] = sorted(
            self.interrupt_disabled_group_ids,
        )
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()


def build_settings(config: dict[str, Any], logger: Any) -> RepeaterSettings:
    """验证外部插件配置并构造运行时策略。

    Args:
        config: AstrBot 提供的可写配置字典。
        logger: 用于记录非法配置回退原因的日志对象。

    Returns:
        所有字段已验证、非法值已回退的复读策略。
    """
    repeat_disabled_group_ids = _load_group_ids(
        config.get("repeat_disabled_group_ids", []),
        "repeat_disabled_group_ids",
        logger,
    )
    interrupt_disabled_group_ids = _load_group_ids(
        config.get("interrupt_disabled_group_ids", []),
        "interrupt_disabled_group_ids",
        logger,
    )

    threshold = config.get("repeat_threshold", 3)
    if (
        not isinstance(threshold, int)
        or isinstance(threshold, bool)
        or threshold < 2
    ):
        logger.warning(f"[repeater] repeat_threshold 非法({threshold})，回退为 3")
        threshold = 3

    probability = config.get("repeat_probability", 0.3)
    if (
        not isinstance(probability, (int, float))
        or isinstance(probability, bool)
        or not 0.0 <= probability <= 1.0
    ):
        logger.warning(
            f"[repeater] repeat_probability 非法({probability})，回退为 0.3",
        )
        probability = 0.3

    default_enabled = config.get("default_enabled", True)
    if not isinstance(default_enabled, bool):
        logger.warning(
            f"[repeater] default_enabled 非法({default_enabled})，回退为 True",
        )
        default_enabled = True

    interrupt_probability = config.get("interrupt_probability", 0.1)
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

    raw_interrupt_texts = config.get(
        "interrupt_texts",
        (DEFAULT_INTERRUPT_TEXT,),
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
        interrupt_texts = (DEFAULT_INTERRUPT_TEXT,)

    interrupt_default_enabled = config.get("interrupt_default_enabled", True)
    if not isinstance(interrupt_default_enabled, bool):
        logger.warning(
            "[repeater] interrupt_default_enabled "
            f"非法({interrupt_default_enabled})，回退为 True",
        )
        interrupt_default_enabled = True

    return RepeaterSettings(
        config=config,
        repeat_disabled_group_ids=repeat_disabled_group_ids,
        interrupt_disabled_group_ids=interrupt_disabled_group_ids,
        repeat_threshold=threshold,
        repeat_probability=float(probability),
        default_enabled=default_enabled,
        interrupt_probability=float(interrupt_probability),
        interrupt_texts=interrupt_texts,
        interrupt_default_enabled=interrupt_default_enabled,
    )


def _load_group_ids(value: Any, field_name: str, logger: Any) -> set[str]:
    """从配置字段读取可用的群 ID 集合。

    Args:
        value: 配置中读取的原始列表值。
        field_name: 用于日志诊断的配置字段名。
        logger: 用于记录字段类型错误的日志对象。

    Returns:
        去重、去空白并转换为字符串后的群 ID 集合。
    """
    if not isinstance(value, list):
        logger.warning(f"[repeater] {field_name} 非法，使用空列表")
        return set()
    group_ids = set()
    for item in value:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            continue
        group_id = str(item).strip()
        if group_id:
            group_ids.add(group_id)
    return group_ids


class RepeaterStateService:
    """管理群状态、持久化和复读发送事务。

    Attributes:
        settings: 已验证的全局复读策略。
        group_states: 按群 ID 保存的可持久化状态。
        group_locks: 防止同一群消息并发修改状态的锁。
        save_lock: 串行化所有状态快照及配置保存的全局锁。
    """

    def __init__(
        self,
        settings: RepeaterSettings,
        load_states: Callable[[], Awaitable[Any]],
        save_states: Callable[[dict[str, dict[str, Any]]], Awaitable[None]],
        *,
        logger: Any | None = None,
    ) -> None:
        """初始化状态服务及其异步存储依赖。

        Args:
            settings: 已验证的全局复读策略。
            load_states: 异步读取全部群状态的函数。
            save_states: 异步保存全部群状态快照的函数。
            logger: 可选日志对象；为 None 时不记录服务日志。
        """
        self.settings = settings
        self._load_states = load_states
        self._save_states = save_states
        self._logger = logger
        self.group_states: dict[str, GroupRepeaterState] = {}
        self.group_locks: dict[str, asyncio.Lock] = {}
        self.save_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """从持久化存储恢复所有可解析的群状态。

        整体数据异常或单群状态解析失败会记录警告；类型不合格的单群条目将被忽略。
        """
        raw_states = await self._load_states()
        if not isinstance(raw_states, dict):
            self._warning("[repeater] group_states 数据异常，使用空状态")
            raw_states = {}

        for group_key, raw_state in raw_states.items():
            if not isinstance(group_key, str) or not isinstance(raw_state, dict):
                continue
            try:
                self.group_states[group_key] = GroupRepeaterState.from_dict(raw_state)
            except (TypeError, ValueError) as exc:
                self._warning(f"[repeater] 群 {group_key} 状态加载失败: {exc}")

        self._info(f"[repeater] 已加载 {len(self.group_states)} 个群的复读状态")

    def lock_for(self, group_key: str) -> asyncio.Lock:
        """返回指定群的互斥锁，首次访问时创建它。

        Args:
            group_key: 群状态的字符串键。

        Returns:
            此群所有状态修改共用的 asyncio 锁。
        """
        lock = self.group_locks.get(group_key)
        if lock is None:
            lock = asyncio.Lock()
            self.group_locks[group_key] = lock
        return lock

    def state_for(self, group_key: str) -> GroupRepeaterState:
        """返回指定群的状态，必要时创建空状态。

        Args:
            group_key: 群状态的字符串键。

        Returns:
            该群的可变复读状态。
        """
        state = self.group_states.get(group_key)
        if state is None:
            state = GroupRepeaterState()
            self.group_states[group_key] = state
        return state

    def is_repeat_enabled(
        self,
        group_id: str,
        state: GroupRepeaterState | None,
    ) -> bool:
        """计算普通复读在指定群的有效开关。

        Args:
            group_id: 要检查的群 ID。
            state: 已加载的群状态；为 None 时仅使用全局策略。

        Returns:
            群未被显式禁用且覆盖值或默认值为开启时为 True。
        """
        if group_id in self.settings.repeat_disabled_group_ids:
            return False
        if state is None or state.enabled_override is None:
            return self.settings.default_enabled
        return state.enabled_override

    def is_interrupt_enabled(
        self,
        group_id: str,
        state: GroupRepeaterState | None,
    ) -> bool:
        """计算打断复读在指定群的有效开关。

        Args:
            group_id: 要检查的群 ID。
            state: 已加载的群状态；为 None 时仅使用全局策略。

        Returns:
            群未被显式禁用且覆盖值或默认值为开启时为 True。
        """
        if group_id in self.settings.interrupt_disabled_group_ids:
            return False
        if state is None or state.interrupt_enabled_override is None:
            return self.settings.interrupt_default_enabled
        return state.interrupt_enabled_override

    async def repeat_enabled_for(self, group_key: str) -> bool:
        """在群锁保护下读取普通复读的有效开关。

        Args:
            group_key: 要检查的群状态键。

        Returns:
            普通复读当前是否有效。
        """
        return await self._enabled_for(group_key, interrupt=False)

    async def interrupt_enabled_for(self, group_key: str) -> bool:
        """在群锁保护下读取打断复读的有效开关。

        Args:
            group_key: 要检查的群状态键。

        Returns:
            打断复读当前是否有效。
        """
        return await self._enabled_for(group_key, interrupt=True)

    async def set_repeat_enabled(self, group_key: str, enabled: bool) -> bool:
        """原子修改普通复读开关并持久化变更。

        Args:
            group_key: 要修改的群状态键。
            enabled: 目标开关状态。

        Returns:
            目标状态在修改前已生效时为 True。

        Raises:
            asyncio.CancelledError: 保存过程中协程被取消。
            Exception: 状态或配置保存失败；内存状态已回滚。
        """
        async with self.lock_for(group_key):
            async with self.save_lock:
                state = self.state_for(group_key)
                previously_enabled = self.is_repeat_enabled(group_key, state)
                was_disabled = group_key in self.settings.repeat_disabled_group_ids
                previous_override = state.enabled_override
                previous_users = state.repeated_users
                previous_fingerprint = state.last_fingerprint
                if enabled:
                    self.settings.repeat_disabled_group_ids.discard(group_key)
                    state.enabled_override = True
                else:
                    self.settings.repeat_disabled_group_ids.add(group_key)
                    state.enabled_override = None
                    state.repeated_users = set()
                    state.last_fingerprint = ""
                try:
                    await self._save_group_settings_locked()
                except (asyncio.CancelledError, Exception):
                    if was_disabled:
                        self.settings.repeat_disabled_group_ids.add(group_key)
                    else:
                        self.settings.repeat_disabled_group_ids.discard(group_key)
                    state.enabled_override = previous_override
                    state.repeated_users = previous_users
                    state.last_fingerprint = previous_fingerprint
                    self._restore_config_after_failure()
                    raise
                return previously_enabled == enabled

    async def set_interrupt_enabled(self, group_key: str, enabled: bool) -> bool:
        """原子修改打断复读开关并持久化变更。

        Args:
            group_key: 要修改的群状态键。
            enabled: 目标开关状态。

        Returns:
            目标状态在修改前已生效时为 True。

        Raises:
            asyncio.CancelledError: 保存过程中协程被取消。
            Exception: 状态或配置保存失败；内存状态已回滚。
        """
        async with self.lock_for(group_key):
            async with self.save_lock:
                state = self.state_for(group_key)
                previously_enabled = self.is_interrupt_enabled(group_key, state)
                was_disabled = group_key in self.settings.interrupt_disabled_group_ids
                previous_override = state.interrupt_enabled_override
                if enabled:
                    self.settings.interrupt_disabled_group_ids.discard(group_key)
                    state.interrupt_enabled_override = True
                else:
                    self.settings.interrupt_disabled_group_ids.add(group_key)
                    state.interrupt_enabled_override = None
                try:
                    await self._save_group_settings_locked()
                except (asyncio.CancelledError, Exception):
                    if was_disabled:
                        self.settings.interrupt_disabled_group_ids.add(group_key)
                    else:
                        self.settings.interrupt_disabled_group_ids.discard(group_key)
                    state.interrupt_enabled_override = previous_override
                    self._restore_config_after_failure()
                    raise
                return previously_enabled == enabled

    async def process_message(
        self,
        group_key: str,
        sender_id: str,
        message_id: str,
        message: RepeatableMessage,
    ) -> RepeatAttempt | None:
        """吸收一条群消息，必要时持久化待发送的复读尝试。

        Args:
            group_key: 消息所属群的状态键。
            sender_id: 当前消息发送者的 ID。
            message_id: 当前事件的消息 ID；空字符串表示不可用。
            message: 已规范化、可用于判重和回发的消息。

        Returns:
            达到阈值且命中概率时返回已写入 pending 标记的尝试；否则返回 None。

        Raises:
            asyncio.CancelledError: 状态保存过程中协程被取消。
            Exception: 状态保存失败；本次内存变更已回滚。
        """
        async with self.lock_for(group_key):
            async with self.save_lock:
                state = self.state_for(group_key)
                repeat_enabled = self.is_repeat_enabled(group_key, state)
                interrupt_enabled = self.is_interrupt_enabled(group_key, state)
                if not repeat_enabled and not interrupt_enabled:
                    return None

                previous_message_id = state.last_message_id
                if message_id and message_id == previous_message_id:
                    return None
                if message_id:
                    state.last_message_id = message_id

                fingerprint = message.fingerprint
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
                threshold_reached = (
                    len(state.repeated_users) >= self.settings.repeat_threshold
                )
                interrupted = (
                    threshold_reached
                    and interrupt_enabled
                    and random.random() < self.settings.interrupt_probability
                )
                should_repeat = (
                    threshold_reached
                    and not interrupted
                    and repeat_enabled
                    and random.random() < self.settings.repeat_probability
                )
                if interrupted or should_repeat:
                    response_text = (
                        random.choice(self.settings.interrupt_texts)
                        if interrupted
                        else message.summary
                    )
                    response_chain = () if interrupted else message.chain
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
                        response_chain=response_chain,
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

    async def rollback_attempt(
        self,
        group_key: str,
        attempt: RepeatAttempt,
    ) -> None:
        """撤销未成功发送的尝试，并清除其 pending 标记。

        Args:
            group_key: 该尝试所属群的状态键。
            attempt: 需要回滚的已持久化复读尝试。

        Raises:
            asyncio.CancelledError: 状态保存过程中协程被取消。
            Exception: 回滚状态保存失败；内存状态已恢复到回滚前。
        """
        async with self.lock_for(group_key):
            async with self.save_lock:
                state = self.state_for(group_key)
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

    async def commit_attempt(
        self,
        group_key: str,
        attempt: RepeatAttempt,
    ) -> None:
        """确认成功发送的尝试，并禁止相同指纹再次触发。

        Args:
            group_key: 该尝试所属群的状态键。
            attempt: 已成功发送、需要提交的复读尝试。

        Raises:
            asyncio.CancelledError: 状态保存过程中协程被取消。
            Exception: 提交状态保存失败；内存状态已恢复到提交前。
        """
        async with self.lock_for(group_key):
            async with self.save_lock:
                state = self.state_for(group_key)
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

    async def save(self) -> None:
        """在全局保存锁内持久化所有群状态快照。"""
        async with self.save_lock:
            await self._save_locked()

    async def _enabled_for(self, group_key: str, *, interrupt: bool) -> bool:
        """在需要时获取群锁后读取指定类型的有效开关。

        Args:
            group_key: 要检查的群状态键。
            interrupt: 为 True 时读取打断复读开关，否则读取普通复读开关。

        Returns:
            所请求功能在当前群是否有效。
        """
        lock = self.group_locks.get(group_key)
        if lock is None:
            state = self.group_states.get(group_key)
            return (
                self.is_interrupt_enabled(group_key, state)
                if interrupt
                else self.is_repeat_enabled(group_key, state)
            )
        async with lock:
            state = self.group_states.get(group_key)
            return (
                self.is_interrupt_enabled(group_key, state)
                if interrupt
                else self.is_repeat_enabled(group_key, state)
            )

    async def _save_locked(self) -> None:
        """持久化当前群状态快照。

        调用方必须已持有 save_lock。
        """
        payload = {
            group_key: state.to_dict() for group_key, state in self.group_states.items()
        }
        await self._save_states(payload)

    async def _save_group_settings_locked(self) -> None:
        """保存配置中的禁用群列表和当前状态快照。

        调用方必须已持有群锁及 save_lock。
        """
        self.settings.save_config()
        await self._save_locked()

    def _restore_config_after_failure(self) -> None:
        """尽力将配置对象恢复到内存回滚后的禁用群列表。

        配置恢复失败只记录异常，避免覆盖原始保存失败。
        """
        try:
            self.settings.save_config()
        except Exception:
            self._exception("[repeater] 插件配置回滚保存失败")

    def _warning(self, message: str) -> None:
        """在配置了日志对象时记录警告。

        Args:
            message: 要记录的警告文本。
        """
        if self._logger is not None:
            self._logger.warning(message)

    def _info(self, message: str) -> None:
        """在配置了日志对象时记录信息。

        Args:
            message: 要记录的信息文本。
        """
        if self._logger is not None:
            self._logger.info(message)

    def _exception(self, message: str) -> None:
        """在配置了日志对象时记录异常。

        Args:
            message: 要记录的异常上下文。
        """
        if self._logger is not None:
            self._logger.exception(message)
