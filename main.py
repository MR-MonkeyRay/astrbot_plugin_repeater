"""将 AstrBot 群事件适配为可持久化的复读事务。

本模块处理框架事件、权限检查和消息发送；连续消息判定及状态更新由
`RepeaterStateService` 负责。
"""

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

if __package__:
    from .repeater_messages import repeatable_message
    from .repeater_service import RepeaterStateService, build_settings
else:
    from repeater_messages import repeatable_message
    from repeater_service import RepeaterStateService, build_settings


PERMISSION_ERROR = (
    "权限错误：仅 AstrBot 管理员、群主或群管理员可以开启或关闭。"
    "请向本群管理员或群主求助。"
)


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

    async def initialize(self) -> None:
        """从插件 KV 存储恢复可解析的各群状态。

        无效状态由状态服务记录警告并跳过。
        """
        await self.state_service.initialize()

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
        state = self.state_service.group_states.get(group_key)
        if not self.state_service.is_repeat_enabled(
            group_key,
            state,
        ) and not self.state_service.is_interrupt_enabled(group_key, state):
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

        result = (
            event.chain_result(list(attempt.response_chain))
            if attempt.response_chain
            else event.plain_result(attempt.response_text)
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
            f"[repeater] {group_key} 触发{action}: {attempt.response_text[:20]}",
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
        command = "打断复读" if interrupt else "自动复读"
        if action == "查看":
            enabled = (
                await self.state_service.interrupt_enabled_for(group_key)
                if interrupt
                else await self.state_service.repeat_enabled_for(group_key)
            )
            status = "开启" if enabled else "关闭"
            if interrupt:
                return (
                    f"本群{noun}：{status}\n"
                    f"打断概率：{settings.interrupt_probability * 100:g}%\n"
                    f"可选文本：{len(settings.interrupt_texts)} 条"
                )
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
                f"{command} 查看 —— 查看本群是否开启该功能\n"
                f"{command} 开启 —— 在本群开启该功能\n"
                f"{command} 关闭 —— 在本群关闭该功能\n"
                "开启/关闭仅限 AstrBot 管理员、群主或群管理员\n"
                f"{command} 帮助 —— 查看命令帮助与用法"
            )

        return f"未知子命令：{action}\n发送「{command} 帮助」查看用法。"

    async def terminate(self) -> None:
        """阻止新处理器，等待活动任务后持久化最终状态。"""
        self.shutting_down = True
        current_task = asyncio.current_task()
        active_tasks = tuple(
            task for task in self.active_handler_tasks if task is not current_task
        )
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await self.state_service.save()
