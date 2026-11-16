"""Step-oriented M1 viewer; raw evidence stays in collapsed advanced details."""

import json
from dataclasses import asdict
from pathlib import Path

import streamlit as st

from minisweagent.observability.execution_status import STATUS
from minisweagent.observability.metrics import run_metrics
from minisweagent.observability.schema import Run
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_steps import Observation, Step, build_trace, response_text

VERDICT = {"passed": "通过", "failed": "未通过", "not_evaluated": "未评测", "evaluation_error": "评测异常"}


def compact_layout() -> None:
    st.html("""<style>
    section[data-testid="stMain"] .block-container {padding-top:4.5rem; padding-bottom:1.5rem;}
    section[data-testid="stMain"] [data-testid="stVerticalBlock"] {gap:0.6rem;}
    section[data-testid="stMain"] [data-testid="stMetricValue"] {font-size:1.6rem;}
    section[data-testid="stMain"] h2, section[data-testid="stMain"] h3 {padding-top:0.25rem; padding-bottom:0.35rem;}
    </style>""")


def recorded(value) -> bool:
    return value is not None and value not in ("", "unknown", "None")


def duration(value: float) -> str:
    return f"{value / 1000:.2f} 秒" if value >= 1000 else f"{value:.1f} 毫秒"


def content(text: str, label: str, language: str | None = None, key: str = "") -> None:
    if not text:
        return
    long = len(text) > 1200 or len(text.splitlines()) > 16
    if long:
        with st.expander(f"{label} · {len(text):,} 字符 · 展开全文"):
            if key:
                needle = st.text_input("筛选日志行", key="search-" + key)
                st.code(
                    "\n".join(line for line in text.splitlines() if needle.casefold() in line.casefold())
                    if needle
                    else text,
                    language=language,
                )
                st.download_button("下载完整日志", text, f"{key}.txt", key="download-" + key)
            elif language:
                st.code(text, language=language)
            else:
                st.text(text)
    elif language or key:
        st.code(text, language=language)
    else:
        st.text(text)


def show_observation(obs: Observation | None, status: str, key: str) -> None:
    if status == "not_executed":
        st.info("此动作未执行。轨迹中的 -1 是占位返回码，不计为非零退出或执行异常。")
        return
    if obs is None:
        st.caption(
            "未记录执行结果" if status != "submitted" else "环境通过 Submitted 结束任务，未返回普通 Observation。"
        )
        return
    if obs.returncode is not None:
        st.caption(f"返回码：{obs.returncode} · {STATUS[status]}")
    elif status != "submitted":
        st.caption("返回码未记录")
    if obs.exception:
        st.error(obs.exception)
    if obs.output and obs.output.lstrip().startswith("diff --git"):
        with st.expander("代码 Diff（本步 Observation）"):
            st.code(obs.output, language="diff")
        return
    streams = (
        [("stdout", obs.stdout), ("stderr", obs.stderr)]
        if obs.stdout is not None or obs.stderr is not None
        else [("输出（轨迹未区分 stdout / stderr）", obs.output)]
    )
    for label, text in streams:
        if text:
            st.caption(label)
            content(text, label, key=key + "-" + label[:6])
    if not any(text for _, text in streams) and not obs.exception:
        st.caption("输出为空" if any(text is not None for _, text in streams) else "输出未记录")


def show_step(run: Run, step: Step) -> None:
    st.subheader(f"Step {step.index}")
    st.caption(STATUS[step.status] + (f" · {duration(step.duration_ms)}" if step.duration_ms is not None else ""))
    st.markdown("**人工指令**" if step.human else "**LLM 决策**")
    if step.decisions:
        for text in step.decisions:
            content(text, "完整回复")
    else:
        st.caption("本步未记录说明文本")
    for error in step.errors:
        content(error, "异常信息")
    if not step.actions:
        st.caption("本步无 Action / Tool Call 记录")
    for i, action in enumerate(step.actions, 1):
        st.markdown(f"**Action {i} · {action.name}**")
        details = [STATUS[action.status]]
        if action.duration_ms is not None:
            details.append(duration(action.duration_ms))
        if action.cwd:
            details.append(f"目录：{action.cwd}")
        st.caption(" · ".join(details))
        if action.command is not None:
            content(action.command, "完整命令", language="bash")
            if isinstance(action.arguments, dict) and (
                other := {k: v for k, v in action.arguments.items() if k not in {"command", "tool_call_id", "cwd"}}
            ):
                with st.expander("其他调用参数"):
                    st.json(other)
        elif action.arguments is not None:
            content(
                json.dumps(action.arguments, ensure_ascii=False, indent=2)
                if isinstance(action.arguments, dict)
                else action.arguments,
                "调用参数",
                language="json",
            )
        st.markdown(f"**Observation {i}**")
        show_observation(action.observation, action.status, f"{run.run_id}-{step.index}-{i}")
    for i, obs in enumerate(step.unmatched_observations):
        st.warning("以下 Observation 没有匹配的 Tool Call，已保留原始结果。")
        show_observation(obs, "unrecorded", f"{run.run_id}-{step.index}-unmatched-{i}")
    for note in step.notes:
        st.markdown("**用户补充 / 流程反馈**")
        content(response_text(note), "完整反馈")
    with st.expander("高级详情 · 原始消息与 Span"):
        st.json(
            {
                "message_indices": step.message_indices,
                "messages": [run.messages[i] for i in step.message_indices],
                "spans": [asdict(s) for s in step.spans],
            }
        )


