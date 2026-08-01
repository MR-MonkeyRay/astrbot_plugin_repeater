"""复读插件的配置验证与运行时策略。"""

from dataclasses import dataclass
from typing import Any


DEFAULT_INTERRUPT_TEXT = "打断！"
DEFAULT_INTERRUPT_MUTE_TEXT = "用户{user}因命中打断复读禁言策略而被禁言{time}s"
DEFAULT_INTELLIGENT_INTERRUPT_PROMPT = (
    "你是友善、机智的群聊复读打断助手。根据用户反复发送的内容，只生成一条简短、有趣、适合群聊的打断语。"
    "可以复读、打乱顺序或合理开玩笑；不得辱骂、歧视、威胁、露骨或攻击个人。不要解释、不要加引号、不要输出前缀。"
)
DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT = (
    "你是友善、机智的群聊禁言通知助手。根据提供的被禁言用户和禁言时长，只生成一条简短、有趣、适合群聊的禁言提示。"
    "不得辱骂、歧视、威胁、露骨或攻击个人。必须保留用户名称和禁言时长；不要解释、不要加引号、不要输出前缀。"
)


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
    interrupt_probability: float
    interrupt_texts: tuple[str, ...]

    # 打断复读禁言
    interrupt_mute_enabled: bool
    interrupt_mute_disabled_group_ids: set[str]
    interrupt_mute_probability: float
    interrupt_mute_duration_min: int
    interrupt_mute_duration_max: int
    interrupt_mute_texts: tuple[str, ...]

    # 智能文案共用
    intelligent_interrupt_provider_id: str
    intelligent_interrupt_model: str

    # 智能打断
    intelligent_interrupt_enabled: bool
    intelligent_interrupt_prompt: str

    # 智能禁言提示
    intelligent_interrupt_mute_enabled: bool
    intelligent_interrupt_mute_prompt: str

    def save_config(self) -> None:
        """将禁用群列表写回配置，并触发配置对象的保存钩子。"""
        self.config["repeat_disabled_group_ids"] = sorted(
            self.repeat_disabled_group_ids,
        )
        self.config["interrupt_disabled_group_ids"] = sorted(
            self.interrupt_disabled_group_ids,
        )
        self.config["interrupt_mute_disabled_group_ids"] = sorted(
            self.interrupt_mute_disabled_group_ids,
        )
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()


