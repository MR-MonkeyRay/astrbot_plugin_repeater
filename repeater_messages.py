"""将框架消息规范化为可判重、可安全回发的复读表示。"""

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from astrbot.api.message_components import (
    BaseMessageComponent,
    ComponentType,
    Plain,
)


class RawOneBotSegment(BaseMessageComponent):
    """保存 AstrBot 尚未建模、但 OneBot 可以原样回发的消息段。

    Attributes:
        segment_type: OneBot 消息段类型。
        data: 该消息段的深拷贝数据负载。
    """

    type: ComponentType = ComponentType.Unknown
    segment_type: str
    data: dict[str, Any]

    def __init__(self, segment_type: str, data: dict[str, Any]) -> None:
        """以深拷贝数据创建一个可原样回发的 OneBot 消息段。

        Args:
            segment_type: OneBot 提供的消息段类型。
            data: OneBot 消息段的数据负载。
        """
        super().__init__(segment_type=segment_type, data=copy.deepcopy(data))

    def toDict(self) -> dict[str, Any]:
        """序列化为 AstrBot/OneBot 使用的消息段字典。

        Returns:
            包含 type 和 data 键的消息段字典。
        """
        return {"type": self.segment_type, "data": self.data}


@dataclass(frozen=True, slots=True)
class RepeatableMessage:
    """用于判重和回发的规范化消息。

    Attributes:
        fingerprint: 标识消息内容和顺序的稳定 SHA-256 指纹。
        text: 事件提供的去除首尾空白后的文本表示。
        chain: 普通复读时应原样回发的非纯文本消息链。
        summary: 用于纯文本回发、日志和兜底显示的消息摘要。
    """

    fingerprint: str
    text: str
    chain: tuple[BaseMessageComponent, ...]
    summary: str


def fingerprint(text: str) -> str:
    """计算 UTF-8 文本的 SHA-256 十六进制指纹。

    Args:
        text: 要参与判重的文本。

    Returns:
        64 个十六进制字符组成的 SHA-256 摘要。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chain_fingerprint(identities: list[dict[str, Any]]) -> str:
    """计算规范化消息链身份列表的稳定指纹。

    Args:
        identities: 按消息链顺序排列的已规范化段身份。

    Returns:
        包含消息链格式版本的 SHA-256 指纹。
    """
    payload = json.dumps(
        ["message-chain-v1", identities],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return fingerprint(payload)


def canonical_value(value: Any) -> Any:
    """将任意段数据转换为稳定、可 JSON 序列化的身份值。

    Args:
        value: 要规范化的原始数据值。

    Returns:
        保留基础标量、递归排序容器并散列字节串后的稳定值。
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, set):
        return sorted((canonical_value(item) for item in value), key=repr)
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def segment_identity(
    segment_type: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """构造一个消息段的判重身份。

    Args:
        segment_type: 消息段类型。
        data: 消息段数据负载。

    Returns:
        对文本、图片、表情和其他段类型均稳定的身份字典。
    """
    normalized_type = segment_type.lower()
    if normalized_type in {"plain", "text"}:
        return {
            "type": "text",
            "text": str(data.get("text", "")).strip(),
        }
    if normalized_type == "image":
        file_id = str(data.get("file") or "")
        url = str(data.get("url") or "")
        return {
            "type": "image",
            "id": file_id or url,
            "sub_type": str(data.get("sub_type") or data.get("type") or ""),
        }
    if normalized_type == "face":
        return {"type": "face", "id": str(data.get("id") or "")}
    if normalized_type == "mface":
        emoji_id = str(data.get("emoji_id") or "")
        package_id = str(data.get("emoji_package_id") or "")
        if emoji_id or package_id:
            identity: dict[str, Any] = {
                "emoji_id": emoji_id,
                "emoji_package_id": package_id,
            }
        else:
            identity = {
                "id": str(
                    data.get("key") or data.get("file") or data.get("url") or ""
                ),
            }
        return {"type": "mface", **identity}
    return {
        "type": normalized_type,
        "data": canonical_value(data),
    }


def raw_onebot_segments(event: Any) -> list[dict[str, Any]] | None:
    """提取包含 OneBot mface 段时可原样回发的原始消息链。

    Args:
        event: 可能携带 OneBot 原始消息对象的事件。

    Returns:
        已复制的原始消息段列表；缺少 mface 或结构无效时返回 None。
    """
    raw_message = getattr(event.message_obj, "raw_message", None)
    if isinstance(raw_message, Mapping):
        raw_segments = raw_message.get("message")
    else:
        raw_segments = getattr(raw_message, "message", None)
    if not isinstance(raw_segments, list):
        return None

    segments: list[dict[str, Any]] = []
    for segment in raw_segments:
        if not isinstance(segment, Mapping):
            return None
        segment_type = segment.get("type")
        data = segment.get("data")
        if not isinstance(segment_type, str) or not isinstance(data, Mapping):
            return None
        segments.append({"type": segment_type, "data": dict(data)})
    if not any(segment["type"].lower() == "mface" for segment in segments):
        return None
    return segments


def repeatable_message(event: Any) -> RepeatableMessage | None:
    """从事件构造用于判重和回发的规范化消息。

    Args:
        event: 提供文本、消息链及可选 OneBot 原始消息的事件。

    Returns:
        可用于复读的消息；事件没有任何有效内容时返回 None。
    """
    text = event.get_message_str().strip()
    raw_segments = raw_onebot_segments(event)
    if raw_segments is not None:
        identities = [
            segment_identity(segment["type"], segment["data"])
            for segment in raw_segments
        ]
        chain = tuple(
            RawOneBotSegment(segment["type"], segment["data"])
            for segment in raw_segments
        )
        labels = " ".join(f"[{segment['type']}]" for segment in raw_segments)
        return RepeatableMessage(
            fingerprint=chain_fingerprint(identities),
            text=text,
            chain=chain,
            summary=text or labels,
        )

    get_messages = getattr(event, "get_messages", None)
    chain = tuple(get_messages()) if callable(get_messages) else ()
    if not chain:
        if not text:
            return None
        return RepeatableMessage(
            fingerprint=fingerprint(text),
            text=text,
            chain=(),
            summary=text,
        )

    if all(isinstance(component, Plain) for component in chain):
        if not text:
            text = "".join(component.text for component in chain).strip()
        if not text:
            return None
        return RepeatableMessage(
            fingerprint=fingerprint(text),
            text=text,
            chain=(),
            summary=text,
        )

    identities: list[dict[str, Any]] = []
    labels: list[str] = []
    for component in chain:
        serialized = component.toDict()
        segment_type = str(serialized.get("type") or component.type.value)
        data = serialized.get("data")
        if not isinstance(data, Mapping):
            data = {}
        identities.append(segment_identity(segment_type, data))
        labels.append(f"[{segment_type}]")
    return RepeatableMessage(
        fingerprint=chain_fingerprint(identities),
        text=text,
        chain=chain,
        summary=text or " ".join(labels),
    )
