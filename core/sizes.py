"""比例与出图尺寸。

出图只使用 OpenAI 原生的 `size` 字段（像素串或 auto），不使用中转平台的
aspect_ratio / image_size 等扩展字段。

比例取值依据 OpenAI 官方支持集合：
- 官方标准尺寸：1024x1024（1:1）、1536x1024（3:2）、1024x1536（2:3）
- 其余为满足同一约束的对应比例尺寸：宽高都能被 16 整除、比例在 1:3~3:1、
  最长边不超过 3840、总像素不低于 655360
"""

from __future__ import annotations

AUTO = "auto"
UNSPECIFIED = "不指定"

# 比例 → size。键与 _conf_schema.json 里的下拉选项必须一致，改动时两处一起改。
RATIO_SIZES: dict[str, str] = {
    "1:1": "1024x1024",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "4:3": "2048x1536",
    "3:4": "1536x2048",
    "16:9": "2048x1152",
    "9:16": "1152x2048",
    "5:4": "1280x1024",
    "4:5": "1024x1280",
    "21:9": "2048x880",
}

DEFAULT_CARD_RATIO = "16:9"

# 下拉选项：「不指定」在最前，其余按 RATIO_SIZES 的声明顺序
RATIO_OPTIONS: tuple[str, ...] = (UNSPECIFIED, *RATIO_SIZES)


def size_for_ratio(ratio: object, fallback: str = AUTO) -> str:
    """比例转 size。空值、「不指定」与未收录的比例一律返回 fallback。"""
    return RATIO_SIZES.get(str(ratio or "").strip(), fallback)


def card_size(ratio: object) -> str:
    """名片卡底图尺寸。比例缺失或非法时退回默认比例，不交给接口自己定。"""
    return size_for_ratio(ratio, RATIO_SIZES[DEFAULT_CARD_RATIO])


def prompt_size(ratio: object) -> str:
    """名片特写尺寸。选「不指定」时返回 auto，由接口自己决定。"""
    return size_for_ratio(ratio)
