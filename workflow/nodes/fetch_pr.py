import re
import base64
import httpx
from workflow.pr_state import PRState
from tools.github_pr_api import fetch_pr as _fetch_pr
from core.llm.client import vision_client


def fetch_pr(state: PRState) -> dict:
    return _fetch_pr(state["pr_url"])


def describe_pr_images(state: PRState) -> dict:
    text = state.get("body", "") + "\n".join(c["body"] for c in state.get("comments", []))
    urls = re.findall(r'!\[.*?\]\((https?://\S+?)\)', text)
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
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "这是来自 GitHub PR 的图片，描述内容，重点关注错误信息、UI 变化、架构图。100字以内，直接描述。"},
                ]}],
                max_tokens=200,
            )
            desc = result.choices[0].message.content.strip()
            descriptions.append(f"[图片 {url}]\n{desc}")
        except Exception as e:
            descriptions.append(f"[图片 {url}]\n无法解析: {e}")

    return {"image_descriptions": "\n\n".join(descriptions)}
