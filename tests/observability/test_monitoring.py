import json
import os
from pathlib import Path

import pytest

from minisweagent.observability.demo import demo
from minisweagent.observability.metrics import aggregate, run_metrics
from minisweagent.observability.monitoring import measured, monitor_run, overview
from minisweagent.observability.monitoring_dashboard import duration_chart, elapsed, resource_chart
from minisweagent.observability.schema import Run, Span
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_parser import import_trajectory, parse_trajectory

DATA = Path(__file__).parents[1] / "test_data"
DASHBOARD = Path(__file__).parents[2] / "src/minisweagent/observability/dashboard.py"


def nested_run() -> Run:
    run = Run(
        "run",
        "task",
        source="runtime",
        exit_status="Submitted",
        duration_ms=9000,
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "inspect",
                "extra": {
                    "actions": [{"command": "echo a", "tool_call_id": "a"}, {"command": "echo b", "tool_call_id": "b"}]
                },
            },
            {"role": "tool", "tool_call_id": "b", "extra": {"raw_output": "b", "returncode": 0}},
            {"role": "tool", "tool_call_id": "a", "extra": {"raw_output": "a", "returncode": 0}},
            {
                "role": "assistant",
                "content": "retry",
                "extra": {"actions": [{"command": "echo skip", "tool_call_id": "skip"}]},
            },
            {
                "role": "tool",
                "tool_call_id": "skip",
                "extra": {"returncode": -1, "exception_info": "action was not executed"},
            },
        ],
    )
    run.spans = [
        Span("run", "run", span_id="root", duration_ms=9000),
        Span(
            "run",
            "step",
            1,
            span_id="step-1",
            parent_span_id="root",
            duration_ms=1000,
            attributes={"message_indices": [2, 3, 4]},
        ),
        Span(
            "run",
            "llm",
            1,
            span_id="llm-1",
            parent_span_id="step-1",
            duration_ms=100,
            attributes={"total_tokens": 10, "cost_usd": 0.1},
        ),
        Span(
            "run",
            "llm",
            99,
            span_id="llm-2",
            parent_span_id="llm-1",
            duration_ms=200,
            attributes={"total_tokens": 20, "cost_usd": 0.2},
        ),
        Span(
            "run",
            "tool",
            99,
            parent_span_id="step-1",
            duration_ms=500,
            attributes={"command": "echo a", "tool_call_id": "a", "returncode": 0, "output": "a"},
        ),
        Span(
            "run",
            "tool",
            1,
            parent_span_id="step-1",
            duration_ms=0,
            attributes={"command": "echo b", "tool_call_id": "b", "returncode": 0, "output": "b"},
        ),
        Span(
            "run",
            "step",
            2,
            span_id="step-2",
            parent_span_id="root",
            duration_ms=4000,
            attributes={"message_indices": [5, 6]},
        ),
        Span("run", "llm", 2, parent_span_id="step-2"),
        Span(
            "run",
            "tool",
            2,
            parent_span_id="step-2",
            status="not_executed",
            duration_ms=999999,
            attributes={
                "command": "echo skip",
                "tool_call_id": "skip",
                "returncode": -1,
                "exception_info": "action was not executed",
            },
        ),
    ]
    return run


def test_measurements_preserve_zero_missing_and_invalid_values_and_exact_units():
    total = measured([0, 100, None, -1, float("nan"), float("inf"), "100", True])
    assert (total.value, total.known, total.total, total.complete) == (100, 2, 8, False)
    assert measured([None, 0, 100], average=True).value == 50
    assert measured([None, None]).value is None and measured([]).value is None
    assert elapsed(1250) == "1.250 秒" and elapsed(0.025) == "0.025 毫秒"
    assert elapsed(None) == "未记录" and elapsed(0) == "0 毫秒"


