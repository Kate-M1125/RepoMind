"""
LLM 可调用 Tool 的基础抽象。

一个 Tool = OpenAI schema + 执行函数，两者放在一起，不分离。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    execute: Callable[..., str]

    def as_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def build_tools(*tools: Tool) -> tuple[list[dict], dict[str, Callable]]:
    """把多个 Tool 展开成 chat_with_tools 所需的两个参数。"""
    return (
        [t.as_openai() for t in tools],
        {t.name: t.execute for t in tools},
    )
