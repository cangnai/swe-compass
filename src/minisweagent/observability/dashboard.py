"""Run with `python -m minisweagent.run.observe serve --db ...`."""

import argparse
import hashlib
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

from minisweagent.config import get_config_from_spec
from minisweagent.observability.diagnostics_dashboard import diagnostics_page
from minisweagent.observability.experiments import compare, evaluate, export_rows, run_task
from minisweagent.observability.metrics import aggregate
from minisweagent.observability.monitoring import run_state
from minisweagent.observability.monitoring_dashboard import monitoring_page, task_name
from minisweagent.observability.schema import Evaluation, Experiment, Task, new_id
from minisweagent.observability.storage import Store
from minisweagent.observability.trace_dashboard import trace_page
from minisweagent.observability.trajectory_parser import import_trajectory
from minisweagent.utils.serialize import recursive_merge

PAGES = ["轨迹", "监控", "失败分析", "任务与对比"]


def trace_url(run_id: str, step: int = 0) -> str:
    return "?" + urlencode({"page": "轨迹", "run": run_id, "step": step})


def table(rows: list[dict]) -> None:
    if rows:
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            column_config={"查看": st.column_config.LinkColumn("查看 Trace / Step", display_text="打开轨迹")}
            if "查看" in rows[0]
            else None,
        )
    else:
        st.info("暂无记录")


def downloads(rows: list[dict], key: str) -> None:
    for col, format in zip(st.columns(2), ("csv", "json")):
        text = export_rows(rows, format)
        col.download_button(
            f"导出 {format.upper()}",
            ("\ufeff" + text) if format == "csv" else text,
            f"{key}.{format}",
            mime="text/csv" if format == "csv" else "application/json",
            key=f"{key}-{format}",
        )


def experiments_page(store: Store, runs: list) -> None:
    st.header("M4 · 任务与通用对比")
    table([asdict(t) for t in store.tasks()])
    with st.expander("登记自定义任务"):
        with st.form("task-registration"):
            tid = st.text_input("task_id", value="task-" + new_id()[:8])
            title = st.text_input("标题")
            description = st.text_area("任务描述")
            repo = st.text_input("Git 仓库绝对路径")
            revision = st.text_input("初始 commit SHA")
            command = st.text_input("验证命令（可空）")
            if st.form_submit_button("保存任务"):
                if not tid or not title:
                    st.error("请填写 task_id 和标题")
                else:
                    store.save_task(Task(tid, title, description, repo, revision, command))
                    st.success("任务已保存")
                    st.rerun()
    tasks = store.tasks()
    if tasks:
        with st.expander("运行 Agent（每次从初始 commit 创建独立工作区）"):
            with st.form("execute-task"):
                task_id = st.selectbox("任务", [t.task_id for t in tasks])
                specs = st.text_area("配置规格，每行一个；顺序合并", value="mini.yaml")
                do_eval = st.checkbox("执行任务的真实验证命令", value=True)
                if st.form_submit_button("启动真实运行"):
                    with st.spinner("正在执行 Agent 与评测…"):
                        run = run_task(
                            store,
                            task_id,
                            recursive_merge(
                                *[get_config_from_spec(s.strip()) for s in specs.splitlines() if s.strip()]
                            ),
                            evaluate_after=do_eval,
                        )
                    st.success(f"运行结束：{run.run_id}")
                    st.markdown(f"[查看 Trace]({trace_url(run.run_id)})")
    if not runs:
        return
    ids = [r.run_id for r in runs]
    with st.expander("关联已有 Run 到任务 / 运行验证"):
        rid = st.selectbox("已有 Run", ids)
        if tasks:
            tid = st.selectbox("关联任务", [t.task_id for t in tasks], key="associate-task")
            if st.button("保存关联（清除旧任务的评测判定）"):
                run = store.run(rid)
                if run.task_id != tid:
                    run.task_id = tid
                    run.metadata.pop("task_snapshot", None)
                    store.save_run(run)
                    store.save_evaluation(Evaluation(rid))
                st.success("关联已保存")
        if st.button("在该 Run 的保留工作区执行验证"):
            with st.spinner("评测中…"):
                st.json(asdict(evaluate(store, rid)))
    selected = st.multiselect("选择任意 Run 比较（可跨任务，不预设实验组）", ids)
    experiments = store.experiments()
    if experiments:
        chosen = st.selectbox(
            "打开已保存对比",
            [""] + [e.experiment_id for e in experiments],
            format_func=lambda eid: next((e.name for e in experiments if e.experiment_id == eid), "请选择"),
        )
        if chosen and not selected:
            selected = next(e.run_ids for e in experiments if e.experiment_id == chosen)
    if selected:
        rows = compare(store, selected)
        table([{k: v for k, v in r.items() if k not in {"config_snapshot", "test_summary"}} for r in rows])
        st.caption("比较前核对 task_id、base_revision 和 initial_state；跨任务汇总不代表公平的模型排名。")
        for row in rows:
            with st.expander(f"{row['run_id']} · 配置与评测证据"):
                st.json(row)
                st.markdown(f"[打开 Trace]({trace_url(row['run_id'])})")
        st.subheader("所选 Run 汇总")
        st.json(aggregate([store.run(rid) for rid in selected]))
        downloads(rows, "comparison")
        name = st.text_input("保存为对比名称")
        if st.button("保存 Experiment", disabled=not name):
            store.save_experiment(Experiment(name, selected))
            st.success("对比已保存")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".mini-observe/traces.db")
    args, _ = parser.parse_known_args()
    st.set_page_config(page_title="mini-SWE-agent 观测与评测", layout="wide")
    st.title("mini-SWE-agent · 观测与评测")
    db = st.sidebar.text_input("SQLite 数据库", str(Path(args.db).resolve()))
    store = Store(db)
    st.sidebar.button("刷新记录")
    page = st.sidebar.radio(
        "功能", PAGES, index=PAGES.index(st.query_params.get("page")) if st.query_params.get("page") in PAGES else 0
    )
    with st.sidebar.expander("导入历史轨迹"):
        files = st.file_uploader(".traj.json（可多选）", type="json", accept_multiple_files=True)
        if st.button("导入所选文件", disabled=not files):
            for file in files:
                data = file.getvalue()
                source = store.artifacts / f"upload-{hashlib.sha256(data).hexdigest()}.traj.json"
                source.write_bytes(data)
                import_trajectory(store, source)
            st.success("导入完成")
    all_runs = store.runs()
    if page == "失败分析":
        diagnostics_page(store, all_runs)
        return
    tasks_by_id = {t.task_id: t for t in store.tasks()}
    task = st.sidebar.selectbox(
        "筛选任务",
        [""] + sorted({r.task_id for r in all_runs}),
        format_func=lambda value: task_name(tasks_by_id[value]) if value else "全部任务",
    )
    model = st.sidebar.selectbox(
        "筛选模型",
        [""] + sorted({r.model for r in all_runs}),
        format_func=lambda value: "全部模型" if not value else "模型未记录" if value == "unknown" else value,
    )
    status = st.sidebar.selectbox(
        "筛选终止状态",
        [""] + sorted({r.exit_status for r in all_runs}),
        format_func=lambda value: run_state(value) if value else "全部状态",
    )
    search = st.sidebar.text_input("搜索消息 / 命令 / 输出")
    runs = store.runs(task, model, status, search)
    if page == "轨迹":
        trace_page(store, runs)
    elif page == "监控":
        monitoring_page(store, runs)
    else:
        experiments_page(store, runs)


if __name__ == "__main__":
    main()