def test_call_tree_uses_parent_links_and_never_sums_run_step_and_child_durations():
    run = nested_run()
    original = run.to_dict()
    monitor = monitor_run(run)
    assert len(monitor.steps) == 2 and [len(view.llm) for view in monitor.steps] == [2, 1]
    assert [len(view.step.actions) for view in monitor.steps] == [2, 1]
    assert monitor.usage.llm_ms.value == 300 and monitor.usage.tool_ms.value == 500
    assert (monitor.usage.llm_ms.known, monitor.usage.llm_ms.total) == (2, 3)
    assert (monitor.usage.tool_ms.known, monitor.usage.tool_ms.total) == (2, 2)
    assert monitor.run.duration_ms == 9000 and [view.step.duration_ms for view in monitor.steps] == [1000, 4000]
    assert monitor.steps[1].step.status == "not_executed" and monitor.steps[1].usage.tool_ms.value is None
    assert monitor.usage.tokens.value == 30 and not monitor.usage.tokens.complete
    assert monitor.usage.cost.value == pytest.approx(0.3) and not monitor.usage.cost.complete
    assert not monitor.orphan_llm and not monitor.orphan_tools and run.to_dict() == original


def test_partial_usage_aggregate_cost_and_recorded_call_count_are_not_confused():
    run = nested_run()
    run.metadata["model_stats"] = {"api_calls": 5, "instance_cost": 1.23}
    monitor = monitor_run(run)
    assert (monitor.usage.tokens.value, monitor.usage.tokens.known, monitor.usage.tokens.total) == (30, 2, 5)
    assert monitor.usage.cost.value == 1.23 and monitor.usage.cost.source == "run" and monitor.usage.cost.complete
    assert monitor.call_cost.value == pytest.approx(0.3) and monitor.call_cost.total == 5
    assert monitor.steps[0].usage.cost.value == pytest.approx(0.3) and monitor.steps[1].usage.cost.value is None
    assert run_metrics(run)["cost_usd"] == 1.23 and not run_metrics(run)["total_tokens_complete"]
    summary = overview([monitor, monitor_run(Run("missing", "task", exit_status="unknown"))])
    assert summary.runs == 2 and summary.tasks == 1 and summary.submitted == 1 and summary.abnormal == 0
    assert summary.average_ms.value == 9000 and summary.average_ms.known == 1
    assert summary.tokens.value == 30 and summary.token_complete_runs == 0 and summary.cost_complete_runs == 1
    assert summary.token_known_calls == 2 and summary.llm_calls == 5 and summary.aggregate_cost_runs == 1
    assert aggregate([run])["cost_complete_runs"] == 1


def test_charts_use_seconds_chinese_axes_and_keep_missing_values_as_gaps():
    monitor = monitor_run(nested_run())
    chart = duration_chart(monitor)
    assert chart.layout.xaxis.title.text == "执行步骤" and chart.layout.yaxis.title.text == "调用累计耗时（秒）"
    assert list(chart.data[0].x) == ["Step 1", "Step 2"]
    assert list(chart.data[0].y) == [0.3, None] and list(chart.data[1].y) == [0.5, None]
    assert list(chart.data[0].customdata[0]) == [2, 2]
    assert resource_chart(monitor, "tokens") is None
    monitor.steps[1].usage.tokens = measured([7])
    monitor.steps[1].usage.cost = measured([0.05])
    assert resource_chart(monitor, "tokens").layout.yaxis.title.text == "Token 数（个）"
    assert list(resource_chart(monitor, "cost").data[0].y) == pytest.approx([0.3, 0.05])


def test_unassigned_calls_are_preserved_and_parent_cycles_do_not_loop():
    run = Run(
        "orphan",
        "task",
        source="runtime",
        spans=[
            Span(
                "orphan", "llm", span_id="cycle", parent_span_id="cycle", duration_ms=10, attributes={"total_tokens": 4}
            ),
            Span("orphan", "tool", duration_ms=20, status="error", attributes={"command": "actual", "returncode": 1}),
        ],
    )
    monitor = monitor_run(run)
    assert not monitor.steps and len(monitor.orphan_llm) == len(monitor.orphan_tools) == 1
    assert monitor.usage.llm_ms.value == 10 and monitor.usage.tool_ms.value == 20 and monitor.usage.tokens.value == 4
    assert duration_chart(monitor) is None and monitor.orphan_tools[0].status == "nonzero_exit"