def build_settings(config: dict[str, Any], logger: Any) -> RepeaterSettings:
    """验证外部插件配置并构造运行时策略。"""
    # 基础复读
    default_enabled = _validated_bool(
        config.get("default_enabled", True), "default_enabled", True, logger
    )
    repeat_disabled_group_ids = _load_group_ids(
        config.get("repeat_disabled_group_ids", []),
        "repeat_disabled_group_ids",
        logger,
    )
    threshold = _validated_integer(
        config.get("repeat_threshold", 3), "repeat_threshold", 3, 2, logger
    )
    probability = _validated_probability(
        config.get("repeat_probability", 0.3), "repeat_probability", 0.3, logger
    )

    # 打断复读
    interrupt_default_enabled = _validated_bool(
        config.get("interrupt_default_enabled", True),
        "interrupt_default_enabled",
        True,
        logger,
    )
    interrupt_disabled_group_ids = _load_group_ids(
        config.get("interrupt_disabled_group_ids", []),
        "interrupt_disabled_group_ids",
        logger,
    )
    interrupt_probability = _validated_probability(
        config.get("interrupt_probability", 0.1),
        "interrupt_probability",
        0.1,
        logger,
    )
    interrupt_texts = _validated_texts(
        config.get("interrupt_texts", (DEFAULT_INTERRUPT_TEXT,)),
        DEFAULT_INTERRUPT_TEXT,
        "[repeater] interrupt_texts 非法或为空，回退为默认打断文本",
        logger,
    )

    # 打断复读禁言
    interrupt_mute_enabled = _validated_bool(
        config.get("interrupt_mute_enabled", False),
        "interrupt_mute_enabled",
        False,
        logger,
    )
    interrupt_mute_disabled_group_ids = _load_group_ids(
        config.get("interrupt_mute_disabled_group_ids", []),
        "interrupt_mute_disabled_group_ids",
        logger,
    )
    interrupt_mute_probability = _validated_probability(
        config.get("interrupt_mute_probability", 0.05),
        "interrupt_mute_probability",
        0.05,
        logger,
    )
    duration_min = _validated_duration(
        config.get("interrupt_mute_duration_min", 1),
        "interrupt_mute_duration_min",
        1,
        logger,
    )
    duration_max = _validated_duration(
        config.get("interrupt_mute_duration_max", 15),
        "interrupt_mute_duration_max",
        15,
        logger,
    )
    if duration_max < duration_min:
        logger.warning(
            "[repeater] interrupt_mute_duration_max 小于 "
            "interrupt_mute_duration_min，使用下限值",
        )
        duration_max = duration_min
    interrupt_mute_texts = _validated_texts(
        config.get("interrupt_mute_texts", (DEFAULT_INTERRUPT_MUTE_TEXT,)),
        DEFAULT_INTERRUPT_MUTE_TEXT,
        "[repeater] interrupt_mute_texts 非法或为空，回退为默认禁言文本",
        logger,
    )

    # 智能文案共用
    intelligent_interrupt_provider_id = _validated_optional_text(
        config.get("intelligent_interrupt_provider_id", ""),
        "intelligent_interrupt_provider_id",
        logger,
    )
    intelligent_interrupt_model = _validated_optional_text(
        config.get("intelligent_interrupt_model", ""),
        "intelligent_interrupt_model",
        logger,
    )

    # 智能打断
    intelligent_interrupt_enabled = _validated_bool(
        config.get("intelligent_interrupt_enabled", False),
        "intelligent_interrupt_enabled",
        False,
        logger,
    )
    intelligent_interrupt_prompt = _validated_required_text(
        config.get(
            "intelligent_interrupt_prompt",
            DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
        ),
        "intelligent_interrupt_prompt",
        DEFAULT_INTELLIGENT_INTERRUPT_PROMPT,
        logger,
        "智能打断",
    )

    # 智能禁言提示
    intelligent_interrupt_mute_enabled = _validated_bool(
        config.get("intelligent_interrupt_mute_enabled", False),
        "intelligent_interrupt_mute_enabled",
        False,
        logger,
    )
    intelligent_interrupt_mute_prompt = _validated_required_text(
        config.get(
            "intelligent_interrupt_mute_prompt",
            DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
        ),
        "intelligent_interrupt_mute_prompt",
        DEFAULT_INTELLIGENT_INTERRUPT_MUTE_PROMPT,
        logger,
        "智能禁言",
    )

    return RepeaterSettings(
        config=config,
        default_enabled=default_enabled,
        repeat_disabled_group_ids=repeat_disabled_group_ids,
        repeat_threshold=threshold,
        repeat_probability=probability,
        interrupt_default_enabled=interrupt_default_enabled,
        interrupt_disabled_group_ids=interrupt_disabled_group_ids,
        interrupt_probability=interrupt_probability,
        interrupt_texts=interrupt_texts,
        interrupt_mute_enabled=interrupt_mute_enabled,
        interrupt_mute_disabled_group_ids=interrupt_mute_disabled_group_ids,
        interrupt_mute_probability=interrupt_mute_probability,
        interrupt_mute_duration_min=duration_min,
        interrupt_mute_duration_max=duration_max,
        interrupt_mute_texts=interrupt_mute_texts,
        intelligent_interrupt_provider_id=intelligent_interrupt_provider_id,
        intelligent_interrupt_model=intelligent_interrupt_model,
        intelligent_interrupt_enabled=intelligent_interrupt_enabled,
        intelligent_interrupt_prompt=intelligent_interrupt_prompt,
        intelligent_interrupt_mute_enabled=intelligent_interrupt_mute_enabled,
        intelligent_interrupt_mute_prompt=intelligent_interrupt_mute_prompt,
    )


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


def _validated_duration(value: Any, field_name: str, default: int, logger: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 3600:
        return value
    logger.warning(f"[repeater] {field_name} 非法({value})，回退为 {default}")
    return default


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
