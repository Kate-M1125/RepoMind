"""
评估 Runner：对数据集里每个 Issue 跑 RepoMind 分析，再交给 Judge 打分。
支持断点续跑（跳过已有结果）和并行执行。
"""
import json
import uuid
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from workflow.graph import app as langgraph_app
from eval.judge import judge_single, judge_feature, judge_question

_write_lock = threading.Lock()


def run_evaluation(
    dataset: list[dict],
    results_path: str = "eval/results.json",
    max_workers: int = 3,
    eval_type: str = "bug",
) -> list[dict]:
    """
    对 dataset 每条数据并行执行：
      1. 调 RepoMind 分析
      2. LLM Judge 打分
      3. 加锁写入 results_path（断点续跑安全）
    """
    results_file = Path(results_path)

    # 加载已有结果（断点续跑）
    existing: dict[str, dict] = {}
    if results_file.exists():
        for item in json.loads(results_file.read_text()):
            existing[item["issue_url"]] = item

    todo = [item for item in dataset if item["issue_url"] not in existing]
    total = len(dataset)

    for item in dataset:
        if item["issue_url"] in existing:
            idx = dataset.index(item) + 1
            print(f"[{idx}/{total}] 跳过（已有结果）: #{item['issue_number']}")

    if not todo:
        return list(existing.values())

    def process(item: dict) -> dict:
        idx = dataset.index(item) + 1
        print(f"\n[{idx}/{total}] 分析 Issue #{item['issue_number']}: {item['title'][:50]}")

        ai_result = _run_repomind(item["issue_url"])
        print(f"  [{idx}] 分析完成，置信度: {ai_result.get('confidence', 0):.2f}")

        print(f"  [{idx}] 开始 Judge 评分...")
        if eval_type == "feature":
            scores = judge_feature(
                title=item["title"],
                body=item["body"],
                ai_result=ai_result,
                ground_truth=item,
            )
            print(f"  [{idx}] Overall: {scores.get('overall', 0):.2f} | "
                  f"需求理解: {scores.get('requirement_understanding', 0):.2f} | "
                  f"架构适配: {scores.get('architecture_fit', 0):.2f} | "
                  f"可行性: {scores.get('implementation_feasibility', 0):.2f} | "
                  f"已存在检测: {scores.get('exists_detection_accuracy', 0):.2f}")
        elif eval_type == "question":
            scores = judge_question(
                title=item["title"],
                body=item["body"],
                ai_result=ai_result,
                key_answer=item.get("key_answer", ""),
            )
            print(f"  [{idx}] Overall: {scores.get('overall', 0):.2f} | "
                  f"问题识别: {scores.get('confusion_identified', 0):.2f} | "
                  f"技术准确: {scores.get('answer_accuracy', 0):.2f} | "
                  f"可操作: {scores.get('actionability', 0):.2f}")
        else:
            scores = judge_single(
                title=item["title"],
                body=item["body"],
                ai_result=ai_result,
                ground_truth=item,
            )
            print(f"  [{idx}] Overall: {scores.get('overall', 0):.2f} | "
                  f"根因: {scores.get('root_cause_accuracy', 0):.2f} | "
                  f"文件: {scores.get('file_accuracy', 0):.2f}")

        record = {
            "issue_url":    item["issue_url"],
            "issue_number": item["issue_number"],
            "title":        item["title"],
            "pr_url":       item.get("pr_url", ""),
            "eval_type":    eval_type,
            "ai_result":    ai_result,
            "scores":       scores,
        }

        # 加锁：读-改-写原子化，防止并发覆盖
        with _write_lock:
            current = json.loads(results_file.read_text()) if results_file.exists() else []
            current.append(record)
            results_file.write_text(json.dumps(current, ensure_ascii=False, indent=2))

        return record

    results = list(existing.values())
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process, item) for item in todo]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                print(f"  [error] 任务失败: {e}")

    return results


def _run_repomind(issue_url: str) -> dict:
    """调 LangGraph workflow，返回关键字段。"""
    try:
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        state = langgraph_app.invoke(
            {"issue_url": issue_url, "iteration": 0, "error": None},
            config=config,
        )
        return {
            "root_cause":     state.get("root_cause", ""),
            "fix_suggestion": state.get("fix_suggestion", ""),
            "affected_files": state.get("affected_files", []),
            "severity":       state.get("severity", ""),
            "confidence":     state.get("confidence", 0.0),
            "issue_type":     state.get("issue_type", ""),
            "feature_exists": state.get("feature_exists", False),
            "existing_api":   state.get("existing_api", ""),
            "code_changes":   state.get("code_changes", []),
        }
    except Exception as e:
        print(f"  [error] RepoMind 分析失败: {e}")
        return {
            "root_cause": "", "fix_suggestion": "",
            "affected_files": [], "severity": "",
            "confidence": 0.0, "error": str(e),
        }
