"""提示词清单：读取配置中的名片特写提示词，提供序号、清单文本与等待提示。"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TASK_HINT = "正在努力生成图片中，请耐心等待几分钟..."
DEFAULT_PROMPT_NAME = "提示词"
MENU_TITLE = "名片特写"
CHOICE_TIMEOUT = 15


@dataclass(frozen=True)
class Prompt:
    """一条提示词。index 从 1 起，与清单展示的序号一致。"""

    index: int
    name: str
    text: str


def load_prompts(raw: object) -> list[Prompt]:
    """把配置中的提示词列表规整为带序号的清单。

    跳过内容为空的条目；序号按当前顺序连续生成，不留空号。
    """
    items = raw if isinstance(raw, list) else []
    prompts: list[Prompt] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("prompt") or "").strip()
        if not text:
            continue
        name = (
            str(item.get("name") or "").strip()
            or f"{DEFAULT_PROMPT_NAME}{len(prompts) + 1}"
        )
        prompts.append(Prompt(index=len(prompts) + 1, name=name, text=text))
    return prompts


def menu_text(
    prompts: list[Prompt],
    *,
    title: str = MENU_TITLE,
    timeout: int = CHOICE_TIMEOUT,
) -> str:
    """清单文本：标题、逐行「序号. 名称」、选择提示。"""
    lines = "\n".join(f"{prompt.index}. {prompt.name}" for prompt in prompts)
    return f"{title}\n{lines}\n\n发送序号即可，{timeout} 秒后自动选 1"


def task_hint(value: object) -> str:
    """生图等待提示。配置为空时使用默认文案。"""
    text = str(value or "").strip()
    return text or DEFAULT_TASK_HINT


def render_prompt(template: str, *, school: str, scene: str, accent: str) -> str:
    """替换提示词中的 {school} / {scene} / {accent} 占位符。

    自定义提示词不含占位符时按原文使用；含无法识别的花括号时同样按原文使用。
    """
    try:
        return template.format(school=school, scene=scene, accent=accent)
    except (KeyError, IndexError, ValueError):
        return template
