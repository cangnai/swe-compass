"""M3: select a run, inspect explained diagnostics and follow evidence into M1."""

import json
from dataclasses import asdict

import streamlit as st

from minisweagent.observability.diagnostic_details import ending, explain
from minisweagent.observability.diagnostics import RULE_VERSION, diagnose
from minisweagent.observability.monitoring import monitor_run
from minisweagent.observability.monitoring_dashboard import task_name, trace_link
from minisweagent.observability.schema import Run
from minisweagent.observability.storage import Store
from minisweagent.observability.trace_dashboard import VERDICT, compact_layout, recorded


def excerpt(text: str, limit: int = 240) -> str:
    lines = text.splitlines()
    value = "\n".join(lines[:3])
    return value[:limit] + ("\n…完整证据见对应 Step 或高级详情。" if len(value) > limit or len(lines) > 3 else "")


def diagnostics_page(store: Store, runs: list[Run]) -> None:
    compact_layout()
    st.header("M3 · 失败分析")
    if not runs:
        st.info("暂无符合筛选条件的运行记录。导入轨迹或运行一个任务后即可查看诊断。")
        return
    st.subheader("运行选择")
    tasks = {task.task_id: task for task in store.tasks()}
    numbers, counts = {}, {}
    for run in reversed(store.runs()):
        counts[run.task_id] = counts.get(run.task_id, 0) + 1
        numbers[run.run_id] = counts[run.task_id]
    route = st.query_params.get("run", "")
    task_key, run_key = f"diagnostic-task-{store.path}", f"diagnostic-run-{store.path}"
    task_ids = list(dict.fromkeys(run.task_id for run in runs))
    if st.session_state.get(task_key) not in [""] + task_ids:
        st.session_state[task_key] = ""
    if route != st.session_state.get("diagnostic-route") and route in {run.run_id for run in runs}:
        st.session_state[run_key] = route
        st.session_state[task_key] = next(run.task_id for run in runs if run.run_id == route)
    left, right = st.columns([1, 2])
    task_id = left.selectbox(
        "诊断任务",
        [""] + task_ids,
        key=task_key,
        format_func=lambda value: task_name(tasks[value]) if value else "全部任务",
    )
    candidates = [run for run in runs if not task_id or run.task_id == task_id]
    lookup = {run.run_id: run for run in candidates}
    if st.session_state.get(run_key) not in lookup:
        st.session_state[run_key] = candidates[0].run_id
    rid = right.selectbox(
        "诊断运行",
        list(lookup),
        key=run_key,
        format_func=lambda value: f"{task_name(tasks[lookup[value].task_id])} · 第 {numbers[value]} 次运行 · {lookup[value].model if recorded(lookup[value].model) else '模型未记录'}",
    )
    run = lookup[rid]
    monitor = monitor_run(run)
    events = diagnose(run)
    details = [explain(event, monitor) for event in events]
    st.query_params.update({"page": "失败分析", "run": rid})
    st.session_state["diagnostic-route"] = rid
    st.caption(
        f"Agent 结束状态：{ending(run.exit_status)} · 独立评测：{VERDICT.get(store.evaluation(rid).verdict, '未记录')}"
    )
    st.subheader("诊断概览")
    for col, label, count in zip(
        st.columns(4),
        ("确定异常", "人工干预", "行为风险", "执行记录"),
        (
            sum(d.category == "确定异常" for d in details),
            len({d.event.step_index or d.event.event_id for d in details if d.category == "人工干预"}),
            sum(d.category == "行为风险" for d in details),
            sum(d.category == "执行记录" for d in details),
        ),
    ):
        col.metric(label, count)
    st.caption(
        "确定异常、人工干预、启发式行为风险分开统计，人工干预按已关联 Step 去重。运行限制、中断原因不明及未注明原因的未执行动作归入执行记录。普通非零退出、命令重复和单纯测试失败仅在轨迹与监控中记录。"
    )
    if any(d.category == "确定异常" for d in details):
        st.caption(
            f"异常记录来源：模型或解析 {sum(d.event.type in {'api_error', 'format_error'} for d in details)} 条 · "
            f"执行器 {sum(d.event.type in {'tool_error', 'tool_timeout'} for d in details)} 条 · "
            f"运行异常结束 {sum(d.event.type == 'termination' and d.category == '确定异常' for d in details)} 条；同一步可有不同层次的异常记录。"
        )
    st.subheader("诊断列表")
    categories = ["全部诊断", "确定异常", "人工干预", "行为风险", "执行记录"]
    selected = st.radio("诊断范围", categories, horizontal=True)
    visible = [detail for detail in details if selected == "全部诊断" or detail.category == selected]
    if not visible:
        st.info("当前范围没有触发诊断；这不代表任务已经解决。")
    for detail in visible:
        event = detail.event
        position = f"Step {event.step_index}" if event.step_index else "步骤未关联"
        with st.expander(f"{position} · {detail.title} · {detail.category}"):
            st.markdown("**发生了什么**")
            st.write(detail.happened)
            st.markdown("**为什么触发**")
            st.write(detail.trigger)
            st.markdown("**判断依据**")
            for line in detail.evidence:
                st.text(excerpt(line))
            if not detail.evidence:
                st.caption("诊断依据为记录中的异常状态，未提供额外文字。")
            st.markdown("**后续执行情况**")
            for line in detail.subsequent:
                st.write(line)
            if event.step_index:
                st.markdown(
                    f"[在轨迹页查看 Step {event.step_index} 的完整执行过程]({trace_link(rid, event.step_index)})"
                )
            with st.popover("高级详情 · 原始诊断证据", use_container_width=True):
                st.json(asdict(event))
    with st.expander("高级详情 · 规则与完整诊断下载"):
        st.caption(
            f"当前规则版本：{RULE_VERSION}。每次读取运行记录时重新计算诊断；数据库内旧诊断行保留用于追溯，不参与当前判断。原始消息和观测数据不变。"
        )
        st.download_button(
            "下载诊断 JSON",
            json.dumps([asdict(event) for event in events], ensure_ascii=False, indent=2),
            f"{rid}.diagnostics.json",
            mime="application/json",
        )
