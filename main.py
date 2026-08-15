from dotenv import load_dotenv
load_dotenv()

import re
import sys
import os
import uuid
from langgraph.types import Command

url = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
if not url:
    print("用法: python main.py <github_issue_pr_or_actions_url> [--security]")
    print("  Issue:  https://github.com/owner/repo/issues/123")
    print("  PR:     https://github.com/owner/repo/pull/123")
    print("  CI Run: https://github.com/owner/repo/actions/runs/123456")
    sys.exit(1)

_is_pr = bool(re.search(r"github\.com/[^/]+/[^/]+/pull/\d+", url))
_is_ci = bool(re.search(r"github\.com/[^/]+/[^/]+/actions/runs/\d+", url))
_is_issue = bool(re.search(r"github\.com/[^/]+/[^/]+/issues/\d+", url))

if not (_is_pr or _is_ci or _is_issue):
    print(f"不支持的 GitHub URL: {url}")
    print("目前支持 Issue、Pull Request 和 Actions Run URL。")
    sys.exit(2)

config = {"configurable": {"thread_id": str(uuid.uuid4())}}

if _is_ci:
    # CI 使用独立状态和 checkpoint；第一阶段只读取日志并生成诊断报告。
    from workflow.ci_graph import ci_app
    print(f"🧪 CI 失败分析模式: {url}")
    result = ci_app.invoke({"ci_url": url, "iteration": 0, "error": None}, config=config)
    print(result["final_report"])

elif _is_pr:
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
