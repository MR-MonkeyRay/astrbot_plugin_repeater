"""复读插件的配置验证与运行时策略。"""

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


DEFAULT_INTERRUPT_TEXT = "打断！"
DEFAULT_INTERRUPT_MUTE_TEXT = "用户{user}因命中打断复读禁言策略而被禁言{time}s"
DEFAULT_INTERRUPT_MUTE_PROXY_TEXT = (
    "群管 {admin} 免于打断复读禁言，由下一位发言的 {user} 代为受罚 {time}s"
)
DEFAULT_INTELLIGENT_INTERRUPT_PROMPT = (
    "你是友善、机智的群聊复读打断助手。根据用户反复发送的内容，只生成一条简短、有趣、适合群聊的打断语。"
    "可以复读、打乱顺序或合理开玩笑；不得辱骂、歧视、威胁、露骨或攻击个人。不要解释、不要加引号、不要输出前缀。"
)
DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT = (
    "你是友善、机智的群聊禁言通知助手。根据提供的被禁言用户和禁言时长，只生成一条简短、有趣、适合群聊的禁言提示。"
    "不得辱骂、歧视、威胁、露骨或攻击个人。必须保留用户名称和禁言时长；不要解释、不要加引号、不要输出前缀。"
)
DEFAULT_INTELLIGENT_PROXY_MUTE_PROMPT = (
    "你是调皮捣蛋、爱看热闹的群聊禁言播报员。群管打断复读后凭特权逃过了禁言，"
    "这份惩罚转嫁给了下一位发言的倒霉群友。根据提供的替罪羊、免罪群管和禁言时长，"
    "只生成一条简短、俏皮、带点幸灾乐祸的顶替禁言播报，可以调侃群管的特权和替罪羊的运气，"
    "可以适当使用表情符号。不得辱骂、歧视、威胁、露骨或攻击个人。"
    "必须同时提到替罪羊名称、群管名称和禁言时长；不要解释、不要加引号、不要输出前缀。"
)

INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT = "astrbot"
INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE = "openai_compatible"
INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH = 256
INTELLIGENT_INTERRUPT_MANUAL_API_KEY_MAX_LENGTH = 512

DEFAULT_REPEAT_COOLDOWN_SECONDS = 1800
MIN_REPEAT_COOLDOWN_SECONDS = 60
MAX_REPEAT_COOLDOWN_SECONDS = 86400
DEFAULT_LLM_TIMEOUT_SECONDS = 15
MIN_LLM_TIMEOUT_SECONDS = 1
MAX_LLM_TIMEOUT_SECONDS = 120

CONFIG_SECTION_REPEAT = "repeat"
CONFIG_SECTION_INTERRUPT = "interrupt"
CONFIG_SECTION_MUTE = "mute"
CONFIG_SECTION_INTELLIGENT_PROVIDER = "intelligent_provider"
CONFIG_SECTION_INTELLIGENT_INTERRUPT = "intelligent_interrupt"
CONFIG_SECTION_INTELLIGENT_MUTE = "intelligent_mute"


@dataclass(slots=True)
class RepeaterSettings:
    """经验证后供复读状态机使用的全局策略。"""

    config: dict[str, Any]

    # 基础复读
    default_enabled: bool
    repeat_disabled_group_ids: set[str]
    repeat_threshold: int
    repeat_probability: float

    # 打断复读
    interrupt_default_enabled: bool
    interrupt_disabled_group_ids: set[str]
    interrupt_threshold: int
    interrupt_probability: float
    interrupt_texts: tuple[str, ...]

    # 打断复读禁言
    interrupt_mute_enabled: bool
    interrupt_mute_disabled_group_ids: set[str]
    interrupt_mute_probability: float
    interrupt_mute_duration_min: int
    interrupt_mute_duration_max: int
    interrupt_mute_texts: tuple[str, ...]

    # LLM供应商共用
    intelligent_interrupt_provider_mode: str
    intelligent_interrupt_provider_id: str
    intelligent_interrupt_manual_api_base: str
    intelligent_interrupt_manual_api_key: str
    intelligent_interrupt_model: str

    # 智能打断
    intelligent_interrupt_enabled: bool
    intelligent_interrupt_prompt: str

    # 智能禁言提示
    intelligent_interrupt_mute_enabled: bool
    intelligent_interrupt_mute_prompt: str

    # 有默认值的字段放在末尾，便于直接构造设置对象
    repeat_cooldown_seconds: int = DEFAULT_REPEAT_COOLDOWN_SECONDS
    intelligent_timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS
    interrupt_mute_proxy_texts: tuple[str, ...] = (DEFAULT_INTERRUPT_MUTE_PROXY_TEXT,)
    intelligent_proxy_mute_prompt: str = DEFAULT_INTELLIGENT_PROXY_MUTE_PROMPT

    async def save_config(self) -> None:
        """将群级开关写回分组配置并持久化。"""
        repeat_config = self.config.get(CONFIG_SECTION_REPEAT)
        if not isinstance(repeat_config, dict):
            repeat_config = {}
            self.config[CONFIG_SECTION_REPEAT] = repeat_config
        interrupt_config = self.config.get(CONFIG_SECTION_INTERRUPT)
        if not isinstance(interrupt_config, dict):
            interrupt_config = {}
            self.config[CONFIG_SECTION_INTERRUPT] = interrupt_config
        mute_config = self.config.get(CONFIG_SECTION_MUTE)
        if not isinstance(mute_config, dict):
            mute_config = {}
            self.config[CONFIG_SECTION_MUTE] = mute_config

        repeat_config["disabled_group_ids"] = sorted(self.repeat_disabled_group_ids)
        interrupt_config["disabled_group_ids"] = sorted(
            self.interrupt_disabled_group_ids,
        )
        mute_config["disabled_group_ids"] = sorted(
            self.interrupt_mute_disabled_group_ids,
        )
        await persist_config(
            self.config,
            {
                CONFIG_SECTION_REPEAT: repeat_config,
                CONFIG_SECTION_INTERRUPT: interrupt_config,
                CONFIG_SECTION_MUTE: mute_config,
            },
        )


async def persist_config(config: Any, updates: dict[str, Any]) -> None:
    """通过 AstrBotConfig 的公开接口合并并保存配置。

    ``save_config_async`` 会在锁内合并 ``updates`` 并在线程中写盘；返回 False
    仅表示更新的快照已经取代本次快照，而更新快照同样包含本次合并的内容，
    因此无需回滚。保存一旦开始，即使调用方被取消也会等待写盘结束再传播取消，
    避免内存配置与磁盘状态不一致。

    Args:
        config: 插件配置对象；普通 dict 时只合并内存。
        updates: 要合并的顶层配置分组。
    """
    save_config_async = getattr(config, "save_config_async", None)
    if callable(save_config_async):
        await _await_settled(save_config_async(updates))
        return
    config.update(updates)
    save_config = getattr(config, "save_config", None)
    if callable(save_config):
        result = save_config()
        if inspect.isawaitable(result):
            await _await_settled(result)


async def _await_settled(awaitable: Any) -> Any:
    """等待已开始的保存结束；期间收到的取消在结束后重新抛出。"""
    task = asyncio.ensure_future(awaitable)
    was_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            was_cancelled = True
    result = task.result()
    if was_cancelled:
        raise asyncio.CancelledError
    return result


def _config_section(
    config: dict[str, Any],
    section_name: str,
    logger: Any,
) -> dict[str, Any]:
    """Return one validated configuration section without allocating on success."""
    section = config.get(section_name, {})
    if isinstance(section, dict):
        return section
    logger.warning(f"[repeater] {section_name} 配置块非法，使用默认配置")
    return {}


