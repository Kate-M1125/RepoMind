from dotenv import load_dotenv
load_dotenv()

import re
import sys
import os
import uuid
from langgraph.types import Command

url = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
if not url:
    print("用法: python main.py <github_issue_or_pr_url> [--security]")
    print("  Issue:  https://github.com/owner/repo/issues/123")
    print("  PR:     https://github.com/owner/repo/pull/123")
    sys.exit(1)

_is_pr = bool(re.search(r"github\.com/[^/]+/[^/]+/pull/\d+", url))

config = {"configurable": {"thread_id": str(uuid.uuid4())}}

if _is_pr:
    from workflow.pr_graph import pr_app
    print(f"🔍 PR Review 模式: {url}")
    result = pr_app.invoke({"pr_url": url, "iteration": 0, "error": None}, config=config)
    print(result["final_report"])

else:
    # --security 参数：强制 issue_type=security 触发 HITL
    if "--security" in sys.argv:
        os.environ["FORCE_ISSUE_TYPE"] = "security"
        print("⚠️  模拟模式：强制 issue_type = security，将触发 HITL")

    from workflow.graph import app
    print(f"🐛 Issue 分析模式: {url}")
    result = app.invoke({"issue_url": url, "iteration": 0, "error": None}, config=config)

    if result.get("__interrupt__"):
        info = result["__interrupt__"][0].value
        print(f"\n{'='*50}")
        print(f"⚠️  工作流暂停，等待人工确认")
        print(f"  Issue: {info['issue']}")
        print(f"  内容: {info['body']}")
        print(f"{'='*50}")
        decision = input("\n输入决定 (approve/reject): ").strip()
        result = app.invoke(Command(resume=decision), config=config)

    print(result["final_report"])