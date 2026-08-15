"""第四阶段补丁确定性审查和结论测试。"""


def test_blocks_test_skip_marker():
    """自动补丁不得通过新增 skip 让 CI 表面变绿。"""
    from tools.patch_review import deterministic_review, review_verdict
    patch = """diff --git a/tests/test_api.py b/tests/test_api.py
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -1 +1,2 @@
+@pytest.mark.skip(reason="flaky")
 def test_api(): pass
"""
    issues = deterministic_review(patch, ["tests/test_api.py"], ["tests/test_api.py"])
    assert any(item["category"] == "test_quality" for item in issues)
    assert review_verdict(issues) == "blocked"


def test_blocks_dangerous_security_change():
    """测试通过也不能接受新增 shell=True 等高风险调用。"""
    from tools.patch_review import deterministic_review
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-run(command)
+subprocess.run(command, shell=True)
"""
    issues = deterministic_review(patch, ["app.py"], ["app.py"])
    assert any(item["category"] == "security" and item["severity"] == "high" for item in issues)


def test_flags_out_of_scope_file_as_warning():
    """越界文件需要提醒人工，但 medium 风险不会像安全问题一样硬阻断。"""
    from tools.patch_review import deterministic_review, review_verdict
    issues = deterministic_review("", ["unrelated.py"], ["app.py"])
    assert issues[0]["category"] == "scope_control"
    assert review_verdict(issues) == "warning"


def test_approves_clean_patch():
    """没有确定性问题时应保持 approved，避免默认制造无意义警告。"""
    from tools.patch_review import deterministic_review, review_verdict
    assert deterministic_review("+value = 2", ["app.py"], ["app.py"]) == []
    assert review_verdict([]) == "approved"
