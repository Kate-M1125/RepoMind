from dotenv import load_dotenv
load_dotenv()

import sys
import os
import uuid
from langgraph.types import Command

# --security 参数：强制 issue_type=security 触发 HITL
if "--security" in sys.argv:
    os.environ["FORCE_ISSUE_TYPE"] = "security"
    print("⚠️  模拟模式：强制 issue_type = security，将触发 HITL")

from workflow.graph import app

issue_url = "https://github.com/fastapi/fastapi/issues/1234"
config = {"configurable": {"thread_id": str(uuid.uuid4())}}

result = app.invoke({
    "issue_url": issue_url,
    "iteration": 0,
    "error": None,
}, config=config)

# 检查是否被 HITL 暂停
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