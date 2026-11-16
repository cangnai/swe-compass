"""Execution outcomes are independent of Agent termination and task evaluation."""

STATUS = {
    "ok": "正常退出",
    "nonzero_exit": "非零退出",
    "execution_error": "执行异常",
    "call_error": "模型或解析异常",
    "not_executed": "动作未执行",
    "submitted": "提交结束",
    "unrecorded": "未记录",
    "running": "执行中",
    "interrupted": "流程中断",
}


def tool_status(attributes: dict, status: str = "unknown") -> str:
    extra = attributes.get("extra") or {}
    exception = attributes.get("exception_info") or attributes.get("error")
    exception_type = attributes.get("exception_type") or attributes.get("error_type") or extra.get("exception_type")
    if exception == "action was not executed" or status == "not_executed":
        return "not_executed"
    if status in {"submitted", "running", "interrupted"}:
        return status
    if exception or exception_type or extra.get("exception") or status == "execution_error":
        return "execution_error"
    rc = attributes.get("returncode")
    if isinstance(rc, int) and not isinstance(rc, bool):
        return "ok" if rc == 0 else "nonzero_exit"
    # Old wrappers used error for both nonzero returns and raised exceptions.
    return "execution_error" if status == "error" else "unrecorded"


def abnormal_run(status: str | None) -> bool:
    return status not in {"Submitted", "running", "unknown", "", None}