def test_legacy_response_item_ids_are_normalized_to_call_ids_without_duplicate_tools():
    _, run = parse_trajectory(
        {
            "messages": [
                {
                    "object": "response",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc-id",
                            "call_id": "call-id",
                            "name": "bash",
                            "arguments": '{"command":"echo actual"}',
                        }
                    ],
                },
                {"type": "function_call_output", "call_id": "call-id", "output": '{"returncode":0,"output":"actual"}'},
            ]
        }
    )
    next(span for span in run.spans if span.kind == "tool").attributes["tool_call_id"] = "fc-id"
    monitor = monitor_run(run)
    assert len(monitor.tools) == 1 and not monitor.orphan_tools
    assert monitor.tools[0].call_id == "call-id" and monitor.tools[0].observation.output == "actual"
    assert next(span for span in run.spans if span.kind == "tool").attributes["tool_call_id"] == "fc-id"


@pytest.fixture(scope="module")
def monitoring_store(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("monitoring") / "traces.db")
    demo(store, DATA)
    if source := os.environ.get("MSWEA_TEST_TRAJECTORY"):
        import_trajectory(store, Path(source))
    return store


def test_real_runtime_measurements_and_historical_gaps_are_not_fabricated(monitoring_store):
    monitors = [monitor_run(run) for run in monitoring_store.runs()]
    for monitor in monitors:
        run = monitor.run
        if run.source == "runtime":
            assert monitor.usage.llm_ms.value == pytest.approx(sum(s.duration_ms for s in run.spans if s.kind == "llm"))
            assert monitor.usage.tool_ms.value == pytest.approx(
                sum(s.duration_ms for s in run.spans if s.kind == "tool")
            )
            assert monitor.usage.llm_ms.complete and monitor.usage.tool_ms.complete
            assert monitor.usage.tokens.value is None and monitor.usage.cost.value is None
            assert duration_chart(monitor) is not None and resource_chart(monitor, "tokens") is None
        else:
            assert (
                run.duration_ms is None and monitor.usage.llm_ms.value is None and monitor.usage.tool_ms.value is None
            )
            assert duration_chart(monitor) is None
        assert sum(len(view.llm) for view in monitor.steps) == len([s for s in run.spans if s.kind == "llm"])
        assert sum(len(view.step.actions) for view in monitor.steps) == len([s for s in run.spans if s.kind == "tool"])
        assert not monitor.orphan_llm and not monitor.orphan_tools
    summary = overview(monitors)
    assert summary.average_ms.known == 2
    assert summary.submitted == (3 if os.environ.get("MSWEA_TEST_TRAJECTORY") else 2)
    if os.environ.get("MSWEA_TEST_TRAJECTORY"):
        user = next(m for m in monitors if m.run.model == "openai/gpt-6.1-sol")
        assert user.usage.tokens.value == 50829 and user.usage.cost.value == pytest.approx(0.13161)
        assert (summary.tokens.known, summary.token_complete_runs, summary.runs) == (1, 1, 5)
        assert (summary.token_known_calls, summary.llm_calls) == (9, 29)
        assert user.steps[5].step.actions[0].status == "not_executed" and user.usage.tool_ms.total == 8


def open_monitor(store):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(DASHBOARD), default_timeout=30)
    app.query_params["page"] = "监控"
    app.run()
    next(w for w in app.text_input if w.label == "SQLite 数据库").set_value(str(store.path)).run()
    assert not app.exception
    return app


def test_monitoring_ui_has_human_labels_collapsed_nodes_and_no_database_tables(monitoring_store):
    app = open_monitor(monitoring_store)
    assert not app.dataframe
    assert len(app.json) == len(next(w for w in app.expander if w.label.startswith("高级详情")).get("json")) == 1
    assert all(not w.proto.expanded for w in app.expander)
    ids = [r.run_id for r in monitoring_store.runs()] + [t.task_id for t in monitoring_store.tasks()]
    assert not any(identifier in option for w in app.selectbox for option in w.options for identifier in ids)
    assert not any(text in w.value for w in app.caption for text in ("unknown", "None", "span_id", "trace_id"))
    assert len(app.code) == sum(len(popover.get("code")) for popover in app.get("popover"))
    assert any(w.label == "平均运行耗时" for w in app.metric)
    assert any("完整覆盖" in w.value for w in app.caption)


def test_monitoring_task_run_selection_filtering_and_chart_interaction(monitoring_store):
    app = open_monitor(monitoring_store)
    runtimes = [r for r in reversed(monitoring_store.runs()) if r.source == "runtime"]
    next(w for w in app.selectbox if w.label == "查看任务").set_value(runtimes[0].task_id).run()
    assert not app.exception
    runs_widget = next(w for w in app.selectbox if w.label == "查看运行")
    assert len(runs_widget.options) == 2 and any("第 1 次运行" in option for option in runs_widget.options)
    for run in runtimes:
        next(w for w in app.selectbox if w.label == "查看运行").set_value(run.run_id).run()
        assert not app.exception and app.query_params["run"] in (run.run_id, [run.run_id])
        assert len(app.get("popover")) == 8 and len(app.get("plotly_chart")) == 1
        expected = "通过" if monitoring_store.evaluation(run.run_id).verdict == "passed" else "未通过"
        assert next(w for w in app.metric if w.label == "独立评测结果").value == expected
        assert next(w for w in app.metric if w.label == "Agent 运行状态").value == "已提交"
        assert any("离线脚本演示" in w.value for w in app.info)
        next(w for w in app.radio if w.label == "调用链范围").set_value("非零退出").run()
        assert len(app.get("popover")) == 2 * sum(view.step.status == "nonzero_exit" for view in monitor_run(run).steps)
        next(w for w in app.radio if w.label == "调用链范围").set_value("全部步骤").run()
    historical = next(r for r in monitoring_store.runs() if r.source == "historical")
    next(w for w in app.selectbox if w.label == "查看任务").set_value(historical.task_id).run()
    assert not app.exception and any("不绘制耗时图" in w.value for w in app.caption)
    if historical.model == "openai/gpt-6.1-sol":
        assert len(app.get("plotly_chart")) == 1
        next(w for w in app.radio if w.label == "资源指标").set_value("API 费用").run()
        spec = json.loads(app.get("plotly_chart")[0].proto.spec)
        assert spec["layout"]["yaxis"]["title"]["text"] == "API 费用（美元）"
        next(w for w in app.radio if w.label == "调用链范围").set_value("动作未执行").run()
        assert len(app.get("popover")) == 2 and any("占位值" in w.value for w in app.info)


def test_empty_monitoring_page_and_sidebar_filters(monitoring_store, tmp_path):
    empty = open_monitor(Store(tmp_path / "empty.db"))
    assert not empty.exception and not empty.dataframe and not empty.json and not empty.get("plotly_chart")
    assert next(w for w in empty.metric if w.label == "运行次数").value == "0"
    assert next(w for w in empty.metric if w.label == "已记录 Token").value == "未记录"
    app = open_monitor(monitoring_store)
    task = next(r.task_id for r in monitoring_store.runs() if r.source == "runtime")
    next(w for w in app.sidebar.selectbox if w.label == "筛选任务").set_value(task).run()
    assert not app.exception and next(w for w in app.metric if w.label == "运行次数").value == "2"
    assert next(w for w in app.metric if w.label == "任务数量").value == "1"
