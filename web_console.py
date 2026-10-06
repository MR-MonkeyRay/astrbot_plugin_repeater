"""复读机控制台的 Page Web API。

处理供应商设置、LLM 调用测试和调用记录查询；路由注册与停机排空由插件负责。
"""

import asyncio
import inspect
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

if __package__:
    from .llm_client import (
        IntelligentGenerationResult,
        build_interrupt_prompt,
        build_mute_prompt,
    )
    from .repeater_config import (
        CONFIG_SECTION_INTELLIGENT_PROVIDER,
        INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH,
        INTELLIGENT_INTERRUPT_MANUAL_API_KEY_MAX_LENGTH,
        INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
        INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE,
        build_settings,
        normalize_intelligent_interrupt_manual_api_base,
        normalize_intelligent_interrupt_manual_api_key,
        normalize_intelligent_interrupt_provider_mode,
        persist_config,
    )
else:
    from llm_client import (
        IntelligentGenerationResult,
        build_interrupt_prompt,
        build_mute_prompt,
    )
    from repeater_config import (
        CONFIG_SECTION_INTELLIGENT_PROVIDER,
        INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH,
        INTELLIGENT_INTERRUPT_MANUAL_API_KEY_MAX_LENGTH,
        INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
        INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE,
        build_settings,
        normalize_intelligent_interrupt_manual_api_base,
        normalize_intelligent_interrupt_manual_api_key,
        normalize_intelligent_interrupt_provider_mode,
        persist_config,
    )

if TYPE_CHECKING:
    from .main import RepeaterPlugin


MAX_CONFIG_TEXT_LENGTH = INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH
MAX_HISTORY_PAGE = 10_000
MAX_MODEL_CANDIDATES = 500
TEST_REPEAT_MESSAGE = "这是智能打断测试使用的固定示例消息。"


