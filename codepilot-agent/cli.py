"""CodePilot Agent 命令行入口。

用法示例：

    codepilot review path/to/file.py --mock
    codepilot review src/ --format md --report-dir ./reports
    codepilot review app.py --model claude-sonnet-5 --max-concurrency 4

退出码约定（GitHub Action 依赖这个约定来阻塞合并）：
    0 —— 审查通过，或只有中低危问题。
    1 —— 存在高危问题未通过（阻塞合并）。
    2 —— 执行失败（配置错误、API 故障、预算耗尽）。
"""
from __future__ import annotations

import argparse
import os
import sys

# 允许 `python cli.py` 直接运行时找到包内模块。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.chunker import build_related_files, chunk_source, estimate_tokens  # noqa: E402
from core.errors import CodePilotError, ConfigError  # noqa: E402
from core.schemas import ProjectContext, RunStatus, WorkflowState  # noqa: E402
from core.workflow import WorkflowRunner  # noqa: E402
from llm.client import DEFAULT_MODEL, build_client  # noqa: E402
from report.renderer import render_cli_summary, render_json, render_markdown  # noqa: E402

EXIT_PASSED = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2

PYTHON_SUFFIXES = (".py", ".pyi", ".pyw")


def load_standards(path):
    """从文本或 YAML 读团队编码规范。

    YAML 是列表时取列表；是字典时取 ``standards`` / ``rules`` 键。
    解析失败就直接报错，不静默降级 —— 规范缺失会显著改变审查结果，用户必须知道。
    """
    if not path:
        return []
    if not os.path.isfile(path):
        raise ConfigError("规范文件不存在：{}".format(path))

    with open(path, "r", encoding="utf-8") as handle:
        content = handle.read()

    if path.lower().endswith((".yaml", ".yml")):
        import yaml

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ConfigError("规范文件 YAML 解析失败：{}".format(exc))

        if isinstance(data, list):
            return [str(item) for item in data]
        if isinstance(data, dict):
            for key in ("standards", "rules", "internal_coding_standards"):
                if key in data and isinstance(data[key], list):
                    return [str(item) for item in data[key]]
        return []

    return [line.strip() for line in content.split("\n") if line.strip()]


