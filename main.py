from dotenv import load_dotenv
load_dotenv()

import re
import sys
import os
import uuid
from langgraph.types import Command

url = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
if not url:
    print("用法: python main.py <github_url> [--security] [--execute-patch] [--create-pr]")
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
    # 默认只生成候选补丁；必须显式传参才会在临时 worktree 中执行测试。
    from workflow.ci_graph import ci_app
    print(f"🧪 CI 失败分析模式: {url}")
    # 执行权限逐次授予，不通过配置文件默认打开，防止日常分析意外运行第三方代码。
    create_pr = "--create-pr" in sys.argv
    # 发布必须建立在真实测试结果上，因此 --create-pr 隐式启用隔离执行。
    execute_patch = "--execute-patch" in sys.argv or create_pr
    if execute_patch:
        print("⚠️  已授权：将在隔离 worktree 中应用候选补丁并运行受限测试")
    if create_pr:
        print("⚠️  已授权 GitHub 写入：全部门禁通过后将创建修复分支和 Draft PR")
    result = ci_app.invoke({
        "ci_url": url, "iteration": 0, "patch_attempt": 0,
        "patch_history": [],
        "execute_patch": execute_patch, "create_pr": create_pr, "error": None,
    }, config=config)
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
