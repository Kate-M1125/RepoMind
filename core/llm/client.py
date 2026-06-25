import re
import json
from typing import Literal
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError
from pydantic import BaseModel, Field, field_validator, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from config import settings

# 单例 LLM 客户端，整个应用共用
_client = OpenAI(
    api_key=settings.deepseek_api_key.get_secret_value(),
    base_url=settings.llm_base_url,
)


@retry(
    retry=retry_if_exception_type((APITimeoutError, APIConnectionError, RateLimitError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
def chat(system: str, prompt: str, max_tokens: int = 1000, json_mode: bool = False) -> str:
    kwargs = dict(
        model=settings.llm_model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content.strip()


def _repair_truncated_json(s: str) -> str:
    """
    对被截断的 JSON 字符串做最大努力修复：
    找最后一个完整的逗号分隔点（上一个 value 刚结束），截断后补 }。
    """
    # 找最后一个完整 kv 对结束的位置（逗号前）
    # 策略：从右往左找到最后一个 ',' 或 '{' 之后是完整 string/number 的位置
    # 简化：找最后一个 `": <complete_value>,` 的结尾，截到那里再补 }
    last_comma = s.rfind(",")
    last_open = s.rfind("{")
    cut = max(last_comma, last_open)
    if cut <= 0:
        return ""
    # 截到最后一个完整 value 后面，去掉尾部多余逗号
    truncated = s[: last_comma].rstrip() if last_comma > last_open else s[: last_open + 1]
    return truncated + "}"


def parse_json(text: str) -> dict:
    """三步降级 JSON 解析：直接解析 → 去 markdown → 正则提取"""
    original = text
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    candidate = match.group() if match else (text if text.lstrip().startswith("{") else "")
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # 截断修复：找最后一个完整的 key-value 对，截断后补全 }
        patched = _repair_truncated_json(candidate)
        if patched:
            try:
                return json.loads(patched)
            except json.JSONDecodeError:
                pass

    raise ValueError(f"JSON 解析失败，原始输出前 200 字:\n{original[:200]}")


# ── Pydantic Schema ────────────────────────────────────────────────

class CodeChange(BaseModel):
    file: str
    line: int = 0
    change: str


class AnalyzeResult(BaseModel):
    reasoning: str = ""
    root_cause: str
    affected_files: list[str] = Field(default_factory=list)
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    fix_suggestion: str
    code_changes: list[CodeChange] = Field(default_factory=list)
    suggested_labels: list[str] = Field(default_factory=list)

    @field_validator("root_cause", "fix_suggestion")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("字段不能为空")
        return v.strip()

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, v: str) -> str:
        v = str(v).lower().strip()
        return v if v in ("critical", "high", "medium", "low") else "medium"


_REPAIR_SYSTEM = "你是一名 JSON 修复专家。用户会给你一段格式错误或字段缺失的 JSON，你需要修复它并返回合法的、符合要求 Schema 的 JSON。"

_REPAIR_PROMPT_TEMPLATE = """以下 JSON 格式有误或字段缺失，请修复并返回合法 JSON。

要求字段：
- reasoning (str): 分析推理过程
- root_cause (str, 必填): 根本原因
- affected_files (list[str]): 受影响文件
- severity (str): critical/high/medium/low 之一
- fix_suggestion (str, 必填): 修复建议
- code_changes (list): 每项含 file/line/change
- suggested_labels (list[str]): 标签

原始输出:
{raw}"""


def _parse_and_validate(raw: str) -> AnalyzeResult:
    """解析 → 校验 → LLM 修复兜底，三步降级。"""
    try:
        return AnalyzeResult.model_validate(parse_json(raw))
    except (ValueError, ValidationError) as first_err:
        print(f"[WARNING] 首次解析失败，尝试修复: {first_err}")
        repair_prompt = _REPAIR_PROMPT_TEMPLATE.format(raw=raw)
        repaired_raw = chat(_REPAIR_SYSTEM, repair_prompt, max_tokens=2000, json_mode=True)
        try:
            return AnalyzeResult.model_validate(parse_json(repaired_raw))
        except (ValueError, ValidationError) as second_err:
            print(f"[ERROR] 修复失败，使用降级结果: {second_err}")
            return AnalyzeResult(
                root_cause="解析失败，请人工查看原始 Issue",
                fix_suggestion="LLM 输出格式异常，建议重试",
            )


def chat_json(system: str, prompt: str, max_tokens: int = 2000) -> AnalyzeResult:
    """带 Schema 校验 + LLM 修复兜底的 JSON 调用。"""
    raw = chat(system, prompt, max_tokens=max_tokens, json_mode=True)
    return _parse_and_validate(raw)


def chat_json_with_tools(
    system: str,
    prompt: str,
    tools: list[dict],
    tool_executor: dict,
    max_tokens: int = 2000,
    max_turns: int = 3,
) -> AnalyzeResult:
    """ReAct 工具调用 + Schema 校验 + LLM 修复兜底。"""
    raw = chat_with_tools(system, prompt, tools, tool_executor, max_tokens, max_turns)
    return _parse_and_validate(raw)


def chat_with_tools(
    system: str,
    prompt: str,
    tools: list[dict],
    tool_executor: dict,
    max_tokens: int = 2000,
    max_turns: int = 3,
) -> str:
    """
    ReAct 循环：LLM 可以多轮调用 tools，最终返回文本答案。
    - tools: OpenAI function calling 格式的工具定义列表
    - tool_executor: {tool_name: callable(**args) -> str}
    - max_turns: 最多几轮工具调用，超出后强制要求输出最终答案
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    for _ in range(max_turns):
        resp = _client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=max_tokens,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        # 把 assistant 的 tool_calls 消息追加进去
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        # 执行每个 tool call，把结果追加
        for tc in msg.tool_calls:
            fn = tool_executor.get(tc.function.name)
            try:
                args = json.loads(tc.function.arguments)
                result = fn(**args) if fn else f"未知工具: {tc.function.name}"
            except Exception as e:
                result = f"工具执行失败: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })

    # 超过 max_turns，强制要求最终 JSON 输出
    messages.append({"role": "user", "content": "基于以上所有信息，给出最终 JSON 分析结果。"})
    resp = _client.chat.completions.create(
        model=settings.llm_model,
        max_tokens=max_tokens,
        messages=messages,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""


# vision 客户端复用同一个 base_url（deepseek-vl2 在同一 endpoint）
vision_client = _client