def build_settings(config: dict[str, Any], logger: Any) -> RepeaterSettings:
    """验证分组插件配置并构造运行时策略。"""
    repeat_config = _config_section(config, CONFIG_SECTION_REPEAT, logger)
    interrupt_config = _config_section(config, CONFIG_SECTION_INTERRUPT, logger)
    mute_config = _config_section(config, CONFIG_SECTION_MUTE, logger)
    intelligent_provider_config = _config_section(
        config,
        CONFIG_SECTION_INTELLIGENT_PROVIDER,
        logger,
    )
    intelligent_interrupt_config = _config_section(
        config,
        CONFIG_SECTION_INTELLIGENT_INTERRUPT,
        logger,
    )
    intelligent_mute_config = _config_section(
        config,
        CONFIG_SECTION_INTELLIGENT_MUTE,
        logger,
    )

    # 基础复读
    default_enabled = _validated_bool(
        repeat_config.get("default_enabled", True),
        "repeat.default_enabled",
        True,
        logger,
    )
    repeat_disabled_group_ids = _load_group_ids(
        repeat_config.get("disabled_group_ids", []),
        "repeat.disabled_group_ids",
        logger,
    )
    repeat_threshold = _validated_integer(
        repeat_config.get("threshold", 3),
        "repeat.threshold",
        3,
        3,
        logger,
    )
    probability = _validated_probability(
        repeat_config.get("probability", 0.3),
        "repeat.probability",
        0.3,
        logger,
    )
    repeat_cooldown_seconds = _validated_bounded_integer(
        repeat_config.get("cooldown_seconds", DEFAULT_REPEAT_COOLDOWN_SECONDS),
        "repeat.cooldown_seconds",
        DEFAULT_REPEAT_COOLDOWN_SECONDS,
        MIN_REPEAT_COOLDOWN_SECONDS,
        MAX_REPEAT_COOLDOWN_SECONDS,
        logger,
    )

    # 打断复读
    interrupt_default_enabled = _validated_bool(
        interrupt_config.get("default_enabled", True),
        "interrupt.default_enabled",
        True,
        logger,
    )
    interrupt_disabled_group_ids = _load_group_ids(
        interrupt_config.get("disabled_group_ids", []),
        "interrupt.disabled_group_ids",
        logger,
    )
    interrupt_threshold = _validated_integer(
        interrupt_config.get("threshold", 3),
        "interrupt.threshold",
        3,
        3,
        logger,
    )
    interrupt_probability = _validated_probability(
        interrupt_config.get("probability", 0.1),
        "interrupt.probability",
        0.1,
        logger,
    )
    interrupt_texts = _validated_texts(
        interrupt_config.get("texts", (DEFAULT_INTERRUPT_TEXT,)),
        DEFAULT_INTERRUPT_TEXT,
        "[repeater] interrupt.texts 非法或为空，回退为默认打断文本",
        logger,
    )

    # 打断后禁言
    interrupt_mute_enabled = _validated_bool(
        mute_config.get("enabled", False),
        "mute.enabled",
        False,
        logger,
    )
    interrupt_mute_disabled_group_ids = _load_group_ids(
        mute_config.get("disabled_group_ids", []),
        "mute.disabled_group_ids",
        logger,
    )
    interrupt_mute_probability = _validated_probability(
        mute_config.get("probability", 0.05),
        "mute.probability",
        0.05,
        logger,
    )
    duration_min = _validated_duration(
        mute_config.get("duration_min", 1),
        "mute.duration_min",
        1,
        logger,
    )
    duration_max = _validated_duration(
        mute_config.get("duration_max", 15),
        "mute.duration_max",
        15,
        logger,
    )
    if duration_max < duration_min:
        logger.warning(
            "[repeater] mute.duration_max 小于 mute.duration_min，使用下限值",
        )
        duration_max = duration_min
    interrupt_mute_texts = _validated_texts(
        mute_config.get("texts", (DEFAULT_INTERRUPT_MUTE_TEXT,)),
        DEFAULT_INTERRUPT_MUTE_TEXT,
        "[repeater] mute.texts 非法或为空，回退为默认禁言文本",
        logger,
    )
    interrupt_mute_proxy_texts = _validated_texts(
        mute_config.get("proxy_texts", (DEFAULT_INTERRUPT_MUTE_PROXY_TEXT,)),
        DEFAULT_INTERRUPT_MUTE_PROXY_TEXT,
        "[repeater] mute.proxy_texts 非法或为空，回退为默认顶替禁言文本",
        logger,
    )

    # LLM供应商
    intelligent_interrupt_provider_mode = (
        _validated_intelligent_interrupt_provider_mode(
            intelligent_provider_config.get(
                "mode",
                INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
            ),
            logger,
        )
    )
    intelligent_interrupt_provider_id = _validated_optional_text(
        intelligent_provider_config.get("provider_id", ""),
        "intelligent_provider.provider_id",
        logger,
    )
    intelligent_interrupt_manual_api_base = _validated_manual_api_base(
        intelligent_provider_config.get("manual_api_base", ""),
        logger,
    )
    intelligent_interrupt_manual_api_key = _validated_manual_api_key(
        intelligent_provider_config.get("manual_api_key", ""),
        logger,
    )
    intelligent_interrupt_model = _validated_optional_text(
        intelligent_provider_config.get("model", ""),
        "intelligent_provider.model",
        logger,
    )
    intelligent_timeout_seconds = _validated_bounded_integer(
        intelligent_provider_config.get(
            "timeout_seconds",
            DEFAULT_LLM_TIMEOUT_SECONDS,
        ),
        "intelligent_provider.timeout_seconds",
        DEFAULT_LLM_TIMEOUT_SECONDS,
        MIN_LLM_TIMEOUT_SECONDS,
        MAX_LLM_TIMEOUT_SECONDS,
        logger,
    )

    # 智能打断
    intelligent_interrupt_enabled = _validated_bool(
        intelligent_interrupt_config.get("enabled", False),
        "intelligent_interrupt.enabled",
        False,
        logger,
    )
    intelligent_interrupt_prompt = _validated_required_text(
        intelligent_interrupt_config.get(
            "prompt",
            DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
        ),
        "intelligent_interrupt.prompt",
        DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
        logger,
        "智能打断",
    )

    # 智能禁言提示
    intelligent_interrupt_mute_enabled = _validated_bool(
        intelligent_mute_config.get("enabled", False),
        "intelligent_mute.enabled",
        False,
        logger,
    )
    intelligent_interrupt_mute_prompt = _validated_required_text(
        intelligent_mute_config.get(
            "prompt",
            DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
        ),
        "intelligent_mute.prompt",
        DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
        logger,
        "智能禁言",
    )
    intelligent_proxy_mute_prompt = _validated_required_text(
        intelligent_mute_config.get(
            "proxy_prompt",
            DEFAULT_INTELLIGENT_PROXY_MUTE_PROMPT,
        ),
        "intelligent_mute.proxy_prompt",
        DEFAULT_INTELLIGENT_PROXY_MUTE_PROMPT,
        logger,
        "智能顶替禁言",
    )

    return RepeaterSettings(
        config=config,
        default_enabled=default_enabled,
        repeat_disabled_group_ids=repeat_disabled_group_ids,
        repeat_threshold=repeat_threshold,
        repeat_probability=probability,
        interrupt_default_enabled=interrupt_default_enabled,
        interrupt_disabled_group_ids=interrupt_disabled_group_ids,
        interrupt_threshold=interrupt_threshold,
        interrupt_probability=interrupt_probability,
        interrupt_texts=interrupt_texts,
        interrupt_mute_enabled=interrupt_mute_enabled,
        interrupt_mute_disabled_group_ids=interrupt_mute_disabled_group_ids,
        interrupt_mute_probability=interrupt_mute_probability,
        interrupt_mute_duration_min=duration_min,
        interrupt_mute_duration_max=duration_max,
        interrupt_mute_texts=interrupt_mute_texts,
        intelligent_interrupt_provider_mode=intelligent_interrupt_provider_mode,
        intelligent_interrupt_provider_id=intelligent_interrupt_provider_id,
        intelligent_interrupt_manual_api_base=intelligent_interrupt_manual_api_base,
        intelligent_interrupt_manual_api_key=intelligent_interrupt_manual_api_key,
        intelligent_interrupt_model=intelligent_interrupt_model,
        intelligent_interrupt_enabled=intelligent_interrupt_enabled,
        intelligent_interrupt_prompt=intelligent_interrupt_prompt,
        intelligent_interrupt_mute_enabled=intelligent_interrupt_mute_enabled,
        intelligent_interrupt_mute_prompt=intelligent_interrupt_mute_prompt,
        repeat_cooldown_seconds=repeat_cooldown_seconds,
        intelligent_timeout_seconds=intelligent_timeout_seconds,
        interrupt_mute_proxy_texts=interrupt_mute_proxy_texts,
        intelligent_proxy_mute_prompt=intelligent_proxy_mute_prompt,
    )


