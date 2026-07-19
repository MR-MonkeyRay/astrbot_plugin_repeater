import asyncio
import copy
import json
import unittest
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.api.message_components import Face, Image, Plain

from main import PERMISSION_ERROR, RepeaterPlugin
from repeater_messages import (
    RepeatableMessage,
    fingerprint as make_fingerprint,
    repeatable_message,
)
from repeater_service import (
    DEFAULT_INTERRUPT_TEXT,
    RepeaterSettings,
    RepeaterStateService,
)

class ConfigSchemaTest(unittest.TestCase):
    def test_slider_fields_use_expected_ranges(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        expected_sliders = {
            "repeat_threshold": ("int", {"min": 2, "max": 50, "step": 1}),
            "repeat_probability": ("float", {"min": 0, "max": 1, "step": 0.01}),
            "interrupt_probability": ("float", {"min": 0, "max": 1, "step": 0.01}),
        }
        for key, (field_type, slider) in expected_sliders.items():
            with self.subTest(key=key):
                field = schema[key]
                self.assertEqual(field["type"], field_type)
                self.assertEqual(field["slider"], slider)


class ImportPathTest(unittest.TestCase):
    def test_main_imports_directly_and_as_plugin_package(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary_root:
            plugin_parent = Path(temporary_root) / "data" / "plugins"
            plugin_parent.mkdir(parents=True)
            os.symlink(
                project_root,
                plugin_parent / "astrbot_plugin_repeater",
                target_is_directory=True,
            )

            direct_import = subprocess.run(
                [sys.executable, "-c", "import main"],
                cwd=project_root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(direct_import.returncode, 0, direct_import.stderr)

            package_import = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import importlib; "
                    "importlib.import_module("
                    "'data.plugins.astrbot_plugin_repeater.main'"
                    ")",
                ],
                cwd=temporary_root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(package_import.returncode, 0, package_import.stderr)

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
        chain: list | None = None,
        raw_message: object | None = None,
        astrbot_admin: bool | None = None,
        group_owner: str = "",
        group_admins: list[str] | None = None,
        group_lookup_error: bool = False,
    ) -> None:
        self.group_id = group_id
        self.sender_id = sender_id
        self.text = text
        self.is_at_or_wake_command = wake
        message_chain = chain if chain is not None else ([Plain(text)] if text else [])
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            message=message_chain,
            raw_message=raw_message,
        )
        self.sent: list[object] = []
        self.stopped = False
        self.fail_send = fail_send
        self.astrbot_admin = (
            sender_id == "admin" if astrbot_admin is None else astrbot_admin
        )
        self.group_owner = group_owner
        self.group_admins = group_admins or []
        self.group_lookup_error = group_lookup_error

    def get_group_id(self) -> str:
        return self.group_id

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_message_str(self) -> str:
        return self.text

    def get_messages(self) -> list:
        return self.message_obj.message

    def get_platform_id(self) -> str:
        return "onebot"

    def get_self_id(self) -> str:
        return "bot"

    def is_admin(self) -> bool:
        return self.astrbot_admin

    async def get_group(self):
        if self.group_lookup_error:
            raise RuntimeError("group lookup failed")
        return SimpleNamespace(
            group_owner=self.group_owner,
            group_admins=self.group_admins,
        )

    def plain_result(self, text: str) -> str:
        return text

    def chain_result(self, chain: list) -> list:
        return chain

    async def send(self, result: object) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(result)

    def stop_event(self) -> None:
        self.stopped = True

class MessageBoundaryTest(unittest.TestCase):
    def test_raw_mface_message_normalizes_to_replayable_chain(self) -> None:
        self.assertIsNotNone(repeatable_message)
        event = FakeEvent(
            "message-boundary",
            "A",
            "前缀",
            "1",
            chain=[Plain("前缀")],
            raw_message={
                "message": [
                    {"type": "text", "data": {"text": "前缀"}},
                    {
                        "type": "mface",
                        "data": {
                            "emoji_package_id": "package",
                            "emoji_id": "same",
                        },
                    },
                ],
            },
        )

        message = repeatable_message(event)

        self.assertIsNotNone(message)
        self.assertEqual(message.summary, "前缀")
        self.assertEqual(
            [segment.toDict()["type"] for segment in message.chain],
            ["text", "mface"],
        )
        self.assertEqual(message.chain[1].toDict()["data"]["emoji_id"], "same")


