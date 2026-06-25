import operator
from typing import Annotated
from langgraph.graph import StateGraph, START, END
from agents.orchestrator.state import OrchestratorState, RepoAnalysisState
from agents.orchestrator.nodes import fetch_and_detect, dispatch_to_repo_agents, repo_analysis, merge_reports


# repo_results 用 operator.add reducer，Send 子节点的结果自动追加
class OrchestratorStateWithReducer(OrchestratorState):
    repo_results: Annotated[list[dict], operator.add]


def build_orchestrator():
    graph = StateGraph(OrchestratorStateWithReducer)

    graph.add_node("fetch_and_detect", fetch_and_detect)
    graph.add_node("repo_analysis", repo_analysis)
    graph.add_node("merge_reports", merge_reports)

    graph.add_edge(START, "fetch_and_detect")

    # Send API：fetch_and_detect 完成后，动态并行分发给每个 repo_analysis
    graph.add_conditional_edges(
        "fetch_and_detect",
        dispatch_to_repo_agents,
    )

    # 所有并行 repo_analysis 完成后汇入 merge_reports
    graph.add_edge("repo_analysis", "merge_reports")
    graph.add_edge("merge_reports", END)

    return graph.compile()


orchestrator = build_orchestrator()
