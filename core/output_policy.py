"""单条消息的输出模式临时覆盖。"""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class OutputOverride:
    mode: Optional[str] = None
    cleaned_text: str = ""


MODE_FLAGS = {
    "all": (True, True),
    "text": (True, False),
    "media": (False, True),
}
SUFFIX_TO_MODE = {"a": "all", "t": "text", "m": "media"}


def parse_output_override(message: str) -> OutputOverride:
    """解析消息末尾独立的 a/t/m 标记，并从待解析文本中移除。"""
    raw = message or ""
    stripped = raw.strip()
    if not stripped:
        return OutputOverride(mode=None, cleaned_text=raw)
    parts = stripped.rsplit(maxsplit=1)
    if len(parts) < 2:
        return OutputOverride(mode=None, cleaned_text=raw)
    body, suffix = parts[0], parts[1].lower().strip()
    mode = SUFFIX_TO_MODE.get(suffix)
    if not mode:
        return OutputOverride(mode=None, cleaned_text=raw)
    return OutputOverride(mode=mode, cleaned_text=body.strip())


def apply_override(
    default_flags: Tuple[bool, bool],
    override: Optional[OutputOverride],
) -> Tuple[bool, bool]:
    """将单条消息覆盖应用到默认的文本/富媒体输出标志。"""
    if not override or not override.mode:
        return default_flags
    return MODE_FLAGS.get(override.mode, default_flags)
