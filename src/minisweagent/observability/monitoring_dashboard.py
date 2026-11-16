"""Human-readable M2 overview and expandable Run → Step → call tree."""

from dataclasses import asdict
from urllib.parse import urlencode

import plotly.graph_objects as go
import streamlit as st

from minisweagent.observability.execution_status import STATUS
from minisweagent.observability.experiments import compare, export_rows
from minisweagent.observability.monitoring import Measurement, RunMonitor, monitor_run, number, overview, run_state
from minisweagent.observability.schema import Run, Span, Task
from minisweagent.observability.storage import Store
from minisweagent.observability.trace_dashboard import VERDICT, compact_layout, recorded
from minisweagent.observability.trajectory_steps import Action


def elapsed(value: float | None) -> str:
    if number(value) is None:
        return "未记录"
    return f"{value / 1000:.3f} 秒" if value >= 1000 else f"{value:.4g} 毫秒"


def amount(value: int | float | None, *, money: bool = False) -> str:
    if value is None:
        return "未记录"
    return f"${value:.6f}" if money else f"{value:,.0f}"


def coverage(metric: Measurement, subject: str = "次 LLM 调用") -> str:
    return f"已记录 {metric.known} / {metric.total} {subject}"


def task_name(task: Task) -> str:
    return " ".join(task.title.split())[:75] or "未命名任务"


def trace_link(run_id: str, step: int = 0) -> str:
    return "?" + urlencode({"page": "轨迹", "run": run_id, "step": step})


def chart_layout(figure: go.Figure, title: str, ylabel: str) -> go.Figure:
    figure.update_layout(
        title=title,
        xaxis_title="执行步骤",
        yaxis_title=ylabel,
        template="plotly_white",
        height=300,
        margin=dict(l=20, r=15, t=55, b=35),
        legend=dict(title="调用类型", orientation="h", y=1.15),
        barmode="stack",
    )
    figure.update_xaxes(type="category")
    figure.update_yaxes(rangemode="tozero", tickformat=".4~g")
    return figure


def duration_chart(monitor: RunMonitor) -> go.Figure | None:
    if (
        sum(view.usage.llm_ms.value is not None for view in monitor.steps)
        + sum(view.usage.tool_ms.value is not None for view in monitor.steps)
        < 2
    ):
        return None
    figure = go.Figure()
    for field, label, color in (("llm_ms", "LLM", "#4c78a8"), ("tool_ms", "Tool", "#72b7b2")):
        values = [getattr(view.usage, field) for view in monitor.steps]
        if not any(value.value is not None for value in values):
            continue
        figure.add_bar(
            name=label,
            marker_color=color,
            x=[f"Step {view.step.index}" for view in monitor.steps],
            y=[value.value / 1000 if value.value is not None else None for value in values],
            customdata=[[value.known, value.total] for value in values],
            hovertemplate="%{x}<br>累计耗时：%{y:.6f} 秒<br>覆盖 %{customdata[0]} / %{customdata[1]} 次调用<extra>%{fullData.name}</extra>",
        )
    return chart_layout(figure, "各 Step 的已记录调用耗时", "调用累计耗时（秒）")


def resource_chart(monitor: RunMonitor, field: str) -> go.Figure | None:
    values = [getattr(view.usage, field) for view in monitor.steps]
    if sum(value.value is not None for value in values) < 2:
        return None
    money = field == "cost"
    figure = go.Figure(
        go.Bar(
            name="API 费用" if money else "Token",
            x=[f"Step {view.step.index}" for view in monitor.steps],
            y=[value.value for value in values],
            marker_color="#4c78a8",
            customdata=[[value.known, value.total] for value in values],
            hovertemplate="%{x}<br>"
            + ("费用：$%{y:.6f}" if money else "Token：%{y:,.0f}")
            + "<br>覆盖 %{customdata[0]} / %{customdata[1]} 次 LLM 调用<extra></extra>",
        )
    )
    return chart_layout(
        figure,
        "各 Step 的已记录 API 费用" if money else "各 Step 的已记录 Token 消耗",
        "API 费用（美元）" if money else "Token 数（个）",
    )