def normalize_intelligent_interrupt_provider_mode(value: Any) -> str:
    """将LLM供应商模式规范化为受支持的值。"""
    if isinstance(value, str):
        mode = value.strip()
        if mode in {
            INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT,
            INTELLIGENT_INTERRUPT_PROVIDER_MODE_OPENAI_COMPATIBLE,
        }:
            return mode
    return INTELLIGENT_INTERRUPT_PROVIDER_MODE_ASTRBOT


def normalize_intelligent_interrupt_manual_api_base(value: Any) -> str | None:
    """规范化 OpenAI-compatible Base URL；非法输入返回 ``None``。"""
    if not isinstance(value, str):
        return None

    api_base = value.strip()
    if not api_base:
        return ""
    if (
        len(api_base) > INTELLIGENT_INTERRUPT_MANUAL_API_BASE_MAX_LENGTH
        or any(character.isspace() for character in api_base)
        or "?" in api_base
        or "#" in api_base
    ):
        return None

    try:
        parsed = urlsplit(api_base)
        _ = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.rsplit("@", 1)[-1].endswith(":")
    ):
        return None
    return api_base.rstrip("/")


def normalize_intelligent_interrupt_manual_api_key(value: Any) -> str | None:
    """规范化手动供应商 API Key；非法输入返回 ``None``。"""
    if not isinstance(value, str):
        return None

    api_key = value.strip()
    if len(api_key) > INTELLIGENT_INTERRUPT_MANUAL_API_KEY_MAX_LENGTH:
        return None
    return api_key