def trace_page(store: Store, runs: list[Run]) -> None:
    compact_layout()
    st.header("M1 · 执行轨迹")
    if not runs:
        st.info("导入轨迹或在任务页运行一个任务")
        return
    ids = [r.run_id for r in runs]
    requested = st.query_params.get("run", "")
    route = (requested, st.query_params.get("step", ""))
    run_key = f"trace-run-{store.path}"
    if route != st.session_state.get("trace-route") and requested in ids:
        st.session_state[run_key] = requested
    if st.session_state.get(run_key) not in ids:
        st.session_state[run_key] = ids[0]
    tasks = {t.task_id: t for t in store.tasks()}
    rid = st.selectbox(
        "运行记录",
        ids,
        key=run_key,
        format_func=lambda value: f"{tasks[next(r.task_id for r in runs if r.run_id == value)].title[:65]} · {value[:12]}",
    )
    run = next(r for r in runs if r.run_id == rid)
    trace = build_trace(run)
    metrics = run_metrics(run)
    overview = [
        ("执行步数", str(len(trace.steps))),
        (
            "Agent 结束状态",
            "已提交"
            if run.exit_status == "Submitted"
            else "运行中"
            if run.exit_status == "running"
            else run.exit_status
            if recorded(run.exit_status)
            else "未记录",
        ),
        ("独立评测", VERDICT.get(store.evaluation(rid).verdict, store.evaluation(rid).verdict)),
    ]
    if run.duration_ms is not None:
        overview.append(("总耗时", duration(run.duration_ms)))
    if metrics["total_tokens"] is not None:
        overview.append(("Token（已记录）", f"{metrics['total_tokens']:,}"))
    if metrics["cost_usd"] is not None:
        overview.append(("费用（已记录）", f"${metrics['cost_usd']:.5f}"))
    for col, (label, value) in zip(st.columns(len(overview)), overview):
        col.metric(label, value)
    if recorded(run.model):
        st.caption(f"模型：{run.model}")
    st.caption("步骤状态描述命令执行；Agent 提交与命令返回码为 0 均不代表任务已解决，修复结果以独立评测为准。")
    st.caption("非零退出表示命令已执行，可能是测试发现问题或预期复现；只有明确的执行器异常才标为执行异常。")
    if metrics["total_tokens"] is not None and metrics["total_tokens_known_calls"] < metrics["llm_calls"]:
        st.caption(f"Token 覆盖 {metrics['total_tokens_known_calls']} / {metrics['llm_calls']} 次已记录模型调用。")
    with st.expander("任务内容与初始化提示"):
        content(tasks[run.task_id].description or tasks[run.task_id].title, "任务全文")
        for i in trace.initial_indices:
            if response_text(run.messages[i]) == tasks[run.task_id].description:
                continue
            st.caption("System Prompt" if run.messages[i].get("role") == "system" else "User Prompt")
            content(response_text(run.messages[i]), "完整提示")
    if trace.steps:
        indices = [s.index for s in trace.steps]
        key = f"trace-step-{rid}"
        if route != st.session_state.get("trace-route") and requested == rid:
            target = int(route[1]) if route[1].isdigit() else indices[0]
            st.session_state[key] = target if target in indices else indices[0]
        if st.session_state.get(key) not in indices:
            st.session_state[key] = indices[0]
        listing, detail = st.columns([1, 2.4])
        with listing:
            st.subheader("执行步骤")
            previous, following = st.columns(2)
            position = indices.index(st.session_state[key])
            if previous.button("上一步", disabled=position == 0):
                st.session_state[key] = indices[position - 1]
            if following.button("下一步", disabled=position == len(indices) - 1):
                st.session_state[key] = indices[position + 1]
            lookup = {s.index: s for s in trace.steps}
            chosen = st.radio(
                "选择步骤",
                indices,
                key=key,
                format_func=lambda i: f"Step {i} · {lookup[i].summary} · {STATUS[lookup[i].status]}"
                + (f" · {lookup[i].returncodes}" if lookup[i].returncodes else "")
                + (f" · {duration(lookup[i].duration_ms)}" if lookup[i].duration_ms is not None else ""),
            )
        with detail:
            show_step(run, lookup[chosen])
        st.query_params.update({"page": "轨迹", "run": rid, "step": str(chosen)})
        st.session_state["trace-route"] = (rid, str(chosen))
    else:
        st.info("此轨迹没有已记录的执行步骤。")
        st.query_params.update({"page": "轨迹", "run": rid, "step": "0"})
        st.session_state["trace-route"] = (rid, "0")
    with st.expander("任务结束与提交内容"):
        st.write(f"Agent 结束状态：{overview[1][1]}")
        if run.submission:
            content(run.submission, "完整提交内容")
        for i in trace.exit_indices:
            message = run.messages[i]
            if response_text(message) and response_text(message) != run.submission:
                content(response_text(message), "结束信息")
            if error := message.get("extra", {}).get("exception_str"):
                st.error(error)
    if run.diff and run.diff.strip() not in [s.diff.strip() for s in trace.steps if s.diff]:
        with st.expander("运行级代码 Diff（未归属到具体 Step）"):
            st.code(run.diff, language="diff")
    with st.expander("高级详情 · 配置与完整轨迹下载"):
        st.json(run.config_snapshot)
        st.download_button(
            "下载统一 Trace JSON", json.dumps(run.to_dict(), ensure_ascii=False, indent=2), f"{rid}.trace.json"
        )
        if Path(run.trajectory_path).is_file():
            st.download_button("下载原始轨迹", Path(run.trajectory_path).read_bytes(), f"{rid}.traj.json")
