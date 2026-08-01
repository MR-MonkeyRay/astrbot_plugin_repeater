import asyncio
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
import os
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from typing import Callable

from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.api.message_components import Face, Image, Plain
from astrbot.api.provider import LLMResponse

from main import PERMISSION_ERROR, RepeaterPlugin
from repeater_config import (
    DEFAULT_INTERRUPT_MUTE_TEXT,
    DEFAULT_INTERRUPT_TEXT,
    DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
    DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
    RepeaterSettings,
    build_settings,
)
from repeater_messages import (
    RepeatableMessage,
    fingerprint as make_fingerprint,
    repeatable_message,
)
from repeater_service import (
    GroupRepeaterState,
    RepeaterStateService,
)
from intelligent_history import (
    IntelligentActionRecord,
    IntelligentHistoryStore,
    RETENTION_MS,
)


class ConfigSchemaTest(unittest.TestCase):
    def test_slider_fields_use_expected_ranges(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        expected_sliders = {
            "repeat_threshold": ("int", {"min": 2, "max": 50, "step": 1}),
            "repeat_probability": ("float", {"min": 0, "max": 1, "step": 0.01}),
            "interrupt_probability": ("float", {"min": 0, "max": 1, "step": 0.01}),
            "interrupt_mute_duration_min": (
                "int",
                {"min": 1, "max": 3600, "step": 1},
            ),
            "interrupt_mute_duration_max": (
                "int",
                {"min": 1, "max": 3600, "step": 1},
            ),
            "interrupt_mute_probability": (
                "float",
                {"min": 0, "max": 1, "step": 0.01},
            ),
        }
        for key, (field_type, slider) in expected_sliders.items():
            with self.subTest(key=key):
                field = schema[key]
                self.assertEqual(field["type"], field_type)
                self.assertEqual(field["slider"], slider)

    def test_configuration_schema_uses_feature_group_order(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(
            list(schema.keys()),
            [
                "default_enabled",
                "repeat_disabled_group_ids",
                "repeat_threshold",
                "repeat_probability",
                "interrupt_default_enabled",
                "interrupt_disabled_group_ids",
                "interrupt_probability",
                "interrupt_texts",
                "interrupt_mute_enabled",
                "interrupt_mute_disabled_group_ids",
                "interrupt_mute_probability",
                "interrupt_mute_duration_min",
                "interrupt_mute_duration_max",
                "interrupt_mute_texts",
                "intelligent_interrupt_provider_id",
                "intelligent_interrupt_model",
                "intelligent_interrupt_enabled",
                "intelligent_interrupt_prompt",
                "intelligent_interrupt_mute_enabled",
                "intelligent_interrupt_mute_prompt",
            ],
        )

    def test_interrupt_mute_fields_have_expected_defaults(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["interrupt_mute_enabled"]["type"], "bool")
        self.assertFalse(schema["interrupt_mute_enabled"]["default"])
        self.assertEqual(schema["interrupt_mute_disabled_group_ids"]["type"], "list")
        self.assertEqual(schema["interrupt_mute_disabled_group_ids"]["default"], [])
        self.assertEqual(schema["interrupt_mute_duration_min"]["default"], 1)
        self.assertEqual(schema["interrupt_mute_duration_max"]["default"], 15)
        self.assertEqual(schema["interrupt_mute_probability"]["default"], 0.05)
        self.assertEqual(len(schema["interrupt_mute_texts"]["default"]), 5)

    def test_shared_intelligent_text_fields_have_expected_defaults(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["intelligent_interrupt_provider_id"]["type"], "string")
        self.assertEqual(schema["intelligent_interrupt_provider_id"]["default"], "")
        self.assertEqual(
            schema["intelligent_interrupt_provider_id"]["_special"],
            "select_provider",
        )
        self.assertEqual(schema["intelligent_interrupt_model"]["type"], "string")
        self.assertEqual(schema["intelligent_interrupt_model"]["default"], "")

    def test_intelligent_interrupt_fields_have_expected_defaults(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["intelligent_interrupt_enabled"]["type"], "bool")
        self.assertFalse(schema["intelligent_interrupt_enabled"]["default"])
        self.assertEqual(schema["intelligent_interrupt_prompt"]["type"], "text")
        self.assertEqual(
            schema["intelligent_interrupt_prompt"]["default"],
            DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
        )

    def test_intelligent_interrupt_mute_fields_have_expected_defaults(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["intelligent_interrupt_mute_enabled"]["type"], "bool")
        self.assertFalse(schema["intelligent_interrupt_mute_enabled"]["default"])
        self.assertEqual(schema["intelligent_interrupt_mute_prompt"]["type"], "text")
        self.assertEqual(
            schema["intelligent_interrupt_mute_prompt"]["default"],
            DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
        )

    def test_configuration_schema_is_complete_and_descriptive(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        expected_schema = {
            "default_enabled": {
                "type": "bool",
                "default": True,
                "description": "默认开启复读",
                "hint": "未被群级开关单独设置的群是否默认开启复读。",
            },
            "repeat_disabled_group_ids": {
                "type": "list",
                "default": [],
                "items": {"type": "string"},
                "description": "关闭复读的群号",
                "hint": "由复读开关指令维护；列表中的群不触发普通复读。",
            },
            "repeat_threshold": {
                "type": "int",
                "default": 3,
                "slider": {"min": 2, "max": 50, "step": 1},
                "description": "复读触发人数",
                "hint": "同一内容需由多少名不同用户发送（含首位）才达到复读条件。",
            },
            "repeat_probability": {
                "type": "float",
                "default": 0.3,
                "slider": {"min": 0, "max": 1, "step": 0.01},
                "description": "复读概率",
                "hint": "达到复读条件且未命中打断时，回发原消息的概率（0%–100%）。",
            },
            "interrupt_default_enabled": {
                "type": "bool",
                "default": True,
                "description": "默认开启打断复读",
                "hint": "未被群级开关单独设置的群是否默认开启打断复读。",
            },
            "interrupt_disabled_group_ids": {
                "type": "list",
                "default": [],
                "items": {"type": "string"},
                "description": "关闭打断复读的群号",
                "hint": "由打断复读开关指令维护；列表中的群不触发打断复读。",
            },
            "interrupt_probability": {
                "type": "float",
                "default": 0.1,
                "slider": {"min": 0, "max": 1, "step": 0.01},
                "description": "打断概率",
                "hint": "达到阈值后优先发送打断文本的概率（0%–100%）。",
            },
            "interrupt_texts": {
                "type": "list",
                "default": [
                    "叮——复读结界已启动，下一位请说点新鲜的！",
                    "抓到一群小鹦鹉，统统没收作案声带～",
                    "前方禁止复制粘贴，本喵要开始随机巡逻啦！",
                    "复读能量过载！啪叽一下，频道已被我掐断。",
                    "同一句再来一遍就要收费啦，先欠我一颗糖！",
                ],
                "items": {"type": "string"},
                "description": "随机打断文案",
                "hint": "智能打断关闭、不可用或生成失败时，从此列表随机选择；留空使用“打断！”。",
            },
            "interrupt_mute_enabled": {
                "type": "bool",
                "default": False,
                "description": "打断复读禁言",
                "hint": "开启后，打断复读触发时按概率尝试禁言触发用户；默认关闭。",
            },
            "interrupt_mute_disabled_group_ids": {
                "type": "list",
                "default": [],
                "items": {"type": "string"},
                "description": "关闭禁言的群号",
                "hint": "列表中的群不执行打断复读禁言。",
            },
            "interrupt_mute_probability": {
                "type": "float",
                "default": 0.05,
                "slider": {"min": 0, "max": 1, "step": 0.01},
                "description": "禁言概率",
                "hint": "打断复读触发后尝试禁言的概率（0%–100%）。",
            },
            "interrupt_mute_duration_min": {
                "type": "int",
                "default": 1,
                "slider": {"min": 1, "max": 3600, "step": 1},
                "description": "最短禁言时长",
                "hint": "随机禁言时长的下限，单位秒；应不大于最长禁言时长。",
            },
            "interrupt_mute_duration_max": {
                "type": "int",
                "default": 15,
                "slider": {"min": 1, "max": 3600, "step": 1},
                "description": "最长禁言时长",
                "hint": "随机禁言时长的上限，单位秒；应不小于最短禁言时长。",
            },
            "interrupt_mute_texts": {
                "type": "list",
                "default": [
                    "你以为你打断了复读？错！你已经被打断了人生 {time} 秒 🤐",
                    "打断复读？不好意思，你也被打断发言权了，{time}秒后见 😏",
                    "恭喜 {user} 同学成功触发【打断复读禁言】成就，奖励禁言 {time} 秒 🎉",
                    "复读虽可恶，打断更该罚！{user} 请安静 {time} 秒反思一下 🤔",
                    "检测到反复读行为，根据群规第114514条，{user} 将被禁言 {time} 秒 ⚖️",
                ],
                "items": {"type": "string"},
                "description": "禁言提示文案",
                "hint": "智能禁言提示关闭、不可用或生成失败时随机发送；支持 {user}（被禁言用户）和 {time}（禁言秒数）占位符；留空使用默认文案。",
            },
            "intelligent_interrupt_provider_id": {
                "type": "string",
                "default": "",
                "_special": "select_provider",
                "description": "智能文案供应商 ID",
                "hint": "可在“智能文案测试”页面从下拉列表选择已配置的聊天供应商；手动编辑配置文件时填写其 ID。用于智能打断和智能禁言提示；留空跟随触发会话，页面无法直接测试该模式。",
            },
            "intelligent_interrupt_model": {
                "type": "string",
                "default": "",
                "description": "智能文案模型",
                "hint": "可在“智能文案测试”页面从所选供应商的候选列表选择，或手动输入未枚举的模型 ID；用于智能打断和智能禁言提示；留空使用最终供应商的默认模型。",
            },
            "intelligent_interrupt_enabled": {
                "type": "bool",
                "default": False,
                "description": "智能打断复读",
                "hint": "开启后，打断命中时使用 AstrBot 已配置的聊天供应商生成一条打断文案；默认关闭。",
            },
            "intelligent_interrupt_prompt": {
                "type": "text",
                "default": DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
                "description": "智能打断提示词",
                "hint": "作为生成打断文案的 LLM 系统提示词；空值或非法值回退默认提示词。",
            },
            "intelligent_interrupt_mute_enabled": {
                "type": "bool",
                "default": False,
                "description": "智能禁言提示",
                "hint": "开启后，禁言成功时使用与智能打断相同的聊天供应商和模型生成一条提示文案；默认关闭。",
            },
            "intelligent_interrupt_mute_prompt": {
                "type": "text",
                "default": DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
                "description": "智能禁言提示词",
                "hint": "作为生成禁言提示文案的 LLM 系统提示词；空值或非法值回退默认提示词。",
            },
        }
        self.maxDiff = None
        self.assertEqual(schema, expected_schema)


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

    def test_config_imports_directly_and_as_plugin_package(self) -> None:
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
                [sys.executable, "-c", "import repeater_config"],
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
                    "'data.plugins.astrbot_plugin_repeater.repeater_config'"
                    ")",
                ],
                cwd=temporary_root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(package_import.returncode, 0, package_import.stderr)


class ConfigModuleTest(unittest.TestCase):
    def test_build_settings_preserves_validation_and_warning_contract(self) -> None:
        class RecordingLogger:
            def __init__(self) -> None:
                self.warnings: list[str] = []

            def warning(self, message: str) -> None:
                self.warnings.append(message)

        logger = RecordingLogger()
        settings = build_settings(
            {
                "repeat_disabled_group_ids": [1, " group ", True, ""],
                "interrupt_disabled_group_ids": "invalid",
                "repeat_threshold": True,
                "repeat_probability": True,
                "default_enabled": 1,
                "interrupt_probability": True,
                "interrupt_texts": (" 打断甲 ", "", 1),
                "interrupt_default_enabled": "yes",
                "intelligent_interrupt_enabled": "yes",
                "intelligent_interrupt_provider_id": 1,
                "intelligent_interrupt_model": [],
                "intelligent_interrupt_prompt": "   ",
                "interrupt_mute_enabled": 0,
                "interrupt_mute_duration_min": 60,
                "interrupt_mute_duration_max": 30,
                "interrupt_mute_probability": True,
                "interrupt_mute_texts": "invalid",
                "intelligent_interrupt_mute_enabled": "yes",
                "intelligent_interrupt_mute_prompt": "   ",
            },
            logger,
        )

        self.assertEqual(settings.repeat_disabled_group_ids, {"1", "group"})
        self.assertEqual(settings.interrupt_disabled_group_ids, set())
        self.assertEqual(settings.repeat_threshold, 3)
        self.assertEqual(settings.repeat_probability, 0.3)
        self.assertTrue(settings.default_enabled)
        self.assertEqual(settings.interrupt_probability, 0.1)
        self.assertEqual(settings.interrupt_texts, ("打断甲",))
        self.assertTrue(settings.interrupt_default_enabled)
        self.assertFalse(settings.intelligent_interrupt_enabled)
        self.assertEqual(settings.intelligent_interrupt_provider_id, "")
        self.assertEqual(settings.intelligent_interrupt_model, "")
        self.assertEqual(
            settings.intelligent_interrupt_prompt,
            DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
        )
        self.assertFalse(settings.interrupt_mute_enabled)
        self.assertEqual(settings.interrupt_mute_duration_min, 60)
        self.assertEqual(settings.interrupt_mute_duration_max, 60)
        self.assertEqual(settings.interrupt_mute_probability, 0.05)
        self.assertEqual(settings.interrupt_mute_texts, (DEFAULT_INTERRUPT_MUTE_TEXT,))
        self.assertFalse(settings.intelligent_interrupt_mute_enabled)
        self.assertEqual(
            settings.intelligent_interrupt_mute_prompt,
            DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
        )
        self.assertEqual(
            logger.warnings,
            [
                "[repeater] default_enabled 非法(1)，回退为 True",
                "[repeater] repeat_threshold 非法(True)，回退为 3",
                "[repeater] repeat_probability 非法(True)，回退为 0.3",
                "[repeater] interrupt_default_enabled 非法(yes)，回退为 True",
                "[repeater] interrupt_disabled_group_ids 非法，使用空列表",
                "[repeater] interrupt_probability 非法(True)，回退为 0.1",
                "[repeater] interrupt_mute_enabled 非法(0)，回退为 False",
                "[repeater] interrupt_mute_probability 非法(True)，回退为 0.05",
                "[repeater] interrupt_mute_duration_max 小于 "
                "interrupt_mute_duration_min，使用下限值",
                "[repeater] interrupt_mute_texts 非法或为空，回退为默认禁言文本",
                "[repeater] intelligent_interrupt_provider_id 非法(1)，回退为空字符串",
                "[repeater] intelligent_interrupt_model 非法([])，回退为空字符串",
                "[repeater] intelligent_interrupt_enabled 非法(yes)，回退为 False",
                "[repeater] intelligent_interrupt_prompt 非法或为空，回退为默认智能打断提示词",
                "[repeater] intelligent_interrupt_mute_enabled 非法(yes)，回退为 False",
                "[repeater] intelligent_interrupt_mute_prompt 非法或为空，回退为默认智能禁言提示词",
            ],
        )


class FakeBot:
    def __init__(self) -> None:
        self.actions: list[tuple[str, dict[str, object]]] = []

    async def call_action(self, action: str, **payload: object) -> None:
        self.actions.append((action, payload))


class FakeContext:
    def __init__(
        self,
        *,
        provider_id: str = "current-provider",
        response: LLMResponse | None = None,
        provider_error: BaseException | None = None,
        llm_error: BaseException | None = None,
        before_error: Callable[[], None] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.response = (
            response
            if response is not None
            else LLMResponse("assistant", completion_text="智能打断")
        )
        self.provider_error = provider_error
        self.llm_error = llm_error
        self.before_error = before_error
        self.provider_calls: list[str] = []
        self.llm_calls: list[dict[str, object]] = []

    def get_current_chat_provider_id(self, umo: str) -> str:
        self.provider_calls.append(umo)
        if self.provider_error is not None:
            if self.before_error is not None:
                self.before_error()
            raise self.provider_error
        return self.provider_id

    async def llm_generate(
        self,
        *,
        chat_provider_id: str,
        prompt: str | None = None,
        system_prompt: str | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        self.llm_calls.append(
            {
                "chat_provider_id": chat_provider_id,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "kwargs": kwargs,
            },
        )
        if self.llm_error is not None:
            if self.before_error is not None:
                self.before_error()
            raise self.llm_error
        return self.response


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
        bot: object | None = None,
        sender_name: str | None = None,
        self_id: str | None = None,
    ) -> None:
        self.group_id = group_id
        self.unified_msg_origin = f"onebot:group:{group_id}"
        self.sender_id = sender_id
        self.text = text
        self.is_at_or_wake_command = wake
        message_chain = chain if chain is not None else ([Plain(text)] if text else [])
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            message=message_chain,
            raw_message=raw_message,
            self_id=self_id,
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
        self.bot = bot
        self.sender_name = sender_name
        self.self_id = self_id or "bot"

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
        return self.self_id

    def get_sender_name(self) -> str:
        return self.sender_name or self.sender_id

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


class AsyncMemoryConfig(MemoryConfig):
    def __init__(
        self,
        values: dict | None = None,
        *,
        committed: bool = True,
    ) -> None:
        super().__init__(values)
        self.committed = committed

    async def save_config_async(self, updates: dict) -> bool:
        self.update(updates)
        self.save_count += 1
        return self.committed


class SnapshotMemoryConfig(MemoryConfig):
    def __init__(
        self,
        values: dict | None = None,
        *,
        committed: bool = True,
        mutate_after_write: bool = False,
    ) -> None:
        super().__init__(values)
        self.committed = committed
        self.mutate_after_write = mutate_after_write
        self._save_state_lock = threading.RLock()
        self._save_revision = 0
        self.written_snapshots: list[dict] = []

    def _write_config_snapshot(
        self,
        snapshot: dict,
        _revision: int,
        _retries: int,
    ) -> bool:
        self.written_snapshots.append(copy.deepcopy(snapshot))
        self.save_count += 1
        if self.mutate_after_write:
            self.update(
                {
                    "intelligent_interrupt_provider_id": "later-provider",
                    "intelligent_interrupt_model": "later-model",
                },
            )
        return self.committed


class BlockingAsyncMemoryConfig(MemoryConfig):
    def __init__(self, values: dict | None = None) -> None:
        super().__init__(values)
        self.save_started = asyncio.Event()
        self.release_save = asyncio.Event()

    async def save_config_async(self, updates: dict) -> bool:
        self.update(updates)
        self.save_started.set()
        await self.release_save.wait()
        self.save_count += 1
        return True


class FakeChatProvider:
    def __init__(
        self,
        provider_id: str,
        models: list[str] | None = None,
        *,
        current_model: str = "",
        models_error: BaseException | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.models = models or []
        self.current_model = current_model
        self.models_error = models_error

    def meta(self):
        return SimpleNamespace(id=self.provider_id, model=self.current_model)

    async def get_models(self) -> list[str]:
        if self.models_error is not None:
            raise self.models_error
        return self.models


class FakePageContext(FakeContext):
    def __init__(
        self,
        *,
        providers: list[FakeChatProvider] | None = None,
        providers_error: BaseException | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.providers = providers or [FakeChatProvider("provider-a", ["model-a"])]
        self.providers_error = providers_error
        self.registered_web_apis: list[tuple[str, object, list[str], str]] = []

    def get_all_providers(self) -> list[FakeChatProvider]:
        if self.providers_error is not None:
            raise self.providers_error
        return self.providers

    def register_web_api(
        self,
        route: str,
        view_handler: object,
        methods: list[str],
        desc: str,
    ) -> None:
        self.registered_web_apis.append((route, view_handler, methods, desc))


class BlockingPageContext(FakePageContext):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.generation_started = asyncio.Event()
        self.release_generation = asyncio.Event()

    async def llm_generate(self, **kwargs: object) -> LLMResponse:
        self.generation_started.set()
        await self.release_generation.wait()
        return await super().llm_generate(**kwargs)


class FakePageRequest:
    def __init__(self, *, query: dict[str, str] | None = None, body=None) -> None:
        self.query = query or {}
        self._body = body

    async def json(self, default=None):
        return self._body if self._body is not None else default


class FailingHistoryStore:
    async def initialize(self) -> int:
        return 0

    async def append(self, _record: IntelligentActionRecord) -> None:
        raise RuntimeError("history unavailable")

    async def purge_expired(self) -> int:
        return 0


def response_payload(response) -> dict:
    return json.loads(response.body.decode("utf-8"))

class MemoryRepeater(RepeaterPlugin):
    def __init__(
        self,
        store: dict,
        config: dict | None = None,
        *,
        put_delay: float = 0,
        context: object | None = None,
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
        super().__init__(context, effective_config)
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


class GroupRepeaterStateSerializationTest(unittest.TestCase):
    def test_round_trip_preserves_all_state_fields_and_sorts_collections(self) -> None:
        raw_state = {
            "enabled_override": True,
            "interrupt_enabled_override": True,
            "last_fingerprint": "current-fingerprint",
            "repeated_users": ["user-z", 7, "user-a"],
            "repeated_fingerprints": ["repeat-z", "repeat-a"],
            "pending_fingerprints": ["pending-z", 3, "pending-a"],
            "last_message_id": "message-42",
        }

        restored = GroupRepeaterState.from_dict(raw_state)

        self.assertTrue(restored.enabled_override)
        self.assertTrue(restored.interrupt_enabled_override)
        self.assertEqual(restored.last_fingerprint, "current-fingerprint")
        self.assertEqual(restored.repeated_users, {"user-z", "7", "user-a"})
        self.assertEqual(
            restored.repeated_fingerprints,
            {"repeat-z", "repeat-a"},
        )
        self.assertEqual(
            restored.pending_fingerprints,
            {"pending-z", "3", "pending-a"},
        )
        self.assertEqual(restored.last_message_id, "message-42")
        self.assertEqual(
            restored.to_dict(),
            {
                "enabled_override": True,
                "interrupt_enabled_override": True,
                "last_fingerprint": "current-fingerprint",
                "repeated_users": ["7", "user-a", "user-z"],
                "repeated_fingerprints": ["repeat-a", "repeat-z"],
                "pending_fingerprints": ["3", "pending-a", "pending-z"],
                "last_message_id": "message-42",
            },
        )


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
            interrupt_mute_disabled_group_ids=set(),
            repeat_threshold=2,
            repeat_probability=1.0,
            default_enabled=True,
            interrupt_probability=0.0,
            interrupt_texts=("打断！",),
            interrupt_default_enabled=False,
            intelligent_interrupt_enabled=False,
            intelligent_interrupt_provider_id="",
            intelligent_interrupt_model="",
            intelligent_interrupt_prompt=DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
            interrupt_mute_enabled=False,
            interrupt_mute_duration_min=1,
            interrupt_mute_duration_max=15,
            interrupt_mute_probability=0.0,
            interrupt_mute_texts=("用户{user}因命中打断复读禁言策略而被禁言{time}s",),
            intelligent_interrupt_mute_enabled=False,
            intelligent_interrupt_mute_prompt=DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
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
        self.assertEqual(attempt.sender_id, "B")
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
        self.assertEqual(
            RepeaterPlugin(None, {}).state_service.settings.repeat_probability, 0.3
        )
        self.assertEqual(
            RepeaterPlugin(
                None, {"repeat_probability": "invalid"}
            ).state_service.settings.repeat_probability,
            0.3,
        )

    def test_interrupt_config_defaults_and_invalid_values(self) -> None:
        plugin = RepeaterPlugin(None, {})
        self.assertTrue(plugin.state_service.settings.interrupt_default_enabled)
        self.assertEqual(plugin.state_service.settings.interrupt_probability, 0.1)
        self.assertEqual(
            plugin.state_service.settings.interrupt_texts, (DEFAULT_INTERRUPT_TEXT,)
        )
        self.assertEqual(len(plugin.state_service.settings.interrupt_texts), 1)
        self.assertFalse(plugin.state_service.settings.intelligent_interrupt_enabled)
        self.assertEqual(
            plugin.state_service.settings.intelligent_interrupt_provider_id, ""
        )
        self.assertEqual(plugin.state_service.settings.intelligent_interrupt_model, "")
        self.assertEqual(
            plugin.state_service.settings.intelligent_interrupt_prompt,
            DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
        )
        self.assertFalse(plugin.state_service.settings.intelligent_interrupt_mute_enabled)
        self.assertEqual(
            plugin.state_service.settings.intelligent_interrupt_mute_prompt,
            DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
        )

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
        self.assertEqual(
            invalid.state_service.settings.interrupt_texts, (DEFAULT_INTERRUPT_TEXT,)
        )

        empty = RepeaterPlugin(None, {"interrupt_texts": []})
        self.assertEqual(
            empty.state_service.settings.interrupt_texts, (DEFAULT_INTERRUPT_TEXT,)
        )

        custom = RepeaterPlugin(
            None,
            {"interrupt_texts": [" 第一条 ", "", 2, "第二条"]},
        )
        self.assertEqual(
            custom.state_service.settings.interrupt_texts, ("第一条", "第二条")
        )

        intelligent = RepeaterPlugin(
            None,
            {
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": " provider-a ",
                "intelligent_interrupt_model": " model-b ",
                "intelligent_interrupt_prompt": "   ",
                "intelligent_interrupt_mute_enabled": "yes",
                "intelligent_interrupt_mute_prompt": "   ",
            },
        ).state_service.settings
        self.assertTrue(intelligent.intelligent_interrupt_enabled)
        self.assertEqual(intelligent.intelligent_interrupt_provider_id, "provider-a")
        self.assertEqual(intelligent.intelligent_interrupt_model, "model-b")
        self.assertEqual(
            intelligent.intelligent_interrupt_prompt,
            DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
        )
        self.assertFalse(intelligent.intelligent_interrupt_mute_enabled)
        self.assertEqual(
            intelligent.intelligent_interrupt_mute_prompt,
            DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
        )
        intelligent_mute = RepeaterPlugin(
            None,
            {
                "intelligent_interrupt_mute_enabled": True,
                "intelligent_interrupt_mute_prompt": " 自定义禁言提示 ",
            },
        ).state_service.settings
        self.assertTrue(intelligent_mute.intelligent_interrupt_mute_enabled)
        self.assertEqual(
            intelligent_mute.intelligent_interrupt_mute_prompt,
            "自定义禁言提示",
        )

    def test_interrupt_mute_config_validates_and_persists_groups(self) -> None:
        defaults = RepeaterPlugin(None, {}).state_service.settings
        self.assertEqual(defaults.interrupt_mute_duration_min, 1)
        self.assertEqual(defaults.interrupt_mute_duration_max, 15)

        config = MemoryConfig(
            {
                "interrupt_mute_enabled": True,
                "interrupt_mute_disabled_group_ids": [42, " blocked "],
                "interrupt_mute_duration_min": 120,
                "interrupt_mute_duration_max": 60,
                "interrupt_mute_probability": 0.25,
                "interrupt_mute_texts": [" {user} {time} ", "", 1],
            },
        )
        plugin = RepeaterPlugin(None, config)
        settings = plugin.state_service.settings

        self.assertTrue(settings.interrupt_mute_enabled)
        self.assertEqual(settings.interrupt_mute_disabled_group_ids, {"42", "blocked"})
        self.assertEqual(settings.interrupt_mute_duration_min, 120)
        self.assertEqual(settings.interrupt_mute_duration_max, 120)
        self.assertEqual(settings.interrupt_mute_probability, 0.25)
        self.assertEqual(settings.interrupt_mute_texts, ("{user} {time}",))
        self.assertTrue(plugin.state_service.is_interrupt_mute_enabled("enabled"))
        self.assertFalse(plugin.state_service.is_interrupt_mute_enabled("blocked"))

        settings.save_config()
        self.assertEqual(
            config["interrupt_mute_disabled_group_ids"],
            ["42", "blocked"],
        )

        invalid = RepeaterPlugin(
            None,
            {
                "interrupt_mute_enabled": "yes",
                "interrupt_mute_disabled_group_ids": "invalid",
                "interrupt_mute_duration_min": 0,
                "interrupt_mute_duration_max": True,
                "interrupt_mute_probability": 1.1,
                "interrupt_mute_texts": [],
            },
        ).state_service.settings
        self.assertFalse(invalid.interrupt_mute_enabled)
        self.assertEqual(invalid.interrupt_mute_disabled_group_ids, set())
        self.assertEqual(invalid.interrupt_mute_duration_min, 1)
        self.assertEqual(invalid.interrupt_mute_duration_max, 15)
        self.assertEqual(invalid.interrupt_mute_probability, 0.05)
        self.assertEqual(invalid.interrupt_mute_texts, (DEFAULT_INTERRUPT_MUTE_TEXT,))

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
        self.assertEqual(
            plugin.state_service.group_states["different-media"].repeated_users, {"B"}
        )

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

    async def test_interrupt_mute_bans_interrupter_and_sends_notice(self) -> None:
        bot = FakeBot()
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["打断！"],
                "interrupt_mute_enabled": True,
                "interrupt_mute_duration_min": 30,
                "interrupt_mute_duration_max": 30,
                "interrupt_mute_probability": 1.0,
                "interrupt_mute_texts": ["{user} 被禁言 {time}s"],
            },
        )
        await plugin.initialize()
        first = FakeEvent("10001", "A", "复读内容", "1")
        interrupter = FakeEvent(
            "10001",
            "12345",
            "复读内容",
            "2",
            bot=bot,
            sender_name="打断者",
            self_id="bot",
            group_admins=["bot"],
        )

        with (
            patch("repeater_service.random.random", return_value=0.0),
            patch("main.random.random", return_value=0.0),
        ):
            await plugin.on_group_message(first)
            await plugin.on_group_message(interrupter)

        self.assertEqual(interrupter.sent, ["打断！", "打断者 被禁言 30s"])
        self.assertEqual(
            bot.actions,
            [
                (
                    "set_group_ban",
                    {
                        "group_id": 10001,
                        "user_id": 12345,
                        "duration": 30,
                        "self_id": "bot",
                    },
                ),
            ],
        )
        self.assertTrue(interrupter.stopped)
        self.assertIn(
            make_fingerprint("复读内容"),
            plugin.state_service.group_states["10001"].repeated_fingerprints,
        )

    async def test_intelligent_interrupt_mute_uses_shared_provider_and_model(
        self,
    ) -> None:
        bot = FakeBot()
        context = FakeContext(
            response=LLMResponse("assistant", completion_text="智能禁言"),
        )
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["打断！"],
                "intelligent_interrupt_enabled": False,
                "intelligent_interrupt_provider_id": "provider-a",
                "intelligent_interrupt_model": "model-b",
                "interrupt_mute_enabled": True,
                "interrupt_mute_duration_min": 30,
                "interrupt_mute_duration_max": 30,
                "interrupt_mute_probability": 1.0,
                "interrupt_mute_texts": ["{user} 被禁言 {time}s"],
                "intelligent_interrupt_mute_enabled": True,
            },
            context=context,
        )
        await plugin.initialize()
        first = FakeEvent("10003", "A", "复读内容", "1")
        interrupter = FakeEvent(
            "10003",
            "12345",
            "复读内容",
            "2",
            bot=bot,
            sender_name="打断者",
            self_id="bot",
            group_admins=["bot"],
        )

        with (
            patch("repeater_service.random.random", return_value=0.0),
            patch("main.random.random", return_value=0.0),
        ):
            await plugin.on_group_message(first)
            await plugin.on_group_message(interrupter)

        self.assertEqual(interrupter.sent, ["打断！", "智能禁言"])
        self.assertEqual(
            bot.actions,
            [
                (
                    "set_group_ban",
                    {
                        "group_id": 10003,
                        "user_id": 12345,
                        "duration": 30,
                        "self_id": "bot",
                    },
                ),
            ],
        )
        self.assertEqual(context.provider_calls, [])
        self.assertEqual(
            context.llm_calls,
            [
                {
                    "chat_provider_id": "provider-a",
                    "prompt": "被禁言用户：打断者\n禁言时长：30秒",
                    "system_prompt": DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
                    "kwargs": {"model": "model-b"},
                },
            ],
        )

    async def test_intelligent_interrupt_mute_uses_current_provider_and_default_model(
        self,
    ) -> None:
        bot = FakeBot()
        context = FakeContext(
            provider_id="session-provider",
            response=LLMResponse("assistant", completion_text="会话禁言"),
        )
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["打断！"],
                "intelligent_interrupt_enabled": False,
                "interrupt_mute_enabled": True,
                "interrupt_mute_duration_min": 30,
                "interrupt_mute_duration_max": 30,
                "interrupt_mute_probability": 1.0,
                "interrupt_mute_texts": ["{user} 被禁言 {time}s"],
                "intelligent_interrupt_mute_enabled": True,
            },
            context=context,
        )
        await plugin.initialize()
        first = FakeEvent("10004", "A", "复读内容", "1")
        interrupter = FakeEvent(
            "10004",
            "12345",
            "复读内容",
            "2",
            bot=bot,
            sender_name="打断者",
            self_id="bot",
            group_admins=["bot"],
        )

        with (
            patch("repeater_service.random.random", return_value=0.0),
            patch("main.random.random", return_value=0.0),
        ):
            await plugin.on_group_message(first)
            await plugin.on_group_message(interrupter)

        self.assertEqual(interrupter.sent, ["打断！", "会话禁言"])
        self.assertEqual(context.provider_calls, [interrupter.unified_msg_origin])
        self.assertEqual(
            context.llm_calls,
            [
                {
                    "chat_provider_id": "session-provider",
                    "prompt": "被禁言用户：打断者\n禁言时长：30秒",
                    "system_prompt": DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
                    "kwargs": {},
                },
            ],
        )

    async def test_disabled_intelligent_interrupt_mute_uses_static_text_without_llm(
        self,
    ) -> None:
        bot = FakeBot()
        context = FakeContext()
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["打断！"],
                "intelligent_interrupt_enabled": False,
                "interrupt_mute_enabled": True,
                "interrupt_mute_duration_min": 30,
                "interrupt_mute_duration_max": 30,
                "interrupt_mute_probability": 1.0,
                "interrupt_mute_texts": ["{user} 被禁言 {time}s"],
                "intelligent_interrupt_mute_enabled": False,
            },
            context=context,
        )
        await plugin.initialize()
        first = FakeEvent("10005", "A", "复读内容", "1")
        interrupter = FakeEvent(
            "10005",
            "12345",
            "复读内容",
            "2",
            bot=bot,
            sender_name="打断者",
            self_id="bot",
            group_admins=["bot"],
        )

        with (
            patch("repeater_service.random.random", return_value=0.0),
            patch("main.random.random", return_value=0.0),
        ):
            await plugin.on_group_message(first)
            await plugin.on_group_message(interrupter)

        self.assertEqual(interrupter.sent, ["打断！", "打断者 被禁言 30s"])
        self.assertEqual(len(bot.actions), 1)
        self.assertEqual(context.provider_calls, [])
        self.assertEqual(context.llm_calls, [])

    async def test_intelligent_interrupt_mute_falls_back_on_generation_failure(
        self,
    ) -> None:
        cases = (
            (
                "provider resolution",
                FakeContext(provider_error=RuntimeError("provider unavailable")),
                {},
                1,
                0,
            ),
            (
                "LLM request",
                FakeContext(llm_error=RuntimeError("LLM unavailable")),
                {"intelligent_interrupt_provider_id": "provider-a"},
                0,
                1,
            ),
            (
                "non-assistant response",
                FakeContext(
                    response=LLMResponse("err", completion_text="provider failure"),
                ),
                {"intelligent_interrupt_provider_id": "provider-a"},
                0,
                1,
            ),
            (
                "empty completion",
                FakeContext(
                    response=LLMResponse("assistant", completion_text="   "),
                ),
                {"intelligent_interrupt_provider_id": "provider-a"},
                0,
                1,
            ),
        )
        for index, (
            name,
            context,
            provider_config,
            expected_provider_calls,
            expected_llm_calls,
        ) in enumerate(cases, start=1):
            with self.subTest(name=name):
                bot = FakeBot()
                store: dict = {}
                group_id = str(10100 + index)
                plugin = MemoryRepeater(
                    store,
                    {
                        "repeat_threshold": 2,
                        "interrupt_default_enabled": True,
                        "interrupt_probability": 1.0,
                        "interrupt_texts": ["打断！"],
                        "intelligent_interrupt_enabled": False,
                        "interrupt_mute_enabled": True,
                        "interrupt_mute_duration_min": 30,
                        "interrupt_mute_duration_max": 30,
                        "interrupt_mute_probability": 1.0,
                        "interrupt_mute_texts": ["{user} 被禁言 {time}s"],
                        "intelligent_interrupt_mute_enabled": True,
                        **provider_config,
                    },
                    context=context,
                )
                await plugin.initialize()
                first = FakeEvent(group_id, "A", "故障复读", "1")
                interrupter = FakeEvent(
                    group_id,
                    "12345",
                    "故障复读",
                    "2",
                    bot=bot,
                    sender_name="打断者",
                    self_id="bot",
                    group_admins=["bot"],
                )

                with (
                    patch("repeater_service.random.random", return_value=0.0),
                    patch("main.random.random", return_value=0.0),
                ):
                    await plugin.on_group_message(first)
                    await plugin.on_group_message(interrupter)

                fingerprint = make_fingerprint("故障复读")
                state = plugin.state_service.group_states[group_id]
                self.assertEqual(
                    interrupter.sent,
                    ["打断！", "打断者 被禁言 30s"],
                )
                self.assertEqual(len(bot.actions), 1)
                self.assertEqual(bot.actions[0][0], "set_group_ban")
                self.assertEqual(len(context.provider_calls), expected_provider_calls)
                self.assertEqual(len(context.llm_calls), expected_llm_calls)
                self.assertIn(fingerprint, state.repeated_fingerprints)
                self.assertNotIn(fingerprint, state.pending_fingerprints)

    async def test_intelligent_interrupt_mute_cancellation_preserves_completed_ban(
        self,
    ) -> None:
        bot = FakeBot()
        context = FakeContext(llm_error=asyncio.CancelledError())
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["打断！"],
                "intelligent_interrupt_enabled": False,
                "intelligent_interrupt_provider_id": "provider-a",
                "interrupt_mute_enabled": True,
                "interrupt_mute_duration_min": 30,
                "interrupt_mute_duration_max": 30,
                "interrupt_mute_probability": 1.0,
                "interrupt_mute_texts": ["{user} 被禁言 {time}s"],
                "intelligent_interrupt_mute_enabled": True,
            },
            context=context,
        )
        await plugin.initialize()
        first = FakeEvent("10006", "A", "取消禁言提示", "1")
        interrupter = FakeEvent(
            "10006",
            "12345",
            "取消禁言提示",
            "2",
            bot=bot,
            sender_name="打断者",
            self_id="bot",
            group_admins=["bot"],
        )

        with (
            patch("repeater_service.random.random", return_value=0.0),
            patch("main.random.random", return_value=0.0),
        ):
            await plugin.on_group_message(first)
            with self.assertRaises(asyncio.CancelledError):
                await plugin.on_group_message(interrupter)

        fingerprint = make_fingerprint("取消禁言提示")
        state = plugin.state_service.group_states["10006"]
        self.assertEqual(
            bot.actions,
            [
                (
                    "set_group_ban",
                    {
                        "group_id": 10006,
                        "user_id": 12345,
                        "duration": 30,
                        "self_id": "bot",
                    },
                ),
            ],
        )
        self.assertEqual(interrupter.sent, ["打断！"])
        self.assertTrue(interrupter.stopped)
        self.assertEqual(len(context.llm_calls), 1)
        self.assertIn(fingerprint, state.repeated_fingerprints)
        self.assertNotIn(fingerprint, state.pending_fingerprints)
        self.assertIn(
            fingerprint,
            store["group_states"]["10006"]["repeated_fingerprints"],
        )
        self.assertNotIn(
            fingerprint,
            store["group_states"]["10006"]["pending_fingerprints"],
        )

    async def test_interrupt_mute_skips_non_admin_bot(self) -> None:
        bot = FakeBot()
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["打断！"],
                "interrupt_mute_enabled": True,
                "interrupt_mute_probability": 1.0,
            },
        )
        await plugin.initialize()
        first = FakeEvent("10002", "A", "复读内容", "1")
        interrupter = FakeEvent(
            "10002",
            "12345",
            "复读内容",
            "2",
            bot=bot,
            group_admins=[],
        )

        with patch("repeater_service.random.random", return_value=0.0):
            await plugin.on_group_message(first)
            await plugin.on_group_message(interrupter)

        self.assertEqual(interrupter.sent, ["打断！"])
        self.assertEqual(bot.actions, [])

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
            patch(
                "repeater_service.random.choice", return_value="打断乙"
            ) as choice_mock,
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

    async def test_intelligent_interrupt_uses_llm_text_once(self) -> None:
        context = FakeContext(
            response=LLMResponse("assistant", completion_text="机智打断"),
        )
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
                "intelligent_interrupt_model": "model-b",
            },
            context=context,
        )
        await plugin.initialize()
        first = FakeEvent("intelligent-success", "A", "原始复读内容", "1")
        triggering_event = FakeEvent(
            "intelligent-success",
            "B",
            "原始复读内容",
            "2",
        )

        await plugin.on_group_message(first)
        await plugin.on_group_message(triggering_event)

        self.assertEqual(triggering_event.sent, ["机智打断"])
        self.assertTrue(triggering_event.stopped)
        self.assertEqual(context.provider_calls, [])
        self.assertEqual(
            context.llm_calls,
            [
                {
                    "chat_provider_id": "provider-a",
                    "prompt": "被复读的内容：原始复读内容",
                    "system_prompt": DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
                    "kwargs": {"model": "model-b"},
                },
            ],
        )
        fingerprint = make_fingerprint("原始复读内容")
        state = plugin.state_service.group_states["intelligent-success"]
        self.assertIn(fingerprint, state.repeated_fingerprints)
        self.assertNotIn(fingerprint, state.pending_fingerprints)

    async def test_intelligent_interrupt_uses_current_provider_and_default_model(
        self,
    ) -> None:
        context = FakeContext(
            provider_id="session-provider",
            response=LLMResponse("assistant", completion_text="会话打断"),
        )
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-current-provider", "A", "会话内容", "1"),
        )
        triggering_event = FakeEvent(
            "intelligent-current-provider",
            "B",
            "会话内容",
            "2",
        )

        await plugin.on_group_message(triggering_event)

        self.assertEqual(triggering_event.sent, ["会话打断"])
        self.assertEqual(
            context.provider_calls,
            [triggering_event.unified_msg_origin],
        )
        self.assertEqual(context.llm_calls[0]["chat_provider_id"], "session-provider")
        self.assertNotIn("model", context.llm_calls[0]["kwargs"])

    async def test_intelligent_interrupt_awaits_current_provider_resolution(
        self,
    ) -> None:
        context = FakeContext(
            response=LLMResponse("assistant", completion_text="异步供应商打断"),
        )

        async def resolve_current_provider(umo: str) -> str:
            context.provider_calls.append(umo)
            return "async-provider"

        context.get_current_chat_provider_id = resolve_current_provider
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-async-provider", "A", "异步内容", "1"),
        )
        triggering_event = FakeEvent(
            "intelligent-async-provider",
            "B",
            "异步内容",
            "2",
        )

        await plugin.on_group_message(triggering_event)

        self.assertEqual(triggering_event.sent, ["异步供应商打断"])
        self.assertEqual(
            context.provider_calls,
            [triggering_event.unified_msg_origin],
        )
        self.assertEqual(context.llm_calls[0]["chat_provider_id"], "async-provider")
        self.assertNotIn("model", context.llm_calls[0]["kwargs"])

    async def test_disabled_intelligent_interrupt_does_not_use_context(self) -> None:
        context = FakeContext()
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": False,
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-disabled", "A", "关闭智能", "1"),
        )
        triggering_event = FakeEvent("intelligent-disabled", "B", "关闭智能", "2")

        await plugin.on_group_message(triggering_event)

        self.assertEqual(triggering_event.sent, ["随机后备"])
        self.assertEqual(context.provider_calls, [])
        self.assertEqual(context.llm_calls, [])

    async def test_intelligent_interrupt_falls_back_when_provider_resolution_fails(
        self,
    ) -> None:
        context = FakeContext(provider_error=RuntimeError("provider unavailable"))
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-provider-error", "A", "供应商失败", "1"),
        )
        triggering_event = FakeEvent(
            "intelligent-provider-error",
            "B",
            "供应商失败",
            "2",
        )

        await plugin.on_group_message(triggering_event)

        fingerprint = make_fingerprint("供应商失败")
        state = plugin.state_service.group_states["intelligent-provider-error"]
        self.assertEqual(triggering_event.sent, ["随机后备"])
        self.assertTrue(triggering_event.stopped)
        self.assertEqual(context.provider_calls, [triggering_event.unified_msg_origin])
        self.assertEqual(context.llm_calls, [])
        self.assertIn(fingerprint, state.repeated_fingerprints)
        self.assertNotIn(fingerprint, state.pending_fingerprints)
        self.assertNotIn(
            fingerprint,
            store["group_states"]["intelligent-provider-error"]["pending_fingerprints"],
        )

    async def test_intelligent_interrupt_falls_back_when_llm_fails(self) -> None:
        context = FakeContext(llm_error=RuntimeError("LLM unavailable"))
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-llm-error", "A", "模型失败", "1"),
        )
        triggering_event = FakeEvent(
            "intelligent-llm-error",
            "B",
            "模型失败",
            "2",
        )

        await plugin.on_group_message(triggering_event)

        fingerprint = make_fingerprint("模型失败")
        state = plugin.state_service.group_states["intelligent-llm-error"]
        self.assertEqual(triggering_event.sent, ["随机后备"])
        self.assertTrue(triggering_event.stopped)
        self.assertEqual(context.provider_calls, [])
        self.assertEqual(len(context.llm_calls), 1)
        self.assertIn(fingerprint, state.repeated_fingerprints)
        self.assertNotIn(fingerprint, state.pending_fingerprints)

    async def test_intelligent_interrupt_falls_back_on_empty_completion(self) -> None:
        context = FakeContext(
            response=LLMResponse("assistant", completion_text="   "),
        )
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-empty", "A", "空响应", "1"),
        )
        triggering_event = FakeEvent("intelligent-empty", "B", "空响应", "2")

        await plugin.on_group_message(triggering_event)

        fingerprint = make_fingerprint("空响应")
        state = plugin.state_service.group_states["intelligent-empty"]
        self.assertEqual(triggering_event.sent, ["随机后备"])
        self.assertTrue(triggering_event.stopped)
        self.assertEqual(len(context.llm_calls), 1)
        self.assertIn(fingerprint, state.repeated_fingerprints)
        self.assertNotIn(fingerprint, state.pending_fingerprints)

    async def test_intelligent_interrupt_falls_back_on_error_response(self) -> None:
        context = FakeContext(
            response=LLMResponse("err", completion_text="provider failure"),
        )
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-error-response", "A", "错误响应", "1"),
        )
        triggering_event = FakeEvent(
            "intelligent-error-response",
            "B",
            "错误响应",
            "2",
        )

        await plugin.on_group_message(triggering_event)

        fingerprint = make_fingerprint("错误响应")
        state = plugin.state_service.group_states["intelligent-error-response"]
        self.assertEqual(triggering_event.sent, ["随机后备"])
        self.assertTrue(triggering_event.stopped)
        self.assertEqual(len(context.llm_calls), 1)
        self.assertIn(fingerprint, state.repeated_fingerprints)
        self.assertNotIn(fingerprint, state.pending_fingerprints)

    async def test_intelligent_interrupt_send_failure_rolls_back_and_retries(
        self,
    ) -> None:
        context = FakeContext(
            response=LLMResponse("assistant", completion_text="机智打断"),
        )
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-retry", "A", "发送重试", "1"),
        )
        triggering_event = FakeEvent(
            "intelligent-retry",
            "B",
            "发送重试",
            "2",
            fail_send=True,
        )

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await plugin.on_group_message(triggering_event)

        fingerprint = make_fingerprint("发送重试")
        state = plugin.state_service.group_states["intelligent-retry"]
        self.assertFalse(triggering_event.sent)
        self.assertNotIn(fingerprint, state.repeated_fingerprints)
        self.assertNotIn(fingerprint, state.pending_fingerprints)
        self.assertEqual(state.last_message_id, "1")
        self.assertEqual(
            store["group_states"]["intelligent-retry"]["last_message_id"],
            "1",
        )

        triggering_event.fail_send = False
        await plugin.on_group_message(triggering_event)

        self.assertEqual(triggering_event.sent, ["机智打断"])
        self.assertEqual(len(context.llm_calls), 2)
        self.assertIn(fingerprint, state.repeated_fingerprints)

    async def test_intelligent_interrupt_cancellation_rolls_back_before_send(
        self,
    ) -> None:
        context = FakeContext(llm_error=asyncio.CancelledError())
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-cancel", "A", "取消生成", "1"),
        )
        triggering_event = FakeEvent("intelligent-cancel", "B", "取消生成", "2")

        with self.assertRaises(asyncio.CancelledError):
            await plugin.on_group_message(triggering_event)

        fingerprint = make_fingerprint("取消生成")
        state = plugin.state_service.group_states["intelligent-cancel"]
        self.assertFalse(triggering_event.sent)
        self.assertNotIn(fingerprint, state.pending_fingerprints)
        self.assertNotIn(fingerprint, state.repeated_fingerprints)
        self.assertEqual(state.last_message_id, "1")
        self.assertNotIn(
            fingerprint,
            store["group_states"]["intelligent-cancel"]["pending_fingerprints"],
        )

        context.llm_error = None
        context.response = LLMResponse("assistant", completion_text="恢复生成")
        await plugin.on_group_message(triggering_event)

        self.assertEqual(triggering_event.sent, ["恢复生成"])
        self.assertIn(fingerprint, state.repeated_fingerprints)

    async def test_intelligent_interrupt_cancellation_keeps_pending_when_rollback_fails(
        self,
    ) -> None:
        context = FakeContext(llm_error=asyncio.CancelledError())
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
            },
            context=context,
        )
        context.before_error = lambda: setattr(plugin, "fail_next_put", True)
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-cancel-rollback", "A", "取消回滚", "1"),
        )
        triggering_event = FakeEvent(
            "intelligent-cancel-rollback",
            "B",
            "取消回滚",
            "2",
        )

        with patch("main.logger.exception") as exception_logger:
            with self.assertRaises(asyncio.CancelledError):
                await plugin.on_group_message(triggering_event)

        fingerprint = make_fingerprint("取消回滚")
        state = plugin.state_service.group_states["intelligent-cancel-rollback"]
        self.assertFalse(triggering_event.sent)
        self.assertIn(fingerprint, state.pending_fingerprints)
        self.assertIn(
            fingerprint,
            store["group_states"]["intelligent-cancel-rollback"][
                "pending_fingerprints"
            ],
        )
        self.assertEqual(state.last_message_id, "2")
        self.assertEqual(
            store["group_states"]["intelligent-cancel-rollback"]["last_message_id"],
            "2",
        )
        exception_logger.assert_called_once()
        self.assertIn("回滚保存失败", exception_logger.call_args.args[0])

        suppressed_event = FakeEvent(
            "intelligent-cancel-rollback",
            "C",
            "取消回滚",
            "3",
        )
        await plugin.on_group_message(suppressed_event)
        self.assertFalse(suppressed_event.sent)

    async def test_intelligent_interrupt_send_cancellation_keeps_pending(self) -> None:
        context = FakeContext(
            response=LLMResponse("assistant", completion_text="等待发送"),
        )
        store: dict = {}
        plugin = MemoryRepeater(
            store,
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "interrupt_texts": ["随机后备"],
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
            },
            context=context,
        )
        await plugin.initialize()
        await plugin.on_group_message(
            FakeEvent("intelligent-send-cancel", "A", "发送取消", "1"),
        )
        triggering_event = DelayedEvent(
            "intelligent-send-cancel",
            "B",
            "发送取消",
            "2",
        )
        handler_task = asyncio.create_task(plugin.on_group_message(triggering_event))
        await triggering_event.send_started.wait()

        handler_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await handler_task

        fingerprint = make_fingerprint("发送取消")
        state = plugin.state_service.group_states["intelligent-send-cancel"]
        self.assertFalse(triggering_event.sent)
        self.assertFalse(triggering_event.stopped)
        self.assertIn(fingerprint, state.pending_fingerprints)
        self.assertIn(
            fingerprint,
            store["group_states"]["intelligent-send-cancel"]["pending_fingerprints"],
        )
        self.assertEqual(state.last_message_id, "2")

        suppressed_event = FakeEvent(
            "intelligent-send-cancel",
            "C",
            "发送取消",
            "3",
        )
        await plugin.on_group_message(suppressed_event)
        self.assertFalse(suppressed_event.sent)
        self.assertEqual(len(context.llm_calls), 1)

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
            patch(
                "repeater_service.random.random", side_effect=[0.9, 0.0]
            ) as random_mock,
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
        self.assertFalse(
            plugin.state_service.is_repeat_enabled(
                "group-a", plugin.state_service.state_for("group-a")
            )
        )
        self.assertTrue(
            plugin.state_service.is_repeat_enabled(
                "group-b", plugin.state_service.state_for("group-b")
            )
        )

        plugin.state_service.settings.default_enabled = False
        self.assertFalse(
            plugin.state_service.is_repeat_enabled(
                "group-b", plugin.state_service.state_for("group-b")
            )
        )

        open_reply = await run_command(
            plugin,
            FakeEvent("group-a", "admin", "/repeatMsg 开启", "2", wake=True),
            "开启",
        )
        self.assertEqual(open_reply, ["已在本群开启自动复读。"])
        self.assertTrue(
            plugin.state_service.is_repeat_enabled(
                "group-a", plugin.state_service.state_for("group-a")
            )
        )

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
        self.assertTrue(
            plugin.state_service.is_interrupt_enabled("interrupt-command", state)
        )
        self.assertTrue(
            plugin.state_service.is_repeat_enabled("interrupt-command", state)
        )
        self.assertTrue(
            store["group_states"]["interrupt-command"]["interrupt_enabled_override"]
        )

        reloaded = MemoryRepeater(store)
        await reloaded.initialize()
        reloaded_state = reloaded.state_service.group_states["interrupt-command"]
        self.assertTrue(
            reloaded.state_service.is_interrupt_enabled(
                "interrupt-command",
                reloaded_state,
            )
        )
        close_reply = await run_interrupt_command(reloaded, event, "关闭")
        self.assertEqual(close_reply, ["已在本群关闭打断复读。"])
        self.assertFalse(
            reloaded.state_service.is_interrupt_enabled(
                "interrupt-command",
                reloaded_state,
            )
        )

        help_reply = await run_interrupt_command(reloaded, event, "帮助")
        self.assertIn("打断复读 查看", help_reply[0])

    async def test_interrupt_status_includes_mute_configuration(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "interrupt_default_enabled": True,
                "interrupt_mute_enabled": True,
                "interrupt_mute_duration_min": 10,
                "interrupt_mute_duration_max": 20,
                "interrupt_mute_probability": 0.25,
                "interrupt_mute_texts": ["甲", "乙"],
            },
        )
        await plugin.initialize()

        reply = await run_interrupt_command(
            plugin,
            FakeEvent("status", "member", "", "1"),
            "查看",
        )
        self.assertIn("打断复读禁言：开启", reply[0])
        self.assertIn("禁言概率：25%", reply[0])
        self.assertIn("禁言时长：10-20秒", reply[0])
        self.assertIn("提示文本：2 条", reply[0])

        disabled_plugin = MemoryRepeater(
            {},
            {
                "interrupt_mute_enabled": True,
                "interrupt_mute_disabled_group_ids": ["disabled-status"],
            },
        )
        await disabled_plugin.initialize()
        disabled_reply = await run_interrupt_command(
            disabled_plugin,
            FakeEvent("disabled-status", "member", "", "1"),
            "查看",
        )
        self.assertTrue(disabled_reply[0].endswith("打断复读禁言：关闭"))
        parent_disabled_plugin = MemoryRepeater(
            {},
            {
                "interrupt_default_enabled": False,
                "interrupt_mute_enabled": True,
            },
        )
        await parent_disabled_plugin.initialize()
        parent_disabled_reply = await run_interrupt_command(
            parent_disabled_plugin,
            FakeEvent("parent-disabled-status", "member", "", "1"),
            "查看",
        )
        self.assertIn("本群打断复读：关闭", parent_disabled_reply[0])
        self.assertTrue(parent_disabled_reply[0].endswith("打断复读禁言：关闭"))

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

    async def test_bot_mute_permission_accepts_owner_and_admin(self) -> None:
        owner = FakeEvent("permissions", "member", "", "1", group_owner="bot")
        group_admin = FakeEvent(
            "permissions",
            "member",
            "",
            "2",
            group_admins=["bot"],
        )
        member = FakeEvent("permissions", "member", "", "3")

        self.assertTrue(await RepeaterPlugin._is_bot_admin(owner))
        self.assertTrue(await RepeaterPlugin._is_bot_admin(group_admin))
        self.assertFalse(await RepeaterPlugin._is_bot_admin(member))

    async def test_configured_disabled_group_ids_apply_directly(self) -> None:
        config = MemoryConfig(
            {
                "repeat_disabled_group_ids": ["configured"],
                "interrupt_disabled_group_ids": ["configured"],
                "interrupt_mute_enabled": True,
                "interrupt_mute_disabled_group_ids": ["configured"],
            }
        )
        plugin = MemoryRepeater({}, config)
        await plugin.initialize()

        self.assertFalse(plugin.state_service.is_repeat_enabled("configured", None))
        self.assertFalse(plugin.state_service.is_interrupt_enabled("configured", None))
        self.assertFalse(plugin.state_service.is_interrupt_mute_enabled("configured"))
        self.assertEqual(config.save_count, 0)
        self.assertEqual(config["repeat_disabled_group_ids"], ["configured"])
        self.assertEqual(config["interrupt_disabled_group_ids"], ["configured"])
        self.assertEqual(config["interrupt_mute_disabled_group_ids"], ["configured"])

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

        self.assertFalse(
            await plugin.state_service.is_any_repeat_mode_enabled("disabled"),
        )
        self.assertNotIn("disabled", plugin.state_service.group_states)
        self.assertNotIn("disabled", plugin.state_service.group_locks)

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

    async def test_locked_read_only_group_query_reuses_existing_lock_without_state_allocation(
        self,
    ) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "default_enabled": False,
                "interrupt_default_enabled": False,
            },
        )
        await plugin.initialize()

        service = plugin.state_service
        group_key = "locked-read-only"
        lock = service.lock_for(group_key)
        self.assertIs(service.group_locks[group_key], lock)

        query_task = None
        try:
            async with lock:
                query_task = asyncio.create_task(
                    service.is_any_repeat_mode_enabled(group_key)
                )
                await asyncio.sleep(0)
                self.assertIsNotNone(query_task)
                self.assertFalse(query_task.done())
                self.assertNotIn(group_key, service.group_states)
                self.assertIs(service.group_locks[group_key], lock)

            self.assertIsNotNone(query_task)
            self.assertFalse(await query_task)
            self.assertNotIn(group_key, service.group_states)
            self.assertIs(service.group_locks[group_key], lock)
        finally:
            if query_task is not None and not query_task.done():
                query_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await query_task

    async def test_mode_query_honors_defaults_and_existing_overrides(self) -> None:
        plugin = MemoryRepeater(
            {},
            {
                "default_enabled": False,
                "interrupt_default_enabled": True,
            },
        )
        await plugin.initialize()

        self.assertTrue(
            await plugin.state_service.is_any_repeat_mode_enabled("defaults"),
        )
        self.assertNotIn("defaults", plugin.state_service.group_states)
        self.assertNotIn("defaults", plugin.state_service.group_locks)

        group_key = "override"
        plugin.state_service.settings.interrupt_disabled_group_ids.add(group_key)
        plugin.state_service.group_states[group_key] = GroupRepeaterState(
            enabled_override=True,
        )

        self.assertTrue(
            await plugin.state_service.is_any_repeat_mode_enabled(group_key),
        )

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


class IntelligentHistoryStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_windows_filter_pagination_and_privacy_contract(self) -> None:
        local_timezone = timezone(timedelta(hours=8))
        now = datetime(2026, 8, 1, 0, 30, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        local_day_start_ms = int(
            now.astimezone(local_timezone)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
            .timestamp()
            * 1000
        )
        with tempfile.TemporaryDirectory() as directory:
            store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
                clock=lambda: now,
                local_timezone=local_timezone,
            )
            await store.initialize()

            def record(
                occurred_at_ms: int,
                *,
                kind: str,
                source: str = "runtime",
                outcome: str = "success",
            ) -> IntelligentActionRecord:
                return IntelligentActionRecord(
                    occurred_at_ms=occurred_at_ms,
                    kind=kind,
                    source=source,
                    outcome=outcome,
                    provider_id="provider-a",
                    model="model-a",
                    group_id="group-a",
                    mute_duration_seconds=60 if kind == "mute" else None,
                    latency_ms=7,
                    failure_code=("request_failed" if outcome == "failed" else None),
                )

            cutoff = now_ms - RETENTION_MS
            await store.append(record(cutoff - 1, kind="mute", outcome="failed"))
            await store.append(record(cutoff, kind="repeat"))
            await store.append(record(local_day_start_ms - 1, kind="mute"))
            await store.append(record(local_day_start_ms, kind="repeat"))
            await store.append(record(now_ms - 1_000, kind="repeat"))

            self.assertEqual(await store.purge_expired(), 1)
            today_first_page = await store.query(
                window="day",
                kind="repeat",
                page=1,
                page_size=1,
            )
            self.assertEqual(today_first_page.start_at_ms, local_day_start_ms)
            self.assertEqual(today_first_page.summary["total"], 2)
            self.assertEqual(today_first_page.total_pages, 2)
            self.assertEqual(
                today_first_page.records[0].occurred_at_ms,
                now_ms - 1_000,
            )
            today_second_page = await store.query(
                window="day",
                kind="repeat",
                page=2,
                page_size=1,
            )
            self.assertEqual(
                today_second_page.records[0].occurred_at_ms,
                local_day_start_ms,
            )
            seven_days = await store.query(window="7d", page=1, page_size=50)
            retained_times = {item.occurred_at_ms for item in seven_days.records}
            self.assertIn(cutoff, retained_times)
            self.assertNotIn(cutoff - 1, retained_times)
            self.assertNotIn("text", seven_days.records[0].to_dict())
            self.assertNotIn("completion", seven_days.records[0].to_dict())
            self.assertNotIn("sender_id", seven_days.records[0].to_dict())

    async def test_initialization_purges_only_strictly_expired_records(self) -> None:
        now = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligent_history.sqlite3"
            initial_store = IntelligentHistoryStore(path, clock=lambda: now)
            await initial_store.initialize()
            cutoff = now_ms - RETENTION_MS
            for occurred_at_ms in (cutoff - 1, cutoff):
                await initial_store.append(
                    IntelligentActionRecord(
                        occurred_at_ms=occurred_at_ms,
                        kind="repeat",
                        source="runtime",
                        outcome="success",
                        provider_id="provider-a",
                        model="model-a",
                        group_id="group-a",
                        mute_duration_seconds=None,
                        latency_ms=1,
                    ),
                )
            reloaded_store = IntelligentHistoryStore(path, clock=lambda: now)
            self.assertEqual(await reloaded_store.initialize(), 1)
            records = await reloaded_store.query(window="7d", page_size=50)
            self.assertEqual([item.occurred_at_ms for item in records.records], [cutoff])

    async def test_cancelled_append_waits_for_its_sqlite_worker(self) -> None:
        now = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
                clock=lambda: now,
            )
            await store.initialize()
            record = IntelligentActionRecord(
                occurred_at_ms=int(now.timestamp() * 1000),
                kind="repeat",
                source="runtime",
                outcome="success",
                provider_id="provider-a",
                model="model-a",
                group_id="group-a",
                mute_duration_seconds=None,
                latency_ms=1,
            )
            started = threading.Event()
            release = threading.Event()
            original_append = store._append_sync

            def delayed_append(item: IntelligentActionRecord) -> int:
                started.set()
                if not release.wait(1):
                    raise RuntimeError("append worker was not released")
                return original_append(item)

            store._append_sync = delayed_append
            append_task = asyncio.create_task(store.append(record))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                append_task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(append_task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await append_task
            history = await store.query(window="day", page_size=50)
            self.assertEqual(history.summary["total"], 1)

    async def test_range_display_uses_server_zone_across_dst(self) -> None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            new_york = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            self.skipTest("America/New_York timezone data is unavailable")
        now = datetime(2026, 3, 8, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
                clock=lambda: now,
                local_timezone=new_york,
            )
            await store.initialize()
            history = await store.query(window="day", page_size=50)

        self.assertEqual(history.timezone_name, "EDT")
        self.assertEqual(history.start_display, "2026-03-08T00:00:00-05:00")
        self.assertEqual(history.end_display, "2026-03-08T08:00:00-04:00")


class IntelligentConsoleApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_console_routes_models_and_config_save(self) -> None:
        models = [f"model-{index:03d}" for index in range(501)]
        context = FakePageContext(
            providers=[
                FakeChatProvider("provider-a", models),
                FakeChatProvider("provider-b", ["model-b"]),
            ],
        )
        config = AsyncMemoryConfig(
            {
                "intelligent_interrupt_provider_id": "provider-a",
                "intelligent_interrupt_model": "zz-custom",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            plugin = MemoryRepeater({}, config, context=context)
            plugin.history_store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
            )
            await plugin.initialize()
            cleanup_task = plugin._history_cleanup_task
            try:
                registered = {
                    (route, tuple(methods))
                    for route, _handler, methods, _desc in context.registered_web_apis
                }
                prefix = "/astrbot_plugin_repeater/intelligent-console"
                self.assertEqual(
                    registered,
                    {
                        (f"{prefix}/config", ("GET",)),
                        (f"{prefix}/models", ("GET",)),
                        (f"{prefix}/config", ("POST",)),
                        (f"{prefix}/test/repeat", ("POST",)),
                        (f"{prefix}/test/mute", ("POST",)),
                        (f"{prefix}/history", ("GET",)),
                    },
                )
                config_payload = response_payload(
                    await plugin._web_get_intelligent_console_config(),
                )
                self.assertEqual(config_payload["status"], "ok")
                self.assertTrue(config_payload["data"]["provider_exists"])
                self.assertEqual(
                    [item["id"] for item in config_payload["data"]["providers"]],
                    ["provider-a", "provider-b"],
                )
                with patch(
                    "main.request",
                    FakePageRequest(query={"provider_id": "provider-a"}),
                ):
                    models_payload = response_payload(
                        await plugin._web_get_intelligent_console_models(),
                    )
                candidates = models_payload["data"]["models"]
                self.assertEqual(len(candidates), 500)
                self.assertIn("zz-custom", candidates)
                with patch(
                    "main.request",
                    FakePageRequest(
                        body={
                            "provider_id": "provider-b",
                            "model": " custom-model ",
                        },
                    ),
                ):
                    save_payload = response_payload(
                        await plugin._web_save_intelligent_console_config(),
                    )
                self.assertEqual(save_payload["status"], "ok")
                self.assertEqual(
                    save_payload["data"],
                    {"provider_id": "provider-b", "model": "custom-model"},
                )
                self.assertEqual(
                    config["intelligent_interrupt_provider_id"],
                    "provider-b",
                )
                self.assertEqual(config["intelligent_interrupt_model"], "custom-model")
                self.assertEqual(
                    plugin.state_service.settings.intelligent_interrupt_provider_id,
                    "provider-b",
                )
            finally:
                await plugin.terminate()
            self.assertIsNotNone(cleanup_task)
            self.assertTrue(cleanup_task.cancelled())

    async def test_config_snapshot_conflict_does_not_swap_runtime_settings(self) -> None:
        config = AsyncMemoryConfig(
            {"intelligent_interrupt_provider_id": "provider-a"},
            committed=False,
        )
        plugin = MemoryRepeater({}, config, context=FakePageContext())
        await plugin.initialize()
        try:
            previous_settings = plugin.state_service.settings
            committed = await plugin._save_intelligent_console_config(
                provider_id="provider-b",
                model="model-b",
            )
            self.assertFalse(committed)
            self.assertIs(plugin.state_service.settings, previous_settings)
            self.assertEqual(
                plugin.state_service.settings.intelligent_interrupt_provider_id,
                "provider-a",
            )
        finally:
            await plugin.terminate()

    async def test_private_config_commit_uses_the_committed_snapshot(self) -> None:
        config = SnapshotMemoryConfig(
            {
                "intelligent_interrupt_provider_id": "old-provider",
                "intelligent_interrupt_model": "old-model",
            },
            mutate_after_write=True,
        )
        plugin = MemoryRepeater({}, config, context=FakePageContext())
        await plugin.initialize()
        try:
            committed = await plugin._save_intelligent_console_config(
                provider_id="saved-provider",
                model="saved-model",
            )

            self.assertTrue(committed)
            self.assertEqual(
                config.written_snapshots[-1]["intelligent_interrupt_provider_id"],
                "saved-provider",
            )
            self.assertEqual(
                config["intelligent_interrupt_provider_id"],
                "later-provider",
            )
            self.assertEqual(
                plugin.state_service.settings.intelligent_interrupt_provider_id,
                "saved-provider",
            )
            self.assertEqual(
                plugin.state_service.settings.intelligent_interrupt_model,
                "saved-model",
            )
        finally:
            await plugin.terminate()

    async def test_private_config_commit_keeps_live_config_for_group_toggle(self) -> None:
        config = SnapshotMemoryConfig(
            {
                "intelligent_interrupt_provider_id": "old-provider",
                "intelligent_interrupt_model": "old-model",
            },
        )
        plugin = MemoryRepeater({}, config, context=FakePageContext())
        await plugin.initialize()
        try:
            committed = await plugin._save_intelligent_console_config(
                provider_id="saved-provider",
                model="saved-model",
            )

            self.assertTrue(committed)
            self.assertIs(plugin.state_service.settings.config, config)
            save_count_before_toggle = config.save_count
            await plugin.state_service.set_repeat_enabled("persisted-group", False)

            self.assertEqual(config["repeat_disabled_group_ids"], ["persisted-group"])
            self.assertEqual(config.save_count, save_count_before_toggle + 1)
        finally:
            await plugin.terminate()

    async def test_private_config_conflict_restores_previously_committed_values(
        self,
    ) -> None:
        config = SnapshotMemoryConfig(
            {
                "intelligent_interrupt_provider_id": "old-provider",
                "intelligent_interrupt_model": "old-model",
            },
            committed=False,
            mutate_after_write=True,
        )
        plugin = MemoryRepeater({}, config, context=FakePageContext())
        await plugin.initialize()
        try:
            committed = await plugin._save_intelligent_console_config(
                provider_id="saved-provider",
                model="saved-model",
            )

            self.assertFalse(committed)
            self.assertEqual(
                config["intelligent_interrupt_provider_id"],
                "old-provider",
            )
            self.assertEqual(config["intelligent_interrupt_model"], "old-model")
            self.assertEqual(
                plugin.state_service.settings.intelligent_interrupt_provider_id,
                "old-provider",
            )
            self.assertEqual(
                plugin.state_service.settings.intelligent_interrupt_model,
                "old-model",
            )
        finally:
            await plugin.terminate()

    async def test_cancelled_config_save_waits_for_commit_before_propagating(
        self,
    ) -> None:
        config = BlockingAsyncMemoryConfig(
            {
                "intelligent_interrupt_provider_id": "old-provider",
                "intelligent_interrupt_model": "old-model",
            },
        )
        plugin = MemoryRepeater({}, config, context=FakePageContext())
        await plugin.initialize()
        save_task = asyncio.create_task(
            plugin._save_intelligent_console_config(
                provider_id="saved-provider",
                model="saved-model",
            ),
        )
        try:
            await config.save_started.wait()
            save_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(save_task.done())
            config.release_save.set()
            with self.assertRaises(asyncio.CancelledError):
                await save_task

            self.assertEqual(
                config["intelligent_interrupt_provider_id"],
                "saved-provider",
            )
            self.assertEqual(config["intelligent_interrupt_model"], "saved-model")
            self.assertEqual(
                plugin.state_service.settings.intelligent_interrupt_provider_id,
                "saved-provider",
            )
            self.assertEqual(
                plugin.state_service.settings.intelligent_interrupt_model,
                "saved-model",
            )
        finally:
            config.release_save.set()
            if not save_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await save_task
            await plugin.terminate()

    async def test_page_tests_are_non_destructive_and_immediately_recorded(self) -> None:
        context = FakePageContext(
            response=LLMResponse("assistant", completion_text="测试生成文案"),
        )
        with tempfile.TemporaryDirectory() as directory:
            plugin = MemoryRepeater(
                {},
                {
                    "intelligent_interrupt_provider_id": "provider-a",
                    "intelligent_interrupt_model": "model-a",
                },
                context=context,
            )
            store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
                clock=lambda: datetime.now(timezone.utc),
            )
            plugin.history_store = store
            await plugin.initialize()
            try:
                repeat_payload = response_payload(
                    await plugin._web_test_intelligent_repeat(),
                )
                mute_payload = response_payload(await plugin._web_test_intelligent_mute())
                self.assertEqual(repeat_payload["status"], "ok")
                self.assertEqual(mute_payload["status"], "ok")
                self.assertEqual(context.provider_calls, [])
                self.assertEqual(len(context.llm_calls), 2)
                self.assertEqual(
                    context.llm_calls[0]["prompt"],
                    "被复读的内容：这是智能打断测试使用的固定示例消息。",
                )
                self.assertEqual(
                    context.llm_calls[1]["prompt"],
                    "被禁言用户：测试用户\n禁言时长：60秒",
                )
                self.assertEqual(plugin.state_service.group_states, {})
                history = await store.query(window="day", page_size=50)
                self.assertEqual(history.summary["total"], 2)
                self.assertEqual(history.summary["success"], 2)
                self.assertEqual(
                    {record.source for record in history.records},
                    {"manual_test"},
                )
                self.assertEqual({record.kind for record in history.records}, {"repeat", "mute"})
            finally:
                await plugin.terminate()

    async def test_blank_provider_page_test_never_resolves_a_session(self) -> None:
        context = FakePageContext()
        with tempfile.TemporaryDirectory() as directory:
            plugin = MemoryRepeater(
                {},
                {"intelligent_interrupt_model": "model-a"},
                context=context,
            )
            store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
                clock=lambda: datetime.now(timezone.utc),
            )
            plugin.history_store = store
            await plugin.initialize()
            try:
                response = await plugin._web_test_intelligent_repeat()
                payload = response_payload(response)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(payload["data"]["code"], "provider_resolution_failed")
                self.assertEqual(context.provider_calls, [])
                self.assertEqual(context.llm_calls, [])
                self.assertEqual(plugin.state_service.group_states, {})
                history = await store.query(window="day", page_size=50)
                self.assertEqual(history.summary["failed"], 1)
                self.assertEqual(history.records[0].source, "manual_test")
            finally:
                await plugin.terminate()

    async def test_history_page_rejects_excessive_page_without_latching_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin = MemoryRepeater({}, context=FakePageContext())
            plugin.history_store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
            )
            await plugin.initialize()
            try:
                with patch(
                    "main.request",
                    FakePageRequest(query={"page": "10001"}),
                ):
                    response = await plugin._web_get_intelligent_history()

                self.assertEqual(response.status_code, 400)
                self.assertIsNone(plugin._history_storage_error)
            finally:
                await plugin.terminate()

    async def test_history_page_recovers_after_transient_query_failure(self) -> None:
        class FailingOnceStore:
            def __init__(self) -> None:
                self.query_attempts = 0

            async def query(self, **_kwargs):
                self.query_attempts += 1
                if self.query_attempts == 1:
                    raise RuntimeError("temporary history read failure")
                return SimpleNamespace(
                    to_dict=lambda: {
                        "records": [],
                        "summary": {},
                        "range": {},
                        "pagination": {},
                    },
                )

        plugin = MemoryRepeater({}, context=FakePageContext())
        await plugin.initialize()
        store = FailingOnceStore()
        plugin.history_store = store
        try:
            with patch("main.request", FakePageRequest()):
                failed = await plugin._web_get_intelligent_history()
            with patch("main.request", FakePageRequest()):
                recovered = await plugin._web_get_intelligent_history()

            self.assertEqual(failed.status_code, 503)
            self.assertTrue(plugin._history_available)
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(response_payload(recovered)["status"], "ok")
            self.assertEqual(store.query_attempts, 2)
        finally:
            await plugin.terminate()

    async def test_console_request_drains_before_termination(self) -> None:
        context = BlockingPageContext(
            response=LLMResponse("assistant", completion_text="测试文案"),
        )
        plugin = MemoryRepeater(
            {},
            {
                "intelligent_interrupt_provider_id": "provider-a",
                "intelligent_interrupt_model": "model-a",
            },
            context=context,
        )
        await plugin.initialize()
        handlers = {
            route: handler
            for route, handler, _methods, _description in context.registered_web_apis
        }
        request_task = asyncio.create_task(
            handlers["/astrbot_plugin_repeater/intelligent-console/test/repeat"](),
        )
        termination_task: asyncio.Task[None] | None = None
        try:
            await context.generation_started.wait()
            termination_task = asyncio.create_task(plugin.terminate())
            await asyncio.sleep(0)
            self.assertFalse(termination_task.done())

            context.release_generation.set()
            response = await request_task
            self.assertEqual(response.status_code, 200)
            await termination_task

            stopped_response = await handlers[
                "/astrbot_plugin_repeater/intelligent-console/test/repeat"
            ]()
            self.assertEqual(stopped_response.status_code, 503)
        finally:
            context.release_generation.set()
            if not request_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await request_task
            if termination_task is None:
                await plugin.terminate()
            elif not termination_task.done():
                await termination_task

    async def test_runtime_history_keeps_success_fallback_and_no_cancellation_record(
        self,
    ) -> None:
        context = FakeContext(
            response=LLMResponse("assistant", completion_text="智能打断"),
        )
        with tempfile.TemporaryDirectory() as directory:
            plugin = MemoryRepeater(
                {},
                {
                    "repeat_threshold": 2,
                    "interrupt_default_enabled": True,
                    "interrupt_probability": 1.0,
                    "interrupt_texts": ["静态后备"],
                    "intelligent_interrupt_enabled": True,
                    "intelligent_interrupt_provider_id": "provider-a",
                    "intelligent_interrupt_model": "model-a",
                },
                context=context,
            )
            store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
                clock=lambda: datetime.now(timezone.utc),
            )
            plugin.history_store = store
            await plugin.initialize()

            async def trigger(group_id: str, text: str) -> None:
                await plugin.on_group_message(FakeEvent(group_id, "A", text, "1"))
                await plugin.on_group_message(FakeEvent(group_id, "B", text, "2"))

            try:
                with patch("repeater_service.random.random", return_value=0.0):
                    await trigger("history-success", "成功记录")
                    context.llm_error = RuntimeError("LLM unavailable")
                    await trigger("history-fallback", "后备记录")
                if plugin._history_write_tasks:
                    await asyncio.gather(*tuple(plugin._history_write_tasks))
                history = await store.query(window="day", page_size=50)
                self.assertEqual(history.summary["success"], 1)
                self.assertEqual(history.summary["fallback"], 1)
                self.assertEqual({record.source for record in history.records}, {"runtime"})
                record_count = history.summary["total"]
                context.llm_error = asyncio.CancelledError()
                with patch("repeater_service.random.random", return_value=0.0):
                    await plugin.on_group_message(
                        FakeEvent("history-cancel", "A", "取消记录", "1"),
                    )
                    with self.assertRaises(asyncio.CancelledError):
                        await plugin.on_group_message(
                            FakeEvent("history-cancel", "B", "取消记录", "2"),
                        )
                await asyncio.sleep(0)
                after_cancellation = await store.query(window="day", page_size=50)
                self.assertEqual(after_cancellation.summary["total"], record_count)
            finally:
                await plugin.terminate()

    async def test_termination_drains_runtime_history_scheduled_by_handler(
        self,
    ) -> None:
        context = BlockingPageContext(
            response=LLMResponse("assistant", completion_text="智能打断"),
        )
        with tempfile.TemporaryDirectory() as directory:
            plugin = MemoryRepeater(
                {},
                {
                    "repeat_threshold": 2,
                    "interrupt_default_enabled": True,
                    "interrupt_probability": 1.0,
                    "intelligent_interrupt_enabled": True,
                    "intelligent_interrupt_provider_id": "provider-a",
                    "intelligent_interrupt_model": "model-a",
                },
                context=context,
            )
            store = IntelligentHistoryStore(
                Path(directory) / "intelligent_history.sqlite3",
            )
            plugin.history_store = store
            await plugin.initialize()
            runtime_task: asyncio.Task[None] | None = None
            termination_task: asyncio.Task[None] | None = None
            try:
                with patch("repeater_service.random.random", return_value=0.0):
                    await plugin.on_group_message(
                        FakeEvent("drain-history", "A", "内容", "1"),
                    )
                    runtime_task = asyncio.create_task(
                        plugin.on_group_message(
                            FakeEvent("drain-history", "B", "内容", "2"),
                        ),
                    )
                    await context.generation_started.wait()
                    termination_task = asyncio.create_task(plugin.terminate())
                    await asyncio.sleep(0)
                    self.assertFalse(termination_task.done())
                    context.release_generation.set()
                    await runtime_task
                    await termination_task

                history = await store.query(window="day", page_size=50)
                self.assertEqual(history.summary["total"], 1)
                self.assertEqual(history.summary["success"], 1)
                self.assertEqual(history.records[0].source, "runtime")
            finally:
                context.release_generation.set()
                if runtime_task is not None and not runtime_task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await runtime_task
                if termination_task is None:
                    await plugin.terminate()
                elif not termination_task.done():
                    await termination_task

    async def test_runtime_generation_uses_one_settings_snapshot(self) -> None:
        class SnapshotSwitchingStateService:
            def __init__(self, delegate, old_settings, new_settings) -> None:
                self._delegate = delegate
                self._old_settings = old_settings
                self._new_settings = new_settings
                self.settings_reads = 0

            @property
            def settings(self):
                self.settings_reads += 1
                return (
                    self._old_settings
                    if self.settings_reads <= 2
                    else self._new_settings
                )

            def __getattr__(self, name):
                return getattr(self._delegate, name)

        context = FakeContext(
            response=LLMResponse("assistant", completion_text="快照文案"),
        )
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "old-provider",
                "intelligent_interrupt_model": "old-model",
                "intelligent_interrupt_prompt": "old prompt",
            },
            context=context,
        )
        old_settings = plugin.state_service.settings
        new_settings = copy.deepcopy(old_settings)
        new_settings.intelligent_interrupt_provider_id = "new-provider"
        new_settings.intelligent_interrupt_model = "new-model"
        new_settings.intelligent_interrupt_prompt = "new prompt"
        plugin.state_service = SnapshotSwitchingStateService(
            plugin.state_service,
            old_settings,
            new_settings,
        )
        await plugin.initialize()
        try:
            with patch("repeater_service.random.random", return_value=0.0):
                await plugin.on_group_message(
                    FakeEvent("snapshot", "A", "内容", "1"),
                )
                await plugin.on_group_message(
                    FakeEvent("snapshot", "B", "内容", "2"),
                )

            self.assertEqual(plugin.state_service.settings_reads, 1)
            call = context.llm_calls[0]
            self.assertEqual(call["chat_provider_id"], "old-provider")
            self.assertEqual(call["system_prompt"], "old prompt")
            self.assertEqual(call["kwargs"], {"model": "old-model"})
        finally:
            await plugin.terminate()

    async def test_history_write_failure_does_not_interrupt_runtime_delivery(self) -> None:
        context = FakeContext(
            response=LLMResponse("assistant", completion_text="智能打断"),
        )
        plugin = MemoryRepeater(
            {},
            {
                "repeat_threshold": 2,
                "interrupt_default_enabled": True,
                "interrupt_probability": 1.0,
                "intelligent_interrupt_enabled": True,
                "intelligent_interrupt_provider_id": "provider-a",
            },
            context=context,
        )
        plugin.history_store = FailingHistoryStore()
        await plugin.initialize()
        try:
            first = FakeEvent("history-write-error", "A", "隔离失败", "1")
            trigger = FakeEvent("history-write-error", "B", "隔离失败", "2")
            with patch("repeater_service.random.random", return_value=0.0):
                await plugin.on_group_message(first)
                await plugin.on_group_message(trigger)
            if plugin._history_write_tasks:
                await asyncio.gather(*tuple(plugin._history_write_tasks))
            self.assertEqual(trigger.sent, ["智能打断"])
            self.assertTrue(trigger.stopped)
            self.assertEqual(plugin._history_storage_error, "智能记录存储不可用。")
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