def show_overview(monitors: list[RunMonitor]) -> None:
    summary = overview(monitors)
    st.subheader("运行总览")
    st.caption("统计范围为当前侧栏筛选后的运行记录。")
    for col, label, value, help_text in zip(
        st.columns(4),
        ("任务数量", "运行次数", "已提交数量", "异常终止数量"),
        (summary.tasks, summary.runs, summary.submitted, summary.abnormal),
        (
            "有运行记录的不同任务数量。",
            "每次独立启动 Agent 计为一次运行。",
            "Agent 已提交，不代表任务已解决。",
            "达到上限、超时、异常或中断等结束状态；工具失败另行统计。",
        ),
    ):
        col.metric(label, value, help=help_text)
    st.caption(f"运行中 {summary.running} 次 · 结束状态未记录 {summary.unrecorded} 次")
    tokens, cost, timing = st.columns(3)
    tokens.metric(
        "已记录 Token", amount(summary.tokens.value), help="只累计有记录的 LLM Token；不按字符估算，不把缺失值记为 0。"
    )
    tokens.caption(
        f"涉及 {summary.tokens.known} / {summary.runs} 次运行；完整覆盖 {summary.token_complete_runs} 次运行。"
    )
    tokens.caption(f"逐次记录覆盖 {summary.token_known_calls} / {summary.llm_calls} 次 LLM 调用。")
    cost.metric("已记录 API 费用（美元）", amount(summary.cost.value, money=True))
    cost.caption(f"涉及 {summary.cost.known} / {summary.runs} 次运行；完整覆盖 {summary.cost_complete_runs} 次运行。")
    cost.caption(
        f"逐次费用覆盖 {summary.cost_known_calls} / {summary.llm_calls} 次 LLM 调用；{summary.aggregate_cost_runs} 次运行使用轨迹总费用。"
    )
    timing.metric(
        "平均运行耗时",
        elapsed(summary.average_ms.value),
        help="仅对有 Run 总耗时记录的运行求平均；不是 Step、LLM、Tool 耗时之和。",
    )
    timing.caption(coverage(summary.average_ms, "次运行"))
    if summary.token_complete_runs < summary.runs or summary.cost_complete_runs < summary.runs:
        st.caption("Token 和费用可能仅覆盖部分运行或部分调用；以上数值不是全部运行的完整消耗。")


def show_llm(span: Span, index: int, run: Run) -> None:
    attrs = span.attributes
    model = attrs.get("model") if recorded(attrs.get("model")) else run.model
    tokens = number(attrs.get("total_tokens"))
    parts = [f"LLM 调用 {index}"]
    if recorded(model):
        parts.append(model)
    if tokens is not None:
        parts.append(f"Token {amount(tokens)}")
    if number(attrs.get("cost_usd")) is not None:
        parts.append(amount(attrs["cost_usd"], money=True))
    if number(span.duration_ms) is not None:
        parts.append(elapsed(span.duration_ms))
    if span.status == "error":
        parts.append("模型或解析异常")
    title = " · ".join(parts)
    with st.popover(title, use_container_width=True):
        st.markdown("**模型调用**")
        if recorded(model):
            st.write(f"模型：{model}")
        st.write(
            "状态："
            + {"ok": "正常返回", "error": "调用或解析异常", "running": "调用中", "interrupted": "已中断"}.get(
                span.status, "未记录"
            )
        )
        st.write(f"调用耗时：{elapsed(span.duration_ms)}")
        st.write(f"Token：{amount(tokens)}")
        for key, label in (("input_tokens", "输入 Token"), ("output_tokens", "输出 Token")):
            if (value := number(attrs.get(key))) is not None:
                st.write(f"{label}：{amount(value)}")
        st.write(f"API 费用：{amount(number(attrs.get('cost_usd')), money=True)}")
        if error := attrs.get("error") or attrs.get("error_type"):
            st.error(error)
        st.caption("耗时为一次 model.query 的观测值，包含其内部重试或解析；未拆分为每次 HTTP 请求。")
        st.markdown(
            f"[在轨迹页查看 Step {span.step_index} 的决策与原始消息]({trace_link(run.run_id, span.step_index)})"
        )