class DelayedEvent(FakeEvent):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send(self, result: object) -> None:
        self.send_started.set()
        await self.release_send.wait()
        await super().send(result)


class FailNextPutAfterSendEvent(FakeEvent):
    def __init__(self, plugin: "MemoryRepeater", *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.plugin = plugin

    async def send(self, result: object) -> None:
        try:
            await super().send(result)
        finally:
            self.plugin.fail_next_put = True


class MemoryConfig(dict):
    def __init__(self, values: dict | None = None) -> None:
        super().__init__(values or {})
        self.save_count = 0
        self.fail_next_save = False

    def save_config(self) -> None:
        if self.fail_next_save:
            self.fail_next_save = False
            raise RuntimeError("config save failed")
        self.save_count += 1


class MemoryRepeater(RepeaterPlugin):
    def __init__(
        self,
        store: dict,
        config: dict | None = None,
        *,
        put_delay: float = 0,
    ) -> None:
        defaults = {
            "default_enabled": True,
            "repeat_threshold": 3,
            "repeat_probability": 1.0,
            "interrupt_default_enabled": False,
        }
        if config is not None and callable(getattr(config, "save_config", None)):
            for key, value in defaults.items():
                config.setdefault(key, value)
            effective_config = config
        else:
            effective_config = defaults
            if config is not None:
                effective_config.update(config)
        super().__init__(None, effective_config)
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


class StateServiceBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_threshold_attempt_is_persisted_before_delivery(self) -> None:
        self.assertIsNotNone(RepeaterSettings)
        self.assertIsNotNone(RepeaterStateService)
        self.assertIsNotNone(RepeatableMessage)
        store: dict = {}

        async def load_states():
            return copy.deepcopy(store.get("group_states", {}))

        async def save_states(states):
            store["group_states"] = copy.deepcopy(states)

        settings = RepeaterSettings(
            config={},
            repeat_disabled_group_ids=set(),
            interrupt_disabled_group_ids=set(),
            repeat_threshold=2,
            repeat_probability=1.0,
            default_enabled=True,
            interrupt_probability=0.0,
            interrupt_texts=("打断！",),
            interrupt_default_enabled=False,
        )
        service = RepeaterStateService(settings, load_states, save_states)
        await service.initialize()
        message = RepeatableMessage(
            fingerprint="message-fingerprint",
            text="内容",
            chain=(),
            summary="内容",
        )

        self.assertIsNone(
            await service.process_message("group", "A", "1", message),
        )
        attempt = await service.process_message("group", "B", "2", message)

        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.response_text, "内容")
        self.assertIn(
            "message-fingerprint",
            store["group_states"]["group"]["pending_fingerprints"],
        )

async def run_command(
    plugin: RepeaterPlugin,
    event: FakeEvent,
    action: str,
) -> list[str]:
    return [result async for result in plugin.repeater_command(event, action)]


async def run_interrupt_command(
    plugin: RepeaterPlugin,
    event: FakeEvent,
    action: str,
) -> list[str]:
    return [result async for result in plugin.interrupt_command(event, action)]


