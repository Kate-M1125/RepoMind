import base64
import httpx
from workflow.state import IssueState
from tools.github_api import fetch_issue as _fetch_issue, parse_issue_url
from tools.stack_trace import extract_stack_context
from core.llm.client import vision_client
from config import settings


def fetch_issue(state: IssueState) -> dict:
    return _fetch_issue(state["issue_url"])


def describe_images(state: IssueState) -> dict:
    import re
    urls = re.findall(r'!\[.*?\]\((https?://\S+?)\)', state["body"])
    urls += re.findall(r'(?<!!)\b(https?://\S+\.(?:png|jpg|jpeg|gif|webp))\b', state["body"])
    urls = list(dict.fromkeys(urls))[:3]

    if not urls:
        return {"image_descriptions": ""}

    descriptions = []
    for url in urls:
        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            mime = resp.headers.get("content-type", "image/png").split(";")[0]
            b64 = base64.b64encode(resp.content).decode()
            data_url = f"data:{mime};base64,{b64}"

            result = vision_client.chat.completions.create(
                model="deepseek-vl2",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": (
                            "这是一张来自 GitHub Issue 的图片。"
                            "请描述图片内容，重点关注：错误信息、异常截图、UI 问题、代码片段。"
                            "100字以内，直接描述，不要客套话。"
                        )},
                    ],
                }],
                max_tokens=200,
            )
            desc = result.choices[0].message.content.strip()
            descriptions.append(f"[图片 {url}]\n{desc}")
        except Exception as e:
            descriptions.append(f"[图片 {url}]\n无法解析: {e}")

    return {"image_descriptions": "\n\n".join(descriptions)}


def parse_stack_trace(state: IssueState) -> dict:
    owner, repo, _ = parse_issue_url(state["issue_url"])
    full_text = state["body"] + "\n".join(c["body"] for c in state.get("comments", []))
    context = extract_stack_context(full_text, f"{owner}/{repo}")
    return {"stack_trace_context": context}
