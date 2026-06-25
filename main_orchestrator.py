"""
跨仓库 A2A 分析入口

使用前：先在另一个终端启动 RepoAgent 服务
  python agents/repo_agent_server.py

然后运行：
  python main_orchestrator.py
"""
from dotenv import load_dotenv
load_dotenv()

from agents.orchestrator.graph import orchestrator

result = orchestrator.invoke({
    "issue_url": "https://github.com/fastapi/fastapi/issues/1234",
    "repo_results": [],
    "error": None,
})

print(result["final_report"])