def show_tool(action: Action, index: int, run: Run, step: int) -> None:
    rc = (
        str(action.observation.returncode)
        if action.observation and action.observation.returncode is not None and action.status != "not_executed"
        else "未记录"
    )
    title = f"Tool 调用 {index} · {action.summary} · {STATUS[action.status]}"
    if number(action.duration_ms) is not None and action.status != "not_executed":
        title += f" · {elapsed(action.duration_ms)}"
    if rc != "未记录" and action.status != "not_executed":
        title += f" · 返回码 {rc}"
    with st.popover(title, use_container_width=True):
        st.markdown(f"**工具：{action.name}**")
        st.write(f"执行状态：{STATUS[action.status]}")
        st.write(f"执行耗时：{elapsed(action.duration_ms)}")
        if action.status == "not_executed":
            st.info("此动作未执行，原轨迹中的 -1 是占位值，不计为非零退出或执行异常，也不计入 Tool 耗时。")
        elif action.observation and action.observation.returncode is not None:
            st.write(f"返回码：{action.observation.returncode}")
        else:
            st.caption(
                "返回码未记录"
                if action.status != "submitted"
                else "环境通过 Submitted 结束任务，未返回普通 Observation。"
            )
        if action.cwd:
            st.caption(f"工作目录：{action.cwd}")
        if action.command:
            st.code(action.command, language="bash")
        elif action.arguments is not None:
            st.write(action.arguments)
        if action.observation and action.observation.exception and action.status != "not_executed":
            st.error(action.observation.exception)
        st.markdown(f"[在轨迹页查看 Step {step} 的完整命令与输出]({trace_link(run.run_id, step)})")


def usage_caption(monitor: RunMonitor) -> None:
    stats = monitor.usage
    metrics = [
        ("Token（已记录）", stats.tokens, amount(stats.tokens.value)),
        ("API 费用（美元）", stats.cost, amount(stats.cost.value, money=True)),
        ("LLM 累计调用耗时", stats.llm_ms, elapsed(stats.llm_ms.value)),
        ("Tool 累计执行耗时", stats.tool_ms, elapsed(stats.tool_ms.value)),
    ]
    available = [(title, metric, value) for title, metric, value in metrics if metric.value is not None]
    if available:
        for col, (title, metric, value) in zip(st.columns(len(available)), available):
            col.metric(title, value)
            col.caption(
                "轨迹中的运行总费用"
                if metric.source == "run"
                else coverage(metric, "次工具执行" if title.startswith("Tool") else "次 LLM 调用")
            )
    missing = [title for title, metric, _ in metrics if metric.value is None]
    if number(monitor.run.duration_ms) is None:
        missing.insert(0, "Run 总耗时")
    if any(number(view.step.duration_ms) is None for view in monitor.steps):
        missing.append("部分或全部 Step 总耗时")
    if missing:
        st.caption("本记录未提供：" + "、".join(missing) + "；调用树省略这些缺失字段，详情仍保留原始数据。")
    if stats.cost.source == "run":
        st.caption(
            f"逐次费用仅覆盖 {monitor.call_cost.known} / {monitor.call_cost.total} 次调用；运行总费用未分摊到 Step。"
        )
    st.caption(
        "Run / Step 总耗时是墙钟测量；LLM / Tool 是调用累计耗时。父子层级不相加，并行调用累计耗时可能超过墙钟时间。"
    )
    llm, tool = stats.llm_ms, stats.tool_ms
    if (
        llm.complete
        and tool.complete
        and llm.value is not None
        and tool.value is not None
        and llm.value + tool.value > 0
    ):
        kind, value = ("LLM", llm.value) if llm.value >= tool.value else ("Tool", tool.value)
        st.info(
            f"已完整记录的调用累计耗时中，{kind} 占 {value / (llm.value + tool.value):.1%}。此比例不代表其占 Run 总耗时的比例。"
        )
    else:
        st.info("调用耗时覆盖不足，无法可靠判断时间主要花在 LLM 还是 Tool。历史消息时间间隔不会用来估算调用耗时。")


