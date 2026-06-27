"""
Layer 3 — 端到端回归

两类测试：
  quality  — 4 条"答案明确"的 issue，分数必须 >= min_score（pipeline 质量门禁）
  smoke    — 4 条"链路复杂"的 issue，只验证 pipeline 跑通、输出结构合法

运行方式：
    pytest tests/test_regression.py -m regression -v          # 全部
    pytest tests/test_regression.py -m "regression and quality"  # 只跑质量门禁
    pytest tests/test_regression.py -m "regression and smoke"   # 只跑冒烟
"""
import json
import pytest
from pathlib import Path

BASELINES_FILE = Path(__file__).parent / "regression" / "baselines.json"
SMOKE_FILE = Path(__file__).parent / "regression" / "smoke_cases.json"
TOLERANCE = 0.05


def load_baselines() -> list[dict]:
    with open(BASELINES_FILE) as f:
        return json.load(f)


def load_smoke_cases() -> list[dict]:
    with open(SMOKE_FILE) as f:
        return json.load(f)


def _fetch_issue_meta(issue_url: str) -> dict:
    import httpx
    from config import settings
    parts = issue_url.rstrip("/").split("/")
    owner, repo, number = parts[-4], parts[-3], parts[-1]
    headers = {
        "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
        "Accept": "application/vnd.github+json",
    }
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
        headers=headers, timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return {"title": data["title"], "body": (data.get("body") or "")[:1000]}


# ─────────────────────────────────────────────
# 质量门禁（4条，答案明确，LLM 每次都能答对）
# ─────────────────────────────────────────────

@pytest.mark.regression
@pytest.mark.quality
@pytest.mark.parametrize(
    "baseline",
    load_baselines(),
    ids=lambda b: f"{b['owner']}/{b['repo']}#{b['issue_number']}",
)
def test_quality_score(baseline):
    """分数必须 >= min_score - TOLERANCE，否则视为质量退化。"""
    from eval.runner import _run_repomind
    from eval.judge import judge_single
    from eval.dataset import _fetch_pr_info

    issue_url = baseline["issue_url"]
    min_score = baseline["min_score"] - TOLERANCE

    issue_meta = _fetch_issue_meta(issue_url)
    pr_info = _fetch_pr_info(baseline["pr_url"])
    assert pr_info, f"无法拉取 PR 信息: {baseline['pr_url']}"

    ai_result = _run_repomind(issue_url)
    assert ai_result.get("root_cause"), f"AI 分析返回空: {issue_url}"

    scores = judge_single(
        title=issue_meta["title"],
        body=issue_meta["body"],
        ai_result=ai_result,
        ground_truth=pr_info,
        n=2,
    )
    overall = scores.get("overall", 0)

    print(f"\n  {baseline['owner']}/{baseline['repo']}#{baseline['issue_number']}")
    print(f"  overall={overall:.3f}  (min={min_score:.3f}, expected={baseline['expected_score']:.3f})")
    print(f"  root={scores.get('root_cause_accuracy',0):.2f} "
          f"file={scores.get('file_accuracy',0):.2f} "
          f"sev={scores.get('severity_fit',0):.2f} "
          f"fix={scores.get('fix_actionability',0):.2f}")

    assert overall >= min_score, (
        f"质量退化：{baseline['owner']}/{baseline['repo']}#{baseline['issue_number']} "
        f"得分 {overall:.3f} 低于基准 {min_score:.3f}  "
        f"(历史期望 {baseline['expected_score']:.3f})\n"
        f"reasoning: {scores.get('reasoning', '')}"
    )


# ─────────────────────────────────────────────
# 结构冒烟测试（4条，链路复杂，只验完整性）
# ─────────────────────────────────────────────

@pytest.mark.regression
@pytest.mark.smoke
@pytest.mark.parametrize(
    "case",
    load_smoke_cases(),
    ids=lambda c: f"{c['owner']}/{c['repo']}#{c['issue_number']}",
)
def test_smoke_pipeline(case):
    """验证 pipeline 跑通、输出字段合法，不卡分数。"""
    from eval.runner import _run_repomind

    ai_result = _run_repomind(case["issue_url"])

    assert ai_result is not None, "pipeline 返回 None"
    assert ai_result.get("root_cause"), "root_cause 为空"
    assert ai_result.get("fix_suggestion"), "fix_suggestion 为空"
    assert isinstance(ai_result.get("affected_files", []), list), "affected_files 不是列表"
    assert ai_result.get("severity") in ("critical", "high", "medium", "low"), \
        f"severity 非法值: {ai_result.get('severity')}"
    assert ai_result.get("confidence", 0) > 0, "confidence 为 0"

    print(f"\n  {case['owner']}/{case['repo']}#{case['issue_number']}")
    print(f"  confidence={ai_result.get('confidence',0):.2f}  "
          f"severity={ai_result.get('severity')}  "
          f"files={ai_result.get('affected_files', [])}")


# ─────────────────────────────────────────────
# 基准文件完整性（快速，不需要 LLM）
# ─────────────────────────────────────────────

class TestBaselineFile:
    def test_baselines_file_exists(self):
        assert BASELINES_FILE.exists()

    def test_smoke_file_exists(self):
        assert SMOKE_FILE.exists()

    def test_baselines_not_empty(self):
        assert len(load_baselines()) >= 3

    def test_smoke_cases_not_empty(self):
        assert len(load_smoke_cases()) >= 3

    def test_each_baseline_has_required_fields(self):
        required = {"issue_url", "pr_url", "issue_number", "owner", "repo",
                    "min_score", "expected_score"}
        for b in load_baselines():
            assert not (required - set(b.keys())), f"基准缺字段: {b}"

    def test_each_smoke_case_has_required_fields(self):
        required = {"issue_url", "issue_number", "owner", "repo"}
        for c in load_smoke_cases():
            assert not (required - set(c.keys())), f"smoke case 缺字段: {c}"

    def test_min_score_less_than_expected(self):
        for b in load_baselines():
            assert b["min_score"] < b["expected_score"]

    def test_scores_in_valid_range(self):
        for b in load_baselines():
            assert 0.0 <= b["min_score"] <= 1.0
            assert 0.0 <= b["expected_score"] <= 1.0

    def test_pr_url_is_pull_request(self):
        for b in load_baselines():
            assert "/pull/" in b["pr_url"]