def _validated_intelligent_interrupt_provider_mode(value: Any, logger: Any) -> str:
    mode = normalize_intelligent_interrupt_provider_mode(value)
    if isinstance(value, str) and value.strip() == mode:
        return mode
    logger.warning(
        "[repeater] intelligent_interrupt_provider_mode is invalid; using astrbot",
    )
    return mode


def _validated_manual_api_base(value: Any, logger: Any) -> str:
    api_base = normalize_intelligent_interrupt_manual_api_base(value)
    if api_base is not None:
        return api_base
    logger.warning(
        "[repeater] intelligent_interrupt_manual_api_base is invalid; "
        "treating as unconfigured",
    )
    return ""


def _validated_manual_api_key(value: Any, logger: Any) -> str:
    api_key = normalize_intelligent_interrupt_manual_api_key(value)
    if api_key is not None:
        return api_key
    logger.warning(
        "[repeater] intelligent_interrupt_manual_api_key is invalid; "
        "treating as unconfigured",
    )
    return ""


def _validated_integer(
    value: Any, field_name: str, default: int, minimum: int, logger: Any
) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    logger.warning(f"[repeater] {field_name} 非法({value})，回退为 {default}")
    return default


def _validated_probability(
    value: Any, field_name: str, default: float, logger: Any
) -> float:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.0 <= value <= 1.0
    ):
        return float(value)
    logger.warning(f"[repeater] {field_name} 非法({value})，回退为 {default}")
    return default


def _validated_bool(value: Any, field_name: str, default: bool, logger: Any) -> bool:
    if isinstance(value, bool):
        return value
    logger.warning(f"[repeater] {field_name} 非法({value})，回退为 {default}")
    return default


def _validated_optional_text(value: Any, field_name: str, logger: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    logger.warning(f"[repeater] {field_name} 非法({value})，回退为空字符串")
    return ""


def _validated_required_text(
    value: Any,
    field_name: str,
    default: str,
    logger: Any,
    fallback_label: str,
) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    logger.warning(
        f"[repeater] {field_name} 非法或为空，回退为默认{fallback_label}提示词"
    )
    return default


def _validated_bounded_integer(
    value: Any,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
    logger: Any,
) -> int:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    ):
        return value
    logger.warning(f"[repeater] {field_name} 非法({value})，回退为 {default}")
    return default


def _validated_duration(value: Any, field_name: str, default: int, logger: Any) -> int:
    return _validated_bounded_integer(value, field_name, default, 1, 3600, logger)


def _validated_texts(
    value: Any, default: str, warning: str, logger: Any
) -> tuple[str, ...]:
    texts: tuple[str, ...] = ()
    if isinstance(value, (list, tuple)):
        texts = tuple(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    if texts:
        return texts
    logger.warning(warning)
    return (default,)


def _load_group_ids(value: Any, field_name: str, logger: Any) -> set[str]:
    """从配置字段读取可用的群 ID 集合。"""
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