class RepeaterPluginTest(unittest.IsolatedAsyncioTestCase):
    def test_default_repeat_probability_is_thirty_percent(self) -> None:
        self.assertEqual(RepeaterPlugin(None, {}).state_service.settings.repeat_probability, 0.3)
        self.assertEqual(
            RepeaterPlugin(None, {"repeat_probability": "invalid"}).state_service.settings.repeat_probability,
            0.3,
        )

    def test_interrupt_config_defaults_and_invalid_values(self) -> None:
        plugin = RepeaterPlugin(None, {})
        self.assertTrue(plugin.state_service.settings.interrupt_default_enabled)
        self.assertEqual(plugin.state_service.settings.interrupt_probability, 0.1)
        self.assertEqual(plugin.state_service.settings.interrupt_texts, (DEFAULT_INTERRUPT_TEXT,))
        self.assertEqual(len(plugin.state_service.settings.interrupt_texts), 1)

        invalid = RepeaterPlugin(
            None,
            {
                "interrupt_default_enabled": "yes",
                "interrupt_probability": "invalid",
                "interrupt_texts": ["", 1, "   "],
            },
        )
        self.assertTrue(invalid.state_service.settings.interrupt_default_enabled)
        self.assertEqual(invalid.state_service.settings.interrupt_probability, 0.1)
        self.assertEqual(invalid.state_service.settings.interrupt_texts, (DEFAULT_INTERRUPT_TEXT,))

        empty = RepeaterPlugin(None, {"interrupt_texts": []})
        self.assertEqual(empty.state_service.settings.interrupt_texts, (DEFAULT_INTERRUPT_TEXT,))

        custom = RepeaterPlugin(
            None,
            {"interrupt_texts": [" 第一条 ", "", 2, "第二条"]},
        )
        self.assertEqual(custom.state_service.settings.interrupt_texts, ("第一条", "第二条"))

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

    async def test_same_image_or_face_repeats_original_chain(self) -> None:
        image_plugin = MemoryRepeater({}, {"repeat_threshold": 2})
        await image_plugin.initialize()
        first_image = FakeEvent(
            "image",
            "A",
            "",
            "1",
            chain=[Image(file="same-image", url="https://first.example/image")],
        )
        second_image = FakeEvent(
            "image",
            "B",
            "",
            "2",
            chain=[Image(file="same-image", url="https://second.example/image")],
        )
        await image_plugin.on_group_message(first_image)
        await image_plugin.on_group_message(second_image)

        self.assertFalse(first_image.sent)
        self.assertEqual(len(second_image.sent), 1)
        image_chain = second_image.sent[0]
        self.assertIsInstance(image_chain, list)
        self.assertIsInstance(image_chain[0], Image)
        self.assertEqual(image_chain[0].file, "same-image")

        face_plugin = MemoryRepeater({}, {"repeat_threshold": 2})
        await face_plugin.initialize()
        first_face = FakeEvent("face", "A", "", "1", chain=[Face(id=123)])
        second_face = FakeEvent("face", "B", "", "2", chain=[Face(id=123)])
        await face_plugin.on_group_message(first_face)
        await face_plugin.on_group_message(second_face)

        self.assertEqual(len(second_face.sent), 1)
        face_chain = second_face.sent[0]
        self.assertIsInstance(face_chain[0], Face)
        self.assertEqual(face_chain[0].id, 123)

    async def test_different_media_does_not_share_a_sequence(self) -> None:
        plugin = MemoryRepeater({}, {"repeat_threshold": 2})
        await plugin.initialize()
        first = FakeEvent(
            "different-media",
            "A",
            "相同说明",
            "1",
            chain=[Plain("相同说明"), Image(file="image-a")],
        )
        second = FakeEvent(
            "different-media",
            "B",
            "相同说明",
            "2",
            chain=[Plain("相同说明"), Image(file="image-b")],
        )
        await plugin.on_group_message(first)
        await plugin.on_group_message(second)

        self.assertFalse(first.sent)
        self.assertFalse(second.sent)
        self.assertEqual(plugin.state_service.group_states["different-media"].repeated_users, {"B"})

    async def test_onebot_mface_uses_raw_identity_and_replays_in_order(self) -> None:
        plugin = MemoryRepeater({}, {"repeat_threshold": 2})
        await plugin.initialize()

        def mface_event(sender: str, message_id: str, emoji_id: str, url: str):
            raw_segments = [
                {"type": "text", "data": {"text": "前缀"}},
                {
                    "type": "mface",
                    "data": {
                        "emoji_package_id": "package",
                        "emoji_id": emoji_id,
                        "key": f"key-{url}",
                        "url": url,
                    },
                },
            ]
            return FakeEvent(
                "mface",
                sender,
                "前缀",
                message_id,
                chain=[Plain("前缀")],
                raw_message={"message": raw_segments},
            )

        first = mface_event("A", "1", "same", "https://first.example/mface")
        second = mface_event("B", "2", "same", "https://second.example/mface")
        await plugin.on_group_message(first)
        await plugin.on_group_message(second)

        self.assertEqual(len(second.sent), 1)
        replayed = second.sent[0]
        self.assertEqual(
            [segment.toDict()["type"] for segment in replayed],
            ["text", "mface"],
        )
        self.assertEqual(replayed[1].toDict()["data"]["emoji_id"], "same")

        different = mface_event("C", "3", "different", "https://third.example/mface")
        await plugin.on_group_message(different)
        self.assertFalse(different.sent)

    async def test_media_interrupt_sends_interrupt_text_only(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
            },
        )
        await plugin.initialize()
        first = FakeEvent("media-interrupt", "A", "", "1", chain=[Face(id=456)])
        second = FakeEvent("media-interrupt", "B", "", "2", chain=[Face(id=456)])
        await plugin.on_group_message(first)
        await plugin.on_group_message(second)

        self.assertEqual(second.sent, [DEFAULT_INTERRUPT_TEXT])

    async def test_empty_interrupt_texts_sends_default_text(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": [],
            },
        )
        await plugin.initialize()

        first = FakeEvent("empty-interrupt", "A", "原始复读内容", "1")
        second = FakeEvent("empty-interrupt", "B", "原始复读内容", "2")
        await plugin.on_group_message(first)
        await plugin.on_group_message(second)

        self.assertFalse(first.sent)
        self.assertEqual(second.sent, [DEFAULT_INTERRUPT_TEXT])

    async def test_interrupt_preempts_repeat_and_randomly_selects_text(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "default_enabled": True,
                "repeat_threshold": 2,
                "repeat_probability": 1.0,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["打断甲", "打断乙", "打断丙"],
            },
        )
        await plugin.initialize()
        first = FakeEvent("interrupt", "A", "原始复读内容", "1")
        second = FakeEvent("interrupt", "B", "原始复读内容", "2")

        with (
            patch("repeater_service.random.random", return_value=0.0),
            patch("repeater_service.random.choice", return_value="打断乙") as choice_mock,
        ):
            await plugin.on_group_message(first)
            await plugin.on_group_message(second)

        self.assertFalse(first.sent)
        self.assertEqual(second.sent, ["打断乙"])
        self.assertTrue(second.stopped)
        choice_mock.assert_called_once_with(("打断甲", "打断乙", "打断丙"))
        self.assertIn(
            make_fingerprint("原始复读内容"),
            plugin.state_service.group_states["interrupt"].repeated_fingerprints,
        )

    async def test_interrupt_miss_falls_through_to_normal_repeat(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "default_enabled": True,
                "repeat_threshold": 2,
                "repeat_probability": 1.0,
                "interrupt_default_enabled": True,
                "interrupt_probability": 0.1,
                "interrupt_texts": ["不会发送"],
            },
        )
        await plugin.initialize()
        first = FakeEvent("fallthrough", "A", "继续复读", "1")
        second = FakeEvent("fallthrough", "B", "继续复读", "2")

        with (
            patch("repeater_service.random.random", side_effect=[0.9, 0.0]) as random_mock,
            patch("repeater_service.random.choice") as choice_mock,
        ):
            await plugin.on_group_message(first)
            await plugin.on_group_message(second)

        self.assertEqual(second.sent, ["继续复读"])
        self.assertEqual(random_mock.call_count, 2)
        choice_mock.assert_not_called()

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

        state = plugin.state_service.group_states["new-sequence"]
        self.assertEqual(state.last_fingerprint, "")
        self.assertEqual(state.repeated_users, set())
        self.assertEqual(state.last_message_id, "")
        self.assertNotIn("group_states", store)

        await plugin.on_group_message(event)
        fingerprint = make_fingerprint("首条消息")
        self.assertEqual(state.last_fingerprint, fingerprint)
        self.assertEqual(state.repeated_users, {"A"})
        self.assertEqual(state.last_message_id, "1")
        self.assertEqual(
            store["group_states"]["new-sequence"]["last_fingerprint"],
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

        state = plugin.state_service.group_states["precommit"]
        fingerprint = make_fingerprint("保存失败")
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

        state = plugin.state_service.group_states["retry"]
        fingerprint = make_fingerprint("重试")
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

        fingerprint = make_fingerprint("保守回滚")
        state = plugin.state_service.group_states["rollback"]
        self.assertIn(fingerprint, state.pending_fingerprints)
        self.assertIn(
            fingerprint,
            store["group_states"]["rollback"]["pending_fingerprints"],
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

        fingerprint = make_fingerprint("保守提交")
        state = plugin.state_service.group_states["commit"]
        self.assertEqual(triggering_event.sent, ["保守提交"])
        self.assertIn(fingerprint, state.pending_fingerprints)
        self.assertNotIn(fingerprint, state.repeated_fingerprints)
        self.assertIn(
            fingerprint,
            store["group_states"]["commit"]["pending_fingerprints"],
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

        state = plugin.state_service.group_states["race"]
        self.assertEqual(state.last_fingerprint, make_fingerprint("内容 B"))
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
        self.assertFalse(plugin.state_service.is_repeat_enabled("group-a", plugin.state_service.state_for("group-a")))
        self.assertTrue(plugin.state_service.is_repeat_enabled("group-b", plugin.state_service.state_for("group-b")))

        plugin.state_service.settings.default_enabled = False
        self.assertFalse(plugin.state_service.is_repeat_enabled("group-b", plugin.state_service.state_for("group-b")))

        open_reply = await run_command(
            plugin,
            FakeEvent("group-a", "admin", "/repeatMsg 开启", "2", wake=True),
            "开启",
        )
        self.assertEqual(open_reply, ["已在本群开启自动复读。"])
        self.assertTrue(plugin.state_service.is_repeat_enabled("group-a", plugin.state_service.state_for("group-a")))

    async def test_interrupt_command_has_independent_persisted_state(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(store)
        await plugin.initialize()
        event = FakeEvent("interrupt-command", "admin", "/打断复读 查看", "1")

        status_reply = await run_interrupt_command(plugin, event, "查看")
        open_reply = await run_interrupt_command(plugin, event, "开启")

        state = plugin.state_service.group_states["interrupt-command"]
        self.assertEqual(status_reply[0].splitlines()[0], "本群打断复读：关闭")
        self.assertEqual(open_reply, ["已在本群开启打断复读。"])
        self.assertTrue(plugin.state_service.is_interrupt_enabled("interrupt-command", state))
        self.assertTrue(plugin.state_service.is_repeat_enabled("interrupt-command", state))
        self.assertTrue(
            store["group_states"]["interrupt-command"]["interrupt_enabled_override"]
        )

        reloaded = MemoryRepeater(store)
        await reloaded.initialize()
        reloaded_state = reloaded.state_service.group_states["interrupt-command"]
        self.assertTrue(
            reloaded.state_service.is_interrupt_enabled("interrupt-command",
            reloaded_state,)
        )
        close_reply = await run_interrupt_command(reloaded, event, "关闭")
        self.assertEqual(close_reply, ["已在本群关闭打断复读。"])
        self.assertFalse(
            reloaded.state_service.is_interrupt_enabled("interrupt-command",
            reloaded_state,)
        )

        help_reply = await run_interrupt_command(reloaded, event, "帮助")
        self.assertIn("打断复读 查看", help_reply[0])

    async def test_toggle_permissions_and_config_lists(self) -> None:
        config = MemoryConfig(
            {
                "default_enabled": True,
                "interrupt_default_enabled": True,
            }
        )
        plugin = MemoryRepeater({}, config)
        await plugin.initialize()

        owner = FakeEvent("managed", "owner", "", "1", group_owner="owner")
        group_admin = FakeEvent(
            "managed",
            "moderator",
            "",
            "2",
            group_admins=["moderator"],
        )
        member = FakeEvent("managed", "member", "", "3")

        self.assertEqual(
            await run_command(plugin, owner, "关闭"),
            ["已在本群关闭自动复读。"],
        )
        self.assertEqual(
            await run_interrupt_command(plugin, group_admin, "关闭"),
            ["已在本群关闭打断复读。"],
        )
        self.assertEqual(
            config["repeat_disabled_group_ids"],
            ["managed"],
        )
        self.assertEqual(
            config["interrupt_disabled_group_ids"],
            ["managed"],
        )

        saves_before_denial = config.save_count
        self.assertEqual(await run_command(plugin, member, "开启"), [PERMISSION_ERROR])
        self.assertEqual(config.save_count, saves_before_denial)
        self.assertEqual(
            config["repeat_disabled_group_ids"],
            ["managed"],
        )

        self.assertEqual(
            await run_command(plugin, owner, "开启"),
            ["已在本群开启自动复读。"],
        )
        self.assertEqual(
            await run_interrupt_command(plugin, group_admin, "开启"),
            ["已在本群开启打断复读。"],
        )
        self.assertEqual(config["repeat_disabled_group_ids"], [])
        self.assertEqual(config["interrupt_disabled_group_ids"], [])

    async def test_astrbot_admin_does_not_need_group_lookup(self) -> None:
        config = MemoryConfig()
        plugin = MemoryRepeater({}, config)
        await plugin.initialize()
        event = FakeEvent(
            "admin-managed",
            "admin",
            "",
            "1",
            group_lookup_error=True,
        )

        self.assertEqual(
            await run_command(plugin, event, "关闭"),
            ["已在本群关闭自动复读。"],
        )

    async def test_configured_disabled_group_ids_apply_directly(self) -> None:
        config = MemoryConfig(
            {
                "repeat_disabled_group_ids": ["configured"],
                "interrupt_disabled_group_ids": ["configured"],
            }
        )
        plugin = MemoryRepeater({}, config)
        await plugin.initialize()

        self.assertFalse(plugin.state_service.is_repeat_enabled("configured", None))
        self.assertFalse(plugin.state_service.is_interrupt_enabled("configured", None))
        self.assertEqual(config.save_count, 0)
        self.assertEqual(config["repeat_disabled_group_ids"], ["configured"])
        self.assertEqual(config["interrupt_disabled_group_ids"], ["configured"])

    async def test_config_save_failure_restores_toggle_state(self) -> None:
        config = MemoryConfig()
        plugin = MemoryRepeater({}, config)
        await plugin.initialize()
        config.fail_next_save = True

        with self.assertRaisesRegex(RuntimeError, "config save failed"):
            await run_command(
                plugin,
                FakeEvent("config-failure", "admin", "", "1"),
                "关闭",
            )

        state = plugin.state_service.group_states["config-failure"]
        self.assertTrue(plugin.state_service.is_repeat_enabled("config-failure", state))
        self.assertEqual(config["repeat_disabled_group_ids"], [])

    async def test_command_save_failure_restores_group_state(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(store)
        await plugin.initialize()
        await plugin.on_group_message(FakeEvent("command", "A", "已有序列", "1"))
        saved_before = copy.deepcopy(store["group_states"]["command"])

        plugin.fail_next_put = True
        with self.assertRaisesRegex(RuntimeError, "put failed"):
            await run_command(
                plugin,
                FakeEvent("command", "admin", "/自动复读 关闭", "2", wake=True),
                "关闭",
            )

        state = plugin.state_service.group_states["command"]
        self.assertTrue(plugin.state_service.is_repeat_enabled("command", state))
        self.assertEqual(state.last_fingerprint, make_fingerprint("已有序列"))
        self.assertEqual(state.repeated_users, {"A"})
        self.assertEqual(store["group_states"]["command"], saved_before)

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
        self.assertNotIn("disabled", plugin.state_service.group_states)
        self.assertNotIn("disabled", plugin.state_service.group_locks)
        self.assertEqual(store, {})
    async def test_disabled_group_skips_unparseable_message_chain(self) -> None:
        class ExplodingComponent:
            def toDict(self) -> dict:
                raise RuntimeError("disabled group must not parse messages")

        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "default_enabled": False,
                "interrupt_default_enabled": False,
            },
        )
        await plugin.initialize()
        event = FakeEvent(
            "disabled-unparseable",
            "A",
            "",
            "1",
            chain=[ExplodingComponent()],
        )

        await plugin.on_group_message(event)

        self.assertFalse(event.sent)
        self.assertNotIn("disabled-unparseable", plugin.state_service.group_states)
        self.assertNotIn("disabled-unparseable", plugin.state_service.group_locks)
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
        self.assertNotIn("new-group", plugin.state_service.group_states)

        triggering_event.release_send.set()
        await asyncio.gather(send_task, terminate_task)
        self.assertFalse(plugin.active_handler_tasks)
        self.assertIn(
            make_fingerprint("热重载"),
            plugin.state_service.group_states["reload"].repeated_fingerprints,
        )

    async def test_concurrent_group_saves_keep_both_updates(self) -> None:
        store: dict = {}
        plugin = MemoryRepeater(store, put_delay=0.01)

        async def update(group_key: str, fingerprint: str) -> None:
            plugin.state_service.state_for(group_key).last_fingerprint = fingerprint
            await plugin.state_service.save()

        await asyncio.gather(
            update("group-a", "A"),
            update("group-b", "B"),
        )

        saved = store["group_states"]
        self.assertEqual(saved["group-a"]["last_fingerprint"], "A")
        self.assertEqual(saved["group-b"]["last_fingerprint"], "B")
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
        self.assertIn("first", saved)
        self.assertIn("second", saved)
        self.assertNotIn("failed", saved)
        failed_state = plugin.state_service.group_states["failed"]
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

    def test_interrupt_repeat_alias_is_registered(self) -> None:
        handlers = [
            handler
            for handler in star_handlers_registry
            if handler.handler_name == "interrupt_command"
        ]
        self.assertTrue(handlers)
        command_filter = next(
            event_filter
            for event_filter in handlers[-1].event_filters
            if hasattr(event_filter, "command_name")
        )
        self.assertEqual(command_filter.command_name, "打断复读")
        self.assertIn("interruptRepeat", command_filter.alias)


if __name__ == "__main__":
    unittest.main()