class IntelligentConsoleApi:
    """Page API handlers bound to one loaded plugin instance."""

    def __init__(self, plugin: "RepeaterPlugin") -> None:
        self._plugin = plugin

    async def _chat_provider_catalog(
        self,
    ) -> tuple[list[dict[str, str]], dict[str, Any], bool]:
        """Return configured chat providers without exposing provider secrets."""
        get_all_providers = getattr(
            getattr(self._plugin, "context", None),
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
                        current_model.strip() if isinstance(current_model, str) else ""
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
            raise ValueError(f"{field_name} 不能超过 {MAX_CONFIG_TEXT_LENGTH} 个字符")
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

    def shutdown_response(self):
        """Reject Page requests after the plugin has begun termination."""
        if not self._plugin.shutting_down:
            return None
        return error_response("插件正在停止，LLM调用测试暂不可用。", status_code=503)

    async def get_config(self):
        """Return saved settings, runtime switches, and available provider choices."""
        shutdown_response = self.shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        settings = self._plugin.state_service.settings
        provider_mode = settings.intelligent_interrupt_provider_mode
        provider_id = settings.intelligent_interrupt_provider_id
        load_provider_catalog = (
            provider_mode == INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT
            or request.query.get("include_provider_catalog") == "1"
        )
        if load_provider_catalog:
            (
                options,
                provider_by_id,
                catalog_available,
            ) = await self._chat_provider_catalog()
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
                        "available": self._plugin._history_available,
                        "message": self._plugin._history_storage_error,
                    },
                },
            },
        )

    async def get_models(self):
        """Return model candidates for one explicit configured chat provider."""
        shutdown_response = self.shutdown_response()
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
            logger.exception("[repeater] 读取LLM供应商模型列表失败")
            return error_response(
                "无法读取模型列表，仍可手动输入自定义模型 ID。",
                status_code=503,
            )
        if not isinstance(models, (list, tuple, set)):
            return error_response(
                "模型列表格式无效，仍可手动输入自定义模型 ID。",
                status_code=503,
            )
        model_ids = {
            model.strip()
            for model in models
            if isinstance(model, str)
            and model.strip()
            and len(model.strip()) <= MAX_CONFIG_TEXT_LENGTH
        }
        settings = self._plugin.state_service.settings
        saved_model = ""
        if (
            settings.intelligent_interrupt_provider_id == provider_id
            and settings.intelligent_interrupt_model
        ):
            saved_model = settings.intelligent_interrupt_model
        candidates = sorted(model_ids, key=str.casefold)[:MAX_MODEL_CANDIDATES]
        if saved_model and saved_model not in candidates:
            if len(candidates) == MAX_MODEL_CANDIDATES:
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

    async def save_provider_settings(
        self,
        *,
        provider_id: str,
        model: str,
        provider_mode: str = INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
        manual_api_base: str = "",
        manual_api_key: str | None = None,
    ) -> None:
        """Persist shared provider settings and swap the runtime settings.

        The section is written through ``AstrBotConfig.save_config_async``. A
        failed write restores the previous section; a cancelled request still
        waits for the started write and applies the saved settings first.
        """
        state_service = self._plugin.state_service
        config = self._plugin.config
        async with state_service.save_lock:
            settings = state_service.settings
            previous_section = deepcopy(
                config.get(CONFIG_SECTION_INTELLIGENT_PROVIDER),
            )
            section = dict(previous_section or {})
            section.update(
                {
                    "mode": provider_mode,
                    "provider_id": provider_id,
                    "manual_api_base": manual_api_base,
                    "manual_api_key": (
                        settings.intelligent_interrupt_manual_api_key
                        if manual_api_key is None
                        else manual_api_key
                    ),
                    "model": model,
                },
            )
            try:
                await persist_config(
                    config,
                    {CONFIG_SECTION_INTELLIGENT_PROVIDER: section},
                )
            except asyncio.CancelledError:
                state_service.settings = build_settings(config, logger)
                raise
            except Exception:
                if previous_section is None:
                    config.pop(CONFIG_SECTION_INTELLIGENT_PROVIDER, None)
                else:
                    config[CONFIG_SECTION_INTELLIGENT_PROVIDER] = previous_section
                raise
            state_service.settings = build_settings(config, logger)

    async def save_config(self):
        """Validate and persist the Page's shared provider settings."""
        shutdown_response = self.shutdown_response()
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
                provider_mode == INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE
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
        if provider_mode == INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT and provider_id:
            _, provider_by_id, catalog_available = await self._chat_provider_catalog()
            if not catalog_available:
                return error_response("聊天供应商列表暂不可用。", status_code=503)
            if provider_id not in provider_by_id:
                return error_response("指定的聊天供应商不存在。", status_code=404)
        try:
            await self.save_provider_settings(
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
                f"[repeater] 保存LLM供应商配置失败（{type(exc).__name__}）",
            )
            return error_response("保存LLM供应商配置失败。", status_code=500)
        saved_settings = self._plugin.state_service.settings
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
        message_text: str | None = None,
        prompt: str | None = None,
    ) -> None:
        """Synchronously retain a Page test so an immediate history refresh sees it."""
        outcome: Literal["success", "fallback", "failed"] = (
            "success" if result.result_code == "success" else "failed"
        )
        await self._plugin._append_history_record(
            self._plugin._history_record_for_generation(
                result,
                kind=kind,
                source="manual_test",
                outcome=outcome,
                message_text=message_text,
                prompt=prompt,
            ),
        )

    async def _run_test(
        self,
        *,
        kind: Literal["repeat", "mute"],
    ):
        """Run a fixed generation test without group side effects."""
        shutdown_response = self.shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        settings = self._plugin.state_service.settings
        provider_mode = settings.intelligent_interrupt_provider_mode
        provider_id = settings.intelligent_interrupt_provider_id
        model = settings.intelligent_interrupt_model
        if kind == "repeat":
            message_text = TEST_REPEAT_MESSAGE
            prompt = build_interrupt_prompt(message_text)
            system_prompt = settings.intelligent_interrupt_prompt
            feature_name = "智能打断LLM调用测试"
        else:
            message_text = None
            prompt = build_mute_prompt("测试用户", 60)
            system_prompt = settings.intelligent_interrupt_mute_prompt
            feature_name = "智能禁言提示LLM调用测试"
        if provider_mode == INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT:
            if not provider_id:
                result = IntelligentGenerationResult(
                    completion=None,
                    provider_id=None,
                    model=model,
                    latency_ms=0,
                    result_code="provider_resolution_failed",
                )
                await self._record_manual_generation(
                    kind=kind,
                    result=result,
                    message_text=message_text,
                    prompt=prompt,
                )
                return error_response(
                    "留空供应商会跟随触发会话；LLM调用测试需要先保存一个明确的聊天供应商。",
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
                await self._record_manual_generation(
                    kind=kind,
                    result=result,
                    message_text=message_text,
                    prompt=prompt,
                )
                return error_response(
                    "保存的聊天供应商当前不可用，请重新选择后保存。",
                    status_code=409 if catalog_available else 503,
                    data={"code": result.result_code},
                )
        result = await self._plugin.llm_client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            settings=settings,
            unified_msg_origin=None,
            feature_name=feature_name,
        )
        await self._record_manual_generation(
            kind=kind,
            result=result,
            message_text=message_text,
            prompt=prompt,
        )
        if result.result_code != "success":
            if (
                provider_mode == INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE
                and result.result_code == "provider_resolution_failed"
            ):
                return error_response(
                    "OpenAI 兼容直连模式需要已保存的 API Base URL、API Key 和自定义模型 ID。",
                    status_code=409,
                    data={"code": result.result_code},
                )
            return error_response(
                "LLM调用失败，请检查供应商和模型配置。",
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

    async def test_repeat(self):
        """Run only the repeat prompt; never send a group message."""
        return await self._run_test(kind="repeat")

    async def test_mute(self):
        """Run only the mute-notice prompt; never call moderation APIs."""
        return await self._run_test(kind="mute")

    async def get_history(self):
        """Serve bounded, time-windowed call records."""
        shutdown_response = self.shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        if not self._plugin._history_available:
            return error_response(
                self._plugin._history_storage_error or "调用记录存储不可用。",
                status_code=503,
            )
        raw_window = request.query.get("window", "24h")

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
            history_page = await self._plugin.history_store.query(
                window=raw_window,
                kind=None if raw_kind == "all" else raw_kind,
                page=page,
                page_size=page_size,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[repeater] 读取调用记录失败")
            return error_response("调用记录存储不可用。", status_code=503)
        return json_response({"status": "ok", "data": history_page.to_dict()})

    async def clear_history(self):
        """Delete all persisted LLM call records."""
        shutdown_response = self.shutdown_response()
        if shutdown_response is not None:
            return shutdown_response
        if not self._plugin._history_available:
            return error_response(
                self._plugin._history_storage_error or "调用记录存储不可用。",
                status_code=503,
            )
        try:
            deleted = await self._plugin.history_store.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[repeater] 清理调用记录失败")
            return error_response("调用记录存储不可用。", status_code=503)
        return json_response({"status": "ok", "data": {"deleted": deleted}})
