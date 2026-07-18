import asyncio
import copy
import unittest
from types import SimpleNamespace

from astrbot.core.star.star_handler import star_handlers_registry

from main import RepeaterPlugin


class FakeEvent:
    def __init__(
        self,
        group_id: str,
        sender_id: str,
        text: str,
        message_id: str,
        *,
        wake: bool = False,
        fail_send: bool = False,
    ) -> None:
        self.group_id = group_id
        self.sender_id = sender_id
        self.text = text
        self.is_at_or_wake_command = wake
        self.message_obj = SimpleNamespace(message_id=message_id)
        self.sent: list[str] = []
        self.stopped = False
        self.fail_send = fail_send

    def get_group_id(self) -> str:
        return self.group_id

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_message_str(self) -> str:
        return self.text

    def get_platform_id(self) -> str:
        return "onebot"

    def get_self_id(self) -> str:
        return "bot"

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, result: str) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(result)

    def stop_event(self) -> None:
        self.stopped = True


class DelayedEvent(FakeEvent):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send(self, result: str) -> None:
        self.send_started.set()
        await self.release_send.wait()
        await super().send(result)


class FailNextPutAfterSendEvent(FakeEvent):
    def __init__(self, plugin: "MemoryRepeater", *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.plugin = plugin

    async def send(self, result: str) -> None:
        try:
            await super().send(result)
        finally:
            self.plugin.fail_next_put = True


class MemoryRepeater(RepeaterPlugin):
    def __init__(
        self,
        store: dict,
        config: dict | None = None,
        *,
        put_delay: float = 0,
    ) -> None:
        super().__init__(
            None,
            config
            or {
                "default_enabled": True,
                "repeat_threshold": 3,
                "repeat_probability": 1.0,
            },
        )
        self.store = store
        self.put_delay = put_delay
        self.fail_next_put = False
        self.active_puts = 0
        self.max_active_puts = 0

    async def get_kv_data(self, key: str, default=None):
        return copy.deepcopy(self.store.get(key, default))

    async def put_kv_data(self, key: str, value) -> None:
        if self.fail_next_put:
            self.fail_next_put = False
            raise RuntimeError("put failed")
        self.active_puts += 1
        self.max_active_puts = max(self.max_active_puts, self.active_puts)
        try:
            await asyncio.sleep(self.put_delay)
            self.store[key] = copy.deepcopy(value)
        finally:
            self.active_puts -= 1


class SequencedMemoryRepeater(MemoryRepeater):
    def __init__(self, store: dict) -> None:
        super().__init__(store)
        self.put_calls = 0
        self.first_put_started = asyncio.Event()
        self.release_first_put = asyncio.Event()

    async def put_kv_data(self, key: str, value) -> None:
        self.put_calls += 1
        if self.put_calls == 1:
            self.first_put_started.set()
            await self.release_first_put.wait()
        if self.put_calls == 3:
            raise RuntimeError("third put failed")
        await super().put_kv_data(key, value)


async def run_command(
    plugin: RepeaterPlugin,
    event: FakeEvent,
    action: str,
) -> list[str]:
    return [result async for result in plugin.repeater_command(event, action)]


class RepeaterPluginTest(unittest.IsolatedAsyncioTestCase):
    def test_default_repeat_probability_is_thirty_percent(self) -> None:
        self.assertEqual(RepeaterPlugin(None, {}).repeat_probability, 0.3)
        self.assertEqual(
            RepeaterPlugin(None, {"repeat_probability": "invalid"}).repeat_probability,
            0.3,
        )

    async def test_distinct_users_and_permanent_repeat_suppression(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(store)
        await plugin.initialize()

        first_round = [
            FakeEvent("group", sender, "内容 A", str(index))
            for index, sender in enumerate(("A", "A", "B", "C"), start=1)
        ]
        for event in first_round:
            await plugin.on_group_message(event)

        self.assertEqual(
            [event.sent for event in first_round], [[], [], [], ["内容 A"]]
        )
        self.assertTrue(first_round[-1].stopped)

        await plugin.on_group_message(FakeEvent("group", "D", "内容 B", "5"))
        second_round = [
            FakeEvent("group", sender, "内容 A", str(index))
            for index, sender in enumerate(("D", "E", "F", "G"), start=6)
        ]
        for event in second_round:
            await plugin.on_group_message(event)

        self.assertTrue(all(not event.sent for event in second_round))

        reloaded = MemoryRepeater(store)
        await reloaded.initialize()
        post_restart = [
            FakeEvent("group", sender, "内容 A", f"restart-{sender}")
            for sender in ("H", "I", "J", "K")
        ]
        for event in post_restart:
            await reloaded.on_group_message(event)
        self.assertTrue(all(not event.sent for event in post_restart))

    async def test_new_sequence_save_failure_restores_memory_and_can_retry(
        self,
    ) -> None:
        store: dict = {}
        plugin = MemoryRepeater(store)
        await plugin.initialize()
        event = FakeEvent("new-sequence", "A", "首条消息", "1")

        plugin.fail_next_put = True
        with self.assertRaisesRegex(RuntimeError, "put failed"):
            await plugin.on_group_message(event)

        state = plugin.group_states["onebot:new-sequence"]
        self.assertEqual(state.last_fingerprint, "")
        self.assertEqual(state.repeated_users, set())
        self.assertEqual(state.last_message_id, "")
        self.assertNotIn("group_states", store)

        await plugin.on_group_message(event)
        fingerprint = plugin._fingerprint("首条消息")
        self.assertEqual(state.last_fingerprint, fingerprint)
        self.assertEqual(state.repeated_users, {"A"})
        self.assertEqual(state.last_message_id, "1")
        self.assertEqual(
            store["group_states"]["onebot:new-sequence"]["last_fingerprint"],
            fingerprint,
        )

    async def test_precommit_failure_rolls_back_without_sending(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "default_enabled": True,
                "repeat_threshold": 2,
                "repeat_probability": 1.0,
            },
        )
        await plugin.initialize()

        await plugin.on_group_message(FakeEvent("precommit", "A", "保存失败", "1"))
        plugin.fail_next_put = True
        triggering_event = FakeEvent("precommit", "B", "保存失败", "2")
        with self.assertRaisesRegex(RuntimeError, "put failed"):
            await plugin.on_group_message(triggering_event)

        state = plugin.group_states["onebot:precommit"]
        fingerprint = plugin._fingerprint("保存失败")
        self.assertFalse(triggering_event.sent)
        self.assertNotIn(fingerprint, state.pending_fingerprints)
        self.assertNotIn(fingerprint, state.repeated_fingerprints)
        self.assertEqual(state.last_message_id, "1")

        await plugin.on_group_message(triggering_event)
        self.assertEqual(triggering_event.sent, ["保存失败"])

    async def test_known_send_failure_rolls_back_and_can_retry(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "default_enabled": True,
                "repeat_threshold": 2,
                "repeat_probability": 1.0,
            },
        )
        await plugin.initialize()

        await plugin.on_group_message(FakeEvent("retry", "A", "重试", "1"))
        failing_event = FakeEvent(
            "retry",
            "B",
            "重试",
            "2",
            fail_send=True,
        )
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await plugin.on_group_message(failing_event)

        state = plugin.group_states["onebot:retry"]
        fingerprint = plugin._fingerprint("重试")
        self.assertNotIn(fingerprint, state.repeated_fingerprints)
        self.assertNotIn(fingerprint, state.pending_fingerprints)
        self.assertEqual(state.last_message_id, "1")

        failing_event.fail_send = False
        await plugin.on_group_message(failing_event)
        self.assertEqual(failing_event.sent, ["重试"])
        self.assertIn(fingerprint, state.repeated_fingerprints)

    async def test_rollback_save_failure_keeps_pending_suppression(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "default_enabled": True,
                "repeat_threshold": 2,
                "repeat_probability": 1.0,
            },
        )
        await plugin.initialize()
        await plugin.on_group_message(FakeEvent("rollback", "A", "保守回滚", "1"))

        failing_event = FailNextPutAfterSendEvent(
            plugin,
            "rollback",
            "B",
            "保守回滚",
            "2",
            fail_send=True,
        )
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await plugin.on_group_message(failing_event)

        fingerprint = plugin._fingerprint("保守回滚")
        state = plugin.group_states["onebot:rollback"]
        self.assertIn(fingerprint, state.pending_fingerprints)
        self.assertIn(
            fingerprint,
            store["group_states"]["onebot:rollback"]["pending_fingerprints"],
        )

        suppressed_event = FakeEvent("rollback", "C", "保守回滚", "3")
        await plugin.on_group_message(suppressed_event)
        self.assertFalse(suppressed_event.sent)

    async def test_commit_save_failure_keeps_pending_after_successful_send(
        self,
    ) -> None:
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "default_enabled": True,
                "repeat_threshold": 2,
                "repeat_probability": 1.0,
            },
        )
        await plugin.initialize()
        await plugin.on_group_message(FakeEvent("commit", "A", "保守提交", "1"))

        triggering_event = FailNextPutAfterSendEvent(
            plugin,
            "commit",
            "B",
            "保守提交",
            "2",
        )
        with self.assertRaisesRegex(RuntimeError, "put failed"):
            await plugin.on_group_message(triggering_event)

        fingerprint = plugin._fingerprint("保守提交")
        state = plugin.group_states["onebot:commit"]
        self.assertEqual(triggering_event.sent, ["保守提交"])
        self.assertIn(fingerprint, state.pending_fingerprints)
        self.assertNotIn(fingerprint, state.repeated_fingerprints)
        self.assertIn(
            fingerprint,
            store["group_states"]["onebot:commit"]["pending_fingerprints"],
        )

        suppressed_event = FakeEvent("commit", "C", "保守提交", "3")
        await plugin.on_group_message(suppressed_event)
        self.assertFalse(suppressed_event.sent)

    async def test_send_commit_does_not_clear_a_new_sequence(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "default_enabled": True,
                "repeat_threshold": 2,
                "repeat_probability": 1.0,
            },
        )
        await plugin.initialize()

        await plugin.on_group_message(FakeEvent("race", "A", "内容 A", "1"))
        triggering_event = DelayedEvent("race", "B", "内容 A", "2")
        send_task = asyncio.create_task(plugin.on_group_message(triggering_event))
        await triggering_event.send_started.wait()

        await plugin.on_group_message(FakeEvent("race", "C", "内容 B", "3"))
        triggering_event.release_send.set()
        await send_task

        state = plugin.group_states["onebot:race"]
        self.assertEqual(state.last_fingerprint, plugin._fingerprint("内容 B"))
        self.assertEqual(state.repeated_users, {"C"})

    async def test_group_override_and_default_are_independent(self) -> None:
        plugin = MemoryRepeater({})
        await plugin.initialize()

        close_reply = await run_command(
            plugin,
            FakeEvent("group-a", "admin", "/自动复读 关闭", "1", wake=True),
            "关闭",
        )
        self.assertEqual(close_reply, ["已在本群关闭自动复读。"])
        self.assertFalse(plugin._is_enabled(plugin._state_for("onebot:group-a")))
        self.assertTrue(plugin._is_enabled(plugin._state_for("onebot:group-b")))

        plugin.default_enabled = False
        self.assertFalse(plugin._is_enabled(plugin._state_for("onebot:group-b")))

        open_reply = await run_command(
            plugin,
            FakeEvent("group-a", "admin", "/repeatMsg 开启", "2", wake=True),
            "开启",
        )
        self.assertEqual(open_reply, ["已在本群开启自动复读。"])
        self.assertTrue(plugin._is_enabled(plugin._state_for("onebot:group-a")))

    async def test_command_save_failure_restores_group_state(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(store)
        await plugin.initialize()
        await plugin.on_group_message(FakeEvent("command", "A", "已有序列", "1"))
        saved_before = copy.deepcopy(store["group_states"]["onebot:command"])

        plugin.fail_next_put = True
        with self.assertRaisesRegex(RuntimeError, "put failed"):
            await run_command(
                plugin,
                FakeEvent("command", "admin", "/自动复读 关闭", "2", wake=True),
                "关闭",
            )

        state = plugin.group_states["onebot:command"]
        self.assertTrue(plugin._is_enabled(state))
        self.assertEqual(state.last_fingerprint, plugin._fingerprint("已有序列"))
        self.assertEqual(state.repeated_users, {"A"})
        self.assertEqual(store["group_states"]["onebot:command"], saved_before)

    async def test_read_only_disabled_group_does_not_allocate_state(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "default_enabled": False,
                "repeat_threshold": 3,
                "repeat_probability": 1.0,
            },
        )
        await plugin.initialize()

        reply = await run_command(
            plugin,
            FakeEvent("disabled", "admin", "/自动复读 查看", "1", wake=True),
            "查看",
        )
        await plugin.on_group_message(FakeEvent("disabled", "A", "忽略", "2"))

        self.assertEqual(reply[0].splitlines()[0], "本群自动复读：关闭")
        self.assertNotIn("onebot:disabled", plugin.group_states)
        self.assertNotIn("onebot:disabled", plugin.group_locks)
        self.assertEqual(store, {})

    async def test_terminate_waits_for_active_send_and_blocks_new_events(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "default_enabled": True,
                "repeat_threshold": 2,
                "repeat_probability": 1.0,
            },
        )
        await plugin.initialize()
        await plugin.on_group_message(FakeEvent("reload", "A", "热重载", "1"))

        triggering_event = DelayedEvent("reload", "B", "热重载", "2")
        send_task = asyncio.create_task(plugin.on_group_message(triggering_event))
        await triggering_event.send_started.wait()
        terminate_task = asyncio.create_task(plugin.terminate())
        await asyncio.sleep(0)

        self.assertFalse(terminate_task.done())
        ignored_event = FakeEvent("new-group", "C", "不会处理", "3")
        await plugin.on_group_message(ignored_event)
        self.assertNotIn("onebot:new-group", plugin.group_states)

        triggering_event.release_send.set()
        await asyncio.gather(send_task, terminate_task)
        self.assertFalse(plugin.active_handler_tasks)
        self.assertIn(
            plugin._fingerprint("热重载"),
            plugin.group_states["onebot:reload"].repeated_fingerprints,
        )

    async def test_concurrent_group_saves_keep_both_updates(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(store, put_delay=0.01)

        async def update(group_key: str, fingerprint: str) -> None:
            plugin._state_for(group_key).last_fingerprint = fingerprint
            await plugin._save()

        await asyncio.gather(
            update("onebot:group-a", "A"),
            update("onebot:group-b", "B"),
        )

        saved = store["group_states"]
        self.assertEqual(saved["onebot:group-a"]["last_fingerprint"], "A")
        self.assertEqual(saved["onebot:group-b"]["last_fingerprint"], "B")
        self.assertEqual(plugin.max_active_puts, 1)

    async def test_failed_group_transaction_cannot_leak_through_other_save(
        self,
    ) -> None:
        store: dict = {}
        plugin = SequencedMemoryRepeater(store)

        first_task = asyncio.create_task(
            plugin.on_group_message(FakeEvent("first", "A", "A", "1"))
        )
        await plugin.first_put_started.wait()

        second_task = asyncio.create_task(
            plugin.on_group_message(FakeEvent("second", "B", "B", "2"))
        )
        await asyncio.sleep(0)
        failing_task = asyncio.create_task(
            plugin.on_group_message(FakeEvent("failed", "C", "C", "3"))
        )
        await asyncio.sleep(0)

        plugin.release_first_put.set()
        await asyncio.gather(first_task, second_task)
        with self.assertRaisesRegex(RuntimeError, "third put failed"):
            await failing_task

        saved = store["group_states"]
        self.assertIn("onebot:first", saved)
        self.assertIn("onebot:second", saved)
        self.assertNotIn("onebot:failed", saved)
        failed_state = plugin.group_states["onebot:failed"]
        self.assertEqual(failed_state.last_fingerprint, "")
        self.assertEqual(failed_state.repeated_users, set())
        self.assertEqual(failed_state.last_message_id, "")

    def test_repeat_msg_alias_is_registered(self) -> None:
        handlers = [
            handler
            for handler in star_handlers_registry
            if handler.handler_name == "repeater_command"
        ]
        self.assertTrue(handlers)
        command_filter = next(
            event_filter
            for event_filter in handlers[-1].event_filters
            if hasattr(event_filter, "command_name")
        )
        self.assertEqual(command_filter.command_name, "自动复读")
        self.assertIn("repeatMsg", command_filter.alias)


if __name__ == "__main__":
    unittest.main()