def detect_dependency_versions(target_path):
    """从邻近的 requirements.txt / package.json 抓依赖版本，注入 ProjectContext。"""
    versions = {}
    search_dirs = []
    if os.path.isfile(target_path):
        search_dirs.append(os.path.dirname(os.path.abspath(target_path)))
    else:
        search_dirs.append(os.path.abspath(target_path))

    for directory in search_dirs:
        requirements = os.path.join(directory, "requirements.txt")
        if os.path.isfile(requirements):
            with open(requirements, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "==" not in line:
                        continue
                    name, _, version = line.partition("==")
                    versions[name.strip()] = version.strip()

        package_json = os.path.join(directory, "package.json")
        if os.path.isfile(package_json):
            import json

            try:
                with open(package_json, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                for section in ("dependencies", "devDependencies"):
                    for name, version in (data.get(section) or {}).items():
                        versions[name] = version
            except (ValueError, OSError):
                pass

    return versions


def collect_targets(target):
    """把目标路径展开成待审查的 Python 文件列表。目录会被递归扫描。"""
    if os.path.isfile(target):
        return [target]
    if os.path.isdir(target):
        found = []
        for root, dirs, files in os.walk(target):
            # 跳过噪声目录。
            dirs[:] = [
                d for d in dirs
                if d not in ("__pycache__", ".git", ".venv", "venv", "node_modules", ".codepilot")
            ]
            for name in files:
                if name.endswith(PYTHON_SUFFIXES):
                    found.append(os.path.join(root, name))
        return sorted(found)
    raise ConfigError("目标路径不存在：{}".format(target))


def build_context(args, target_path, source):
    known_siblings = set()
    directory = os.path.dirname(os.path.abspath(target_path)) if os.path.isfile(target_path) \
        else os.path.abspath(target_path)
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            if name.endswith(PYTHON_SUFFIXES):
                known_siblings.add(name[:-3])

    return ProjectContext(
        project_name=args.project_name or os.path.basename(os.path.dirname(os.path.abspath(target_path))) or "project",
        dependency_versions=detect_dependency_versions(target_path),
        internal_coding_standards=load_standards(args.standards),
        related_files=build_related_files(source, known_siblings),
        root_path=directory,
        primary_language="python",
    )


def review_one(args, target_path, client_factory):
    """审查单个文件，返回 (report, rendered_markdown)。"""
    with open(target_path, "r", encoding="utf-8") as handle:
        source = handle.read()

    if not source.strip():
        print("跳过空文件：{}".format(target_path))
        return None, None

    context = build_context(args, target_path, source)

    if args.verbose:
        print("文件     : {}".format(target_path))
        print("估算token: {}".format(estimate_tokens(source)))

    chunks = chunk_source(source, char_budget=args.char_budget)
    if len(chunks) > 1 and args.verbose:
        print("已切分为 {} 个 chunk（超长文件降级路径）".format(len(chunks)))

    # 多 chunk 时逐个审查，最后把各 chunk 的结论归并成一份报告。
    # 这样做牺牲了一点跨 chunk 的关联能力，换来的是大文件不爆 context。
    #
    # 客户端在每个文件内复用（真实调用复用连接，Mock 复用响应游标）：
    # 每个 chunk 新建客户端会让 Mock 的游标复位、用量统计归零。
    client = client_factory(args)

    chunk_reports = []
    for chunk in chunks:
        runner = WorkflowRunner(
            client=client,
            project_context=context,
            workdir=os.path.dirname(os.path.abspath(target_path)) or os.getcwd(),
            max_concurrency=args.max_concurrency,
            llm_budget=args.llm_budget,
            use_git_check=not args.no_git_check,
            logger=(lambda m: print("  " + m)) if args.verbose else None,
        )
        report = runner.run(
            chunk.text if len(chunks) > 1 else source,
            file_path=os.path.basename(target_path),
            resume=args.resume,
        )
        if len(chunks) == 1:
            return report, render_markdown(report, detail=args.detail)
        chunk_reports.append(report)

    # 多 chunk 汇总：逐个 chunk 的结论归并成一份报告。
    # chunk 的文本自带模块头，行号仍是相对原文件的行号（chunker 用 ast 的 lineno），
    # 所以这里不需要做行号偏移修正。
    report = merge_chunk_reports(chunk_reports)
    return report, render_markdown(report, detail=args.detail)


def merge_chunk_reports(chunk_reports):
    """把同一文件多个 chunk 的独立报告归并成一份。

    只把 ``issues`` 拼起来是不够的 —— ``status`` / 未定位 / 重试耗尽 / 修复尝试
    各自散落在每份 chunk 报告里，直接返回最后一份（曾经的实现）会让前面 chunk 的
    结论被静默丢掉：一个「第 1 块有个未修复的 P0、第 2 块干净」的文件会被判为
    **通过**、退出码 0、``blocking.txt`` 写成 false，高危问题失去阻塞能力。

    所以这里除了合并 issue 列表，还要：
    - 合并定位结果、修复方案、修复尝试与四类「未通过」id 集合；
    - 累加已应用补丁数，LLM 调用取最大值（同一个客户端，最后一次的计数才是总数）；
    - **按合并后的阻塞项重算 status** —— 阻塞定义只有 ``blocking_escalation`` 一处，
      不在这里另写一套判断。
    """
    if not chunk_reports:
        raise ConfigError("没有可合并的 chunk 报告")

    merged = chunk_reports[0]

    for part in chunk_reports[1:]:
        merged.issues = merged.issues + list(part.issues)
        merged.located_issues = merged.located_issues + list(part.located_issues)
        merged.fixes = merged.fixes + list(part.fixes)
        merged.attempts = merged.attempts + list(part.attempts)
        for field in ("unresolved_issue_ids", "escalated_issue_ids",
                      "cross_file_issue_ids", "regression_issue_ids"):
            setattr(merged, field, sorted(set(getattr(merged, field))
                                          | set(getattr(part, field))))
        merged.applied_patch_count += part.applied_patch_count
        merged.llm_calls = max(merged.llm_calls, part.llm_calls)
        if part.notes:
            merged.notes = (merged.notes + "\n" + part.notes).strip()

    if merged.blocking_escalation:
        merged.status = RunStatus.BLOCKED
        merged.workflow_state = WorkflowState.ESCALATED
    elif any(part.workflow_state == WorkflowState.FAILED for part in chunk_reports):
        merged.workflow_state = WorkflowState.FAILED
    else:
        merged.status = RunStatus.PASSED

    return merged


def build_parser():
    parser = argparse.ArgumentParser(
        prog="codepilot",
        description="CodePilot Agent —— 多 Agent 协作的 AI 代码审查助手",
    )
    subparsers = parser.add_subparsers(dest="command")

    review = subparsers.add_parser("review", help="审查代码文件或目录")
    review.add_argument("target", help="待审查的文件或目录路径")
    review.add_argument("--mock", action="store_true",
                        help="使用离线 Mock，不调用真实 API（CI 与本地验证用）")
    review.add_argument("--fixtures", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fixtures", "mock_responses"),
        help="Mock 模式的 fixture 目录")
    review.add_argument("--model", default=DEFAULT_MODEL, help="使用的模型")
    review.add_argument("--standards", default=None,
                        help="团队编码规范文件（.txt 每行一条，或 .yaml）")
    review.add_argument("--project-name", default=None, help="项目名，写入报告")
    review.add_argument("--format", choices=("md", "json", "summary"), default="summary",
                        help="输出格式")
    review.add_argument("--detail", choices=("full", "comment"), default="full",
                        help="Markdown 详细程度：comment 省略 JSON 附录，适合 PR 评论")
    review.add_argument("--output", default=None, help="把输出写入指定文件")
    review.add_argument("--report-dir", default=None,
                        help="输出目录：写入 report.md 与 report.json（供 Artifact 归档）")
    review.add_argument("--max-concurrency", type=int, default=4, help="局部并发度")
    review.add_argument("--char-budget", type=int, default=24000,
                        help="单次审查的字符预算，超出则按 AST 切块")
    review.add_argument("--llm-budget", type=int, default=200,
                        help="LLM 调用次数上限，防止闭环失控")
    review.add_argument("--no-git-check", action="store_true",
                        help="跳过 git apply --check 权威校验，只用内存应用器")
    review.add_argument("--resume", action="store_true", help="从 .codepilot/state.json 恢复")
    review.add_argument("--verbose", action="store_true", help="打印过程细节")
    review.add_argument("--api-key", default=None, help="覆盖 ANTHROPIC_API_KEY")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "review":
        parser.print_help()
        return EXIT_ERROR

    def client_factory(_args):
        return build_client(
            mock=args.mock,
            fixture_dir=args.fixtures,
            model=args.model,
            api_key=args.api_key,
        )

    try:
        targets = collect_targets(args.target)
    except ConfigError as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return EXIT_ERROR

    if not targets:
        print("未找到可审查的 Python 文件：{}".format(args.target), file=sys.stderr)
        return EXIT_ERROR

    if not args.mock:
        # 真实模式只建一次客户端，复用连接与用量统计。
        try:
            shared_client = client_factory(args)
        except ConfigError as exc:
            print("错误：{}".format(exc), file=sys.stderr)
            return EXIT_ERROR
        client_factory = lambda _args: shared_client  # noqa: E731

    reports = []
    markdown_outputs = []
    exit_code = EXIT_PASSED

    for target in targets:
        try:
            report, markdown = review_one(args, target, client_factory)
        except CodePilotError as exc:
            print("审查失败（{}）：{}".format(target, exc), file=sys.stderr)
            exit_code = EXIT_ERROR
            continue
        except OSError as exc:
            print("读取文件失败（{}）：{}".format(target, exc), file=sys.stderr)
            exit_code = EXIT_ERROR
            continue

        if report is None:
            continue

        reports.append(report)
        if markdown:
            markdown_outputs.append(markdown)

        if report.status == RunStatus.BLOCKED:
            exit_code = EXIT_BLOCKED

    if not reports:
        return exit_code if exit_code != EXIT_PASSED else EXIT_ERROR

    output = _render_output(args, reports, markdown_outputs)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output)
        print("已写入：{}".format(args.output))
    else:
        print(output)

    if args.report_dir:
        _write_report_dir(args, reports, markdown_outputs)

    return exit_code


def _render_output(args, reports, markdown_outputs):
    if args.format == "json":
        if len(reports) == 1:
            return render_json(reports[0])
        return "[\n" + ",\n".join(render_json(item) for item in reports) + "\n]"

    if args.format == "md":
        return "\n\n---\n\n".join(markdown_outputs)

    # summary：每个文件一段紧凑摘要。
    blocks = []
    for report in reports:
        blocks.append("\n".join(["=" * 60] + render_cli_summary(report)))
    return "\n\n".join(blocks)


def _write_report_dir(args, reports, markdown_outputs):
    """写 report.md 与 report.json。GitHub Action 用这两个文件归档与发评论。"""
    directory = args.report_dir
    if not os.path.isdir(directory):
        os.makedirs(directory)

    md_path = os.path.join(directory, "report.md")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write("\n\n---\n\n".join(markdown_outputs))

    json_path = os.path.join(directory, "report.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        if len(reports) == 1:
            handle.write(render_json(reports[0]))
        else:
            handle.write("[\n" + ",\n".join(render_json(item) for item in reports) + "\n]")

    # 给 GitHub Action 用的退出码标记：blocking=true 表示必须阻塞合并。
    blocking = any(report.status == RunStatus.BLOCKED for report in reports)
    marker_path = os.path.join(directory, "blocking.txt")
    with open(marker_path, "w", encoding="utf-8") as handle:
        handle.write("true" if blocking else "false")

    print("报告已写入：{}".format(md_path))
    print("原始 JSON：{}".format(json_path))


if __name__ == "__main__":
    sys.exit(main())