def show_charts(monitor: RunMonitor) -> None:
    st.markdown("**耗时与资源分布**")
    if (figure := duration_chart(monitor)) is not None:
        st.plotly_chart(figure, use_container_width=True, key="monitor-duration")
        st.caption("仅绘制已记录的调用耗时，缺失项留空；未执行动作不参与累计。柱高不是 Step 总耗时。")
    else:
        st.caption("可比较的逐步耗时记录不足，不绘制耗时图。")
    choices = {"Token": "tokens", "API 费用": "cost"}
    figures = {
        label: figure for label, field in choices.items() if (figure := resource_chart(monitor, field)) is not None
    }
    if figures:
        selected = st.radio("资源指标", list(figures), horizontal=True, key=f"monitor-chart-{monitor.run.run_id}")
        st.plotly_chart(figures[selected], use_container_width=True, key="monitor-resource")
        st.caption("仅展示有记录的 Step；缺失项留空。鼠标悬停可查看逐次记录覆盖范围。")
    else:
        st.caption("逐步 Token / 费用记录不足，不绘制资源图；运行总费用不会均摊到各 Step。")


def show_steps(monitor: RunMonitor) -> None:
    counts = {
        state: sum(a.status == state for a in monitor.tools)
        for state in ("nonzero_exit", "execution_error", "not_executed")
    }
    call_error_steps = {
        view.step.index
        for view in monitor.steps
        if view.step.status == "call_error" or any(span.status == "error" for span in view.llm)
    }
    call_errors = len(call_error_steps)
    st.caption(
        f"工具结果：非零退出 {counts['nonzero_exit']} 次 · 执行异常 {counts['execution_error']} 次 · 动作未执行 {counts['not_executed']} 次；模型或解析异常 {call_errors} 步。"
    )
    st.caption("非零退出表示命令已执行，不判断是否符合调试预期；任务是否解决以独立评测为准。")
    timed = [view for view in monitor.steps if number(view.step.duration_ms) is not None]
    if timed:
        slowest = max(timed, key=lambda view: view.step.duration_ms)
        st.markdown(
            f"已记录耗时的 {len(timed)} / {len(monitor.steps)} 个步骤中，[Step {slowest.step.index}]({trace_link(monitor.run.run_id, slowest.step.index)}) 最长：{elapsed(slowest.step.duration_ms)}。"
        )
    st.markdown("**Agent Run → Step → 调用**")
    st.caption("展开 Step 查看调用列表，点击 LLM / Tool 节点查看详情。")
    selected = st.radio(
        "调用链范围", ["全部步骤", "非零退出", "执行异常", "模型或解析异常", "动作未执行"], horizontal=True
    )
    target = {"非零退出": "nonzero_exit", "执行异常": "execution_error", "动作未执行": "not_executed"}.get(selected)
    views = [
        view
        for view in monitor.steps
        if selected == "全部步骤"
        or target
        and any(a.status == target for a in view.step.actions)
        or selected == "模型或解析异常"
        and view.step.index in call_error_steps
    ]
    for view in views:
        step, stats = view.step, view.usage
        label = f"Step {step.index} · {step.summary} · {STATUS[step.status]} · {len(view.llm)} 次 LLM / {len(step.actions)} 个工具动作"
        if step.returncodes:
            label += " · " + step.returncodes
        if number(step.duration_ms) is not None:
            label += " · " + elapsed(step.duration_ms)
        with st.expander(label):
            parts = []
            for name, measurement, time, money in (
                ("Token", stats.tokens, False, False),
                ("API 费用", stats.cost, False, True),
                ("LLM 累计耗时", stats.llm_ms, True, False),
                ("Tool 累计耗时", stats.tool_ms, True, False),
            ):
                if measurement.value is not None:
                    parts.append(
                        f"{name} {elapsed(measurement.value) if time else amount(measurement.value, money=money)}（{measurement.known}/{measurement.total} 次）"
                    )
            if parts:
                st.caption(" · ".join(parts))
            for i, span in enumerate(view.llm, 1):
                show_llm(span, i, monitor.run)
            for i, action in enumerate(step.actions, 1):
                show_tool(action, i, monitor.run, step.index)
            if not view.llm and not step.actions:
                st.caption("本步无模型或工具调用记录。")
            st.markdown(f"[在轨迹页查看 Step {step.index}]({trace_link(monitor.run.run_id, step.index)})")
    if not views:
        st.caption("没有符合条件的步骤。")
    if monitor.orphan_llm or monitor.orphan_tools:
        with st.expander("未归属 Step 的调用"):
            st.warning("以下调用缺少可关联的 Step，计入 Run 汇总，但未分配到任何步骤。")
            for i, span in enumerate(monitor.orphan_llm, 1):
                show_llm(span, i, monitor.run)
            for i, action in enumerate(monitor.orphan_tools, 1):
                show_tool(action, i, monitor.run, 0)


