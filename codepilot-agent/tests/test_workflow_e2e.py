"""端到端测试：跑通「审查 → 定位 → 修复 → 回归 → 报告 → 退出码」整条链路。

``fixtures/sample_project/user_service.py`` 的 docstring 指名了这个文件：样例里每处
埋雷都用 ``# [SEED:n]`` 标记，期望与这里的断言一一对应。但在此之前这个文件并不存在 ——
样例与 ``fixtures/mock_responses`` 只被手工敲命令行消费过，没有任何自动化测试碰它们。

为什么必须有这一层：其它测试各自只覆盖一个模块（锚点匹配、补丁解析、AST 切块、
状态机），「这些模块串起来到底成不成立」没有任何测试回答。而且 mock 响应里的 patch 是
从样例源码派生出来的（``tools/build_mock_fixtures.py``），样例一改动、patch 不再匹配，
在此之前也不会有任何测试变红。

这一层刻意用**真实 CLI 入口**（``cli.main``）跑，而不是直接调 ``WorkflowRunner``：
CI 门禁依赖的是退出码契约，而契约只在这一层才成立。
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli  # noqa: E402
from core.anchors import locate_issues  # noqa: E402
from core.patch import verify_and_apply  # noqa: E402
from core.schemas import Issue, RunStatus  # noqa: E402
from tools import build_mock_fixtures  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(PROJECT_ROOT, "fixtures", "sample_project")
SAMPLE_NAME = "user_service.py"
FIXTURES_DIR = os.path.join(PROJECT_ROOT, "fixtures", "mock_responses")

# 样例里的 7 处埋雷与 mock 审查响应一一对应。
# （样例还标了 ``# [SEED:8]`` 的 N+1 查询，但 mock 响应没有对应条目 —— 见文末
#  ``TestMockFixtureCoverage`` 对这条边界的显式记录。）
SEED_LINE_SPANS = {
    "seed-1-hardcoded-credential": 1,   # 锚点唯一，单行
    "seed-2-sql-injection": 2,          # 锚点跨两行，验证多行定位与 patch
    "seed-3-none-deref": 1,
    "seed-4-nested-loop": 3,
    "seed-5-weak-hash": 1,
    "seed-6-naming": 1,
    "seed-7-swallowed-exception": 4,
}
SEED_IDS = sorted(SEED_LINE_SPANS)

# 两个 P0 的补丁打不上（mock 响应里的 patch 作用于**原始**源码，第 2 次重试时
# 源码已被同一批 patch 改过，因此 git apply --check 必然拒绝）→ 重试耗尽 → 阻塞合并。
BLOCKING_SEEDS = ["seed-1-hardcoded-credential", "seed-2-sql-injection"]


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def sample_source():
    with open(os.path.join(SAMPLE_DIR, SAMPLE_NAME), "r", encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    """把样例复制到临时目录跑一次完整审查，返回退出码与三个产物。

    复制而不是原地跑：流程会把状态快照写到目标文件旁边的 ``.codepilot/``，
    测试不该往仓库的 fixtures 里写东西。
    """
    workdir = tmp_path_factory.mktemp("e2e")
    target = workdir / SAMPLE_NAME
    shutil.copyfile(os.path.join(SAMPLE_DIR, SAMPLE_NAME), str(target))
    report_dir = workdir / "reports"

    exit_code = cli.main([
        "review", str(target),
        "--mock",
        "--fixtures", FIXTURES_DIR,
        "--format", "md",
        "--detail", "comment",
        "--report-dir", str(report_dir),
    ])

    return {
        "exit_code": exit_code,
        "report": read_json(str(report_dir / "report.json")),
        "markdown": (report_dir / "report.md").read_text(encoding="utf-8"),
        "blocking": (report_dir / "blocking.txt").read_text(encoding="utf-8").strip(),
        "source": target.read_text(encoding="utf-8"),
    }


def by_id(items):
    return {item["issue_id"]: item for item in items}


class TestEndToEndVerdict:
    def test_high_risk_issues_block_merge(self, e2e):
        """两个 P0 没修好 → 退出码 1，且 blocking.txt 必须为 true。"""
        assert e2e["exit_code"] == cli.EXIT_BLOCKED == 1
        assert e2e["blocking"] == "true"

    def test_report_status_matches_exit_code(self, e2e):
        """报告结论与退出码不能各说各话 —— 它们曾经真的不一致过。"""
        assert e2e["report"]["status"] == RunStatus.BLOCKED.value
        assert e2e["report"]["workflow_state"] == "ESCALATED"
        assert "⛔ **阻塞合并**" in e2e["markdown"]

    def test_every_seeded_defect_is_reported(self, e2e):
        assert sorted(by_id(e2e["report"]["issues"])) == SEED_IDS

    def test_nothing_is_left_unlocated(self, e2e):
        """样例的锚点都是有意设计成可精确匹配的，不该出现 unresolved。"""
        assert e2e["report"]["unresolved_issue_ids"] == []
        located = by_id(e2e["report"]["located_issues"])
        assert sorted(located) == SEED_IDS
        for item in located.values():
            assert item["match_status"] == "exact"
            assert item["confidence"] == 1.0
            assert item["line_start"] >= 1


class TestDerivedLineNumbersAreTruthful:
    """整个工具最硬的承诺：行号是**派生**的，不是模型编的。

    这里不比对写死的行号（样例一改就会失效），而是拿报告给出的行号区间回原文取文字，
    验证锚点确实落在那几行里 —— 这正是「绝不编造行号」的可执行定义。
    """

    def test_reported_lines_contain_the_anchor(self, e2e):
        source_lines = e2e["source"].split("\n")
        located = by_id(e2e["report"]["located_issues"])

        for issue in e2e["report"]["issues"]:
            item = located[issue["issue_id"]]
            start, end = item["line_start"], item["line_end"]
            actual = "\n".join(source_lines[start - 1:end])
            assert issue["code_anchor"] in actual, (
                "{} 报告的行号 {}-{} 与锚点对不上".format(issue["issue_id"], start, end)
            )

    def test_multi_line_anchors_span_the_declared_number_of_lines(self, e2e):
        """样例注释里写明了哪些锚点跨行（SEED:2 跨两行、SEED:4 跨三行…）。"""
        located = by_id(e2e["report"]["located_issues"])
        for issue_id, expected_span in SEED_LINE_SPANS.items():
            item = located[issue_id]
            span = item["line_end"] - item["line_start"] + 1
            assert span == expected_span, "{} 的行数区间是 {}".format(issue_id, span)

    def test_control_group_is_not_reported(self, e2e):
        """样例里的 ``safe_get_user_by_name`` 是参数化查询的正确写法，作对照组用。"""
        assert "safe_get_user_by_name" in e2e["source"]
        for issue in e2e["report"]["issues"]:
            assert "safe_get_user_by_name" not in issue["code_anchor"]


class TestSeedExpectations:
    def test_naming_seed_is_handed_to_a_human_not_guessed(self, e2e):
        """SEED:6 重命名会波及调用方 → 必须标记跨文件，而不是猜一个补丁出来。"""
        assert e2e["report"]["cross_file_issue_ids"] == ["seed-6-naming"]
        attempt = by_id(e2e["report"]["attempts"])["seed-6-naming"]
        assert attempt["status"] == "skipped_cross_file"
        assert "人工" in attempt["failure_reason"]

    def test_p0_seeds_exhaust_retries_and_are_escalated(self, e2e):
        """P0 配额 3 次：两次都失败 → 升级；且每次重试都留了轨迹。"""
        assert e2e["report"]["escalated_issue_ids"] == BLOCKING_SEEDS

        attempts = by_id(e2e["report"]["attempts"])
        for issue_id in BLOCKING_SEEDS:
            attempt = attempts[issue_id]
            assert attempt["status"] == "escalated"
            assert attempt["attempts"] == attempt["max_attempts"] == 3
            assert len(attempt["retry_trace"]) == 2

    def test_no_patch_introduced_a_new_issue(self, e2e):
        assert e2e["report"]["regression_issue_ids"] == []

    def test_early_stop_after_two_consecutive_security_failures(self, e2e):
        """seed-1、seed-2 都是 Security 分类，连续两次失败即早停。

        其余 seed 拿到了补丁方案（``fixes`` 有 7 条）却没有进入回归阶段 ——
        这是有意的：同分类连续失败意味着模型在该维度上系统性无能，
        继续重试只是烧 token。
        """
        assert sorted(by_id(e2e["report"]["attempts"])) == sorted(
            BLOCKING_SEEDS + ["seed-6-naming"]
        )
        assert len(e2e["report"]["fixes"]) == 7
        assert e2e["report"]["applied_patch_count"] == 2

    def test_early_stop_keeps_the_llm_budget_small(self, e2e):
        """7 个问题若全部走完闭环，调用数会远高于此。"""
        assert e2e["report"]["llm_calls"] == 8


class TestMockFixturesStayInSyncWithTheSample:
    """mock 响应是从样例源码派生的（``tools/build_mock_fixtures.py``），样例一改就可能失配。

    下面三道守卫的**敏感度不一样**（实测过，别把它们当成等价的三道）：

    ==========================  ==========  ============  ==========
    改动                        可再生性    patch 可应用   锚点可定位
    ==========================  ==========  ============  ==========
    顶部插入一行注释            会红        不会红        不会红
    被审代码改内容              会红        会红          会红
    被审代码改缩进              会红        会红          不会红
    ==========================  ==========  ============  ==========

    关键点：``git apply`` 对行号位移是容忍的（它会自己找上下文），所以「patch 还能
    打上」不足以说明夹具没漂移；而缩进变化会被锚点的归一化匹配层吸收，所以锚点测试
    也拦不住。真正兜底的是可再生性那一道。
    """

    def test_review_fixture_anchors_all_resolve_against_the_sample(self, sample_source):
        payload = read_json(os.path.join(FIXTURES_DIR, "submit_review.json"))
        issues = [Issue(**item) for item in payload[0]["issues"]]

        located = locate_issues(issues, sample_source, SAMPLE_NAME)

        assert len(located) == len(issues)
        for item in located:
            assert item.is_resolved, "{} 的锚点在样例里找不到了".format(item.issue_id)

    def test_fix_fixture_patches_apply_to_the_sample(self, sample_source):
        payload = read_json(os.path.join(FIXTURES_DIR, "submit_fixes.json"))

        applied = 0
        for fix in payload[0]["fixes"]:
            if fix["requires_cross_file"]:
                # 跨文件改动必须显式标记，且不该带补丁
                assert fix["patch"] == ""
                continue
            new_code, info = verify_and_apply(sample_source, fix["patch"], SAMPLE_NAME)
            assert info["hunks_applied"] >= 1
            assert new_code != sample_source
            applied += 1

        assert applied == 6

    def test_fixes_fixture_is_reproducible_from_the_sample(self, sample_source):
        """夹具应当等于用 difflib 从当前样例重新派生的结果。

        这是最强的一道漂移守卫，理由是它连**纯行号位移**都拦得住 ——
        ``git apply`` 会自己找上下文，对位移是容忍的，所以「patch 还能打上」
        发现不了「样例被插了几行」这种改动，而 hunk 头里的行号会变，重新派生
        的结果就对不上仓库里的夹具了。

        反过来，夹具之所以必须可再生：手写 unified diff 的 ``@@`` 计数极易出错，
        而错误的计数会被 git 以 ``corrupt patch`` 拒绝，把「夹具写错」伪装成
        「工具链有问题」。
        """
        for _, old, _ in build_mock_fixtures.SPECS:
            occurrences = sample_source.count(old)
            assert occurrences == 1, (
                "样例里这个锚点已改动（出现 {n} 次）：{snippet!r}；"
                "改了样例就要重新跑 tools/build_mock_fixtures.py 并更新 mock 响应".format(
                    n=occurrences, snippet=old[:60]
                )
            )

        expected = [
            build_mock_fixtures.derive_patch(sample_source, old, new, SAMPLE_NAME)
            for _, old, new in build_mock_fixtures.SPECS
        ]

        payload = read_json(os.path.join(FIXTURES_DIR, "submit_fixes.json"))
        actual = [fix["patch"] for fix in payload[0]["fixes"]
                  if not fix["requires_cross_file"]]

        assert actual == expected


class TestMockFixtureCoverage:
    def test_every_mock_issue_id_has_a_seed_marker_in_the_sample(self, sample_source):
        """mock 响应里的每条问题都要能在样例里找到 ``# [SEED:n]`` 对应说明。"""
        payload = read_json(os.path.join(FIXTURES_DIR, "submit_review.json"))
        markers = [line for line in sample_source.split("\n") if "# [SEED:" in line]

        assert len(payload[0]["issues"]) <= len(markers)

        for issue in payload[0]["issues"]:
            number = issue["issue_id"].split("-")[1]
            assert any("# [SEED:{}]".format(number) in line for line in markers), (
                "{} 在样例里没有对应的 SEED 标记".format(issue["issue_id"])
            )

    def test_sample_seed_8_is_deliberately_uncovered(self, sample_source):
        """样例标了 8 处雷，mock 响应只覆盖 7 处。

        这条边界显式记下来：SEED:8（N+1 查询）没有对应 mock 条目，所以端到端
        断言的是 7 条问题。哪天补上第 8 条响应，这条测试会提醒同步更新上面的预期。
        """
        assert "# [SEED:8]" in sample_source

        payload = read_json(os.path.join(FIXTURES_DIR, "submit_review.json"))
        assert len(payload[0]["issues"]) == 7


class TestExitCodeContract:
    """CI 的 GitHub Action 完全建立在这三个退出码上，所以它们也是项目级契约。"""

    def _args(self, target, fixtures_dir, extra=None):
        return ["review", target, "--mock", "--fixtures", fixtures_dir] + (extra or [])

    def test_clean_project_exits_zero(self, tmp_path):
        target = tmp_path / "clean.py"
        target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        fixtures = tmp_path / "fx"
        fixtures.mkdir()
        (fixtures / "submit_review.json").write_text(
            json.dumps({"issues": []}), encoding="utf-8"
        )
        (fixtures / "submit_locations.json").write_text(
            json.dumps({"located_issues": []}), encoding="utf-8"
        )
        (fixtures / "submit_fixes.json").write_text(
            json.dumps({"fixes": []}), encoding="utf-8"
        )

        assert cli.main(self._args(str(target), str(fixtures))) == cli.EXIT_PASSED == 0

    def test_missing_target_exits_two(self, tmp_path):
        code = cli.main(self._args(str(tmp_path / "nope.py"), FIXTURES_DIR))
        assert code == cli.EXIT_ERROR == 2

    def test_no_subcommand_exits_two(self):
        assert cli.main([]) == cli.EXIT_ERROR == 2