def monitoring_page(store: Store, runs: list[Run]) -> None:
    compact_layout()
    st.header("M2 · 运行监控")
    monitors = [monitor_run(run) for run in runs]
    show_overview(monitors)
    if not runs:
        st.info("暂无符合筛选条件的运行记录。导入轨迹或运行一个任务后即可查看。")
        return
    st.divider()
    st.subheader("单次运行详情")
    tasks = {task.task_id: task for task in store.tasks()}
    numbers, counts = {}, {}
    for run in reversed(store.runs()):
        counts[run.task_id] = counts.get(run.task_id, 0) + 1
        numbers[run.run_id] = counts[run.task_id]
    route = st.query_params.get("run", "")
    task_key, run_key = f"monitor-task-{store.path}", f"monitor-run-{store.path}"
    task_ids = list(dict.fromkeys(run.task_id for run in runs))
    if st.session_state.get(task_key) not in [""] + task_ids:
        st.session_state[task_key] = ""
    if route != st.session_state.get("monitor-route") and route in {run.run_id for run in runs}:
        st.session_state[run_key] = route
        st.session_state[task_key] = next(run.task_id for run in runs if run.run_id == route)
    left, right = st.columns([1, 2])
    task_id = left.selectbox(
        "查看任务",
        [""] + task_ids,
        key=task_key,
        format_func=lambda value: task_name(tasks[value]) if value else "全部任务",
    )
    candidates = [m for m in monitors if not task_id or m.run.task_id == task_id]
    ids = [m.run.run_id for m in candidates]
    if st.session_state.get(run_key) not in ids:
        st.session_state[run_key] = ids[0]
    lookup = {m.run.run_id: m for m in candidates}
    rid = right.selectbox(
        "查看运行",
        ids,
        key=run_key,
        format_func=lambda value: f"{task_name(tasks[lookup[value].run.task_id])} · 第 {numbers[value]} 次运行 · {lookup[value].run.model if recorded(lookup[value].run.model) else '模型未记录'}",
    )
    monitor = lookup[rid]
    run = monitor.run
    st.query_params.update({"page": "监控", "run": rid})
    st.session_state["monitor-route"] = rid
    st.caption("运行序号按同任务的入库顺序编号，不因页面筛选而改变。")
    cols = st.columns(4 if number(run.duration_ms) is not None else 3)
    state, result, steps = cols[:3]
    state.metric("Agent 运行状态", run_state(run.exit_status))
    result.metric("独立评测结果", VERDICT.get(store.evaluation(rid).verdict, "未记录"))
    steps.metric("执行步数", len(monitor.steps))
    if len(cols) == 4:
        cols[3].metric("本次运行总耗时", elapsed(run.duration_ms))
    st.caption("Agent 已提交、工具返回码为 0 和独立评测通过是三种不同的状态。")
    if run.model == "offline-scripted-demo":
        st.info("本条是离线脚本演示：工具命令与耗时为真实执行记录，LLM 耗时仅是脚本返回时间，不代表在线 API 时延。")
    usage_caption(monitor)
    show_charts(monitor)
    show_steps(monitor)
    with st.expander("高级详情 · 原始数据与导出"):
        st.json({"运行汇总": asdict(monitor.usage), "原始轨迹": run.to_dict()})
        rows = compare(store, [r.run_id for r in runs])
        for col, format in zip(st.columns(2), ("csv", "json")):
            text = export_rows(rows, format)
            col.download_button(
                f"导出 {format.upper()}",
                ("\ufeff" + text) if format == "csv" else text,
                f"monitoring.{format}",
                mime="text/csv" if format == "csv" else "application/json",
                key="monitor-export-" + format,
            )
