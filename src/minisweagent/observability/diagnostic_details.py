"""Chinese diagnostic explanations built from original evidence and subsequent steps."""

from dataclasses import dataclass

from minisweagent.observability.diagnostics import CONTROLLED_ENDINGS, HUMAN_INTERRUPTS, event_group, normalize_command
from minisweagent.observability.execution_status import STATUS
from minisweagent.observability.monitoring import RunMonitor, run_state
from minisweagent.observability.schema import Event
from minisweagent.observability.trajectory_steps import command_summary

TITLES = {
    "format_error": "模型输出无法解析为 Action",
    "api_error": "模型调用异常",
    "tool_error": "工具执行器异常",
    "tool_timeout": "工具执行超时",
    "action_not_executed": "动作未执行",
    "human_intervention": "用户介入执行流程",
    "suspected_no_progress": "疑似连续操作无进展",
    "termination": "Agent 运行异常终止",
}
SEVERITIES = {"error": "异常", "warning": "风险提示", "info": "事件记录"}


@dataclass
class Diagnostic:
    event: Event
    title: str
    category: str
    severity: str
    confidence: str
    happened: str
    trigger: str
    evidence: list[str]
    subsequent: list[str]


def ending(status: str) -> str:
    return {
        "UserInterruption": "用户中断",
        "UserRejection": "用户拒绝",
        "UserNewTask": "用户追加任务",
        "KeyboardInterrupt": "运行被中断",
        "Interrupted": "运行已中断（原因未记录）",
    }.get(status, run_state(status))


def subsequent(event: Event, monitor: RunMonitor) -> list[str]:
    final = f"Agent 最终状态：{ending(monitor.run.exit_status)}。任务是否解决仍以独立评测为准。"
    if not event.step_index:
        return ["该记录未关联 Step，无法判断它与其他步骤的先后关系。", final]
    later = [view for view in monitor.steps if view.step.index > event.step_index]
    lines = [f"后续还有 {len(later)} 个已记录 Step。" if later else "该诊断之后没有更多已记录 Step。"]
    command = event.evidence.get("command")
    if command:
        attempts = [
            (view.step.index, action)
            for view in later
            for action in view.step.actions
            if normalize_command(action.command) == normalize_command(command)
        ]
        if attempts:
            index, action = attempts[0]
            lines.append(f"相同命令随后在 Step {index} 再次出现：{STATUS[action.status]}。这只说明该次命令结果。")
    if later:
        next_step = later[0].step
        lines.append(f"下一步为 Step {next_step.index}：{next_step.summary}。")
    lines.append(final)
    return lines


def explain(event: Event, monitor: RunMonitor) -> Diagnostic:
    data, kind = event.evidence, event.type
    title = TITLES.get(kind, "运行诊断记录")
    evidence = []
    if command := data.get("command"):
        evidence.append("操作：" + command_summary(command))
    if data.get("returncode") is not None and kind != "action_not_executed":
        evidence.append(f"实际返回码：{data['returncode']}")
    steps = data.get("steps")
    if steps:
        evidence.append("关联步骤：" + "、".join(f"Step {i}" for i in steps))
    if kind == "format_error":
        happened, trigger = (
            "模型回复未能通过 Action 格式解析。",
            "解析器明确记录了 FormatError；不根据回复文本猜测异常。",
        )
        evidence.append("解析器异常类型：FormatError；原始模型回复与格式反馈见对应 Step。")
    elif kind == "api_error":
        happened, trigger = (
            "模型查询调用记录了异常。",
            "依据模型调用 Span 的异常状态或异常类型；不把工具非零退出算作模型异常。",
        )
    elif kind in {"tool_error", "tool_timeout"}:
        happened = "执行器记录了超时。" if kind == "tool_timeout" else "执行器记录了启动或执行过程异常。"
        trigger = "依据执行器异常信息、异常类型或明确的异常记录；非零退出码本身不触发本类诊断。"
    elif kind in {"action_not_executed", "human_intervention"}:
        types = {m.get("extra", {}).get("interrupt_type") for m in data.get("interventions", [])}
        if "UserRejection" in types:
            title = "用户拒绝执行动作" if kind == "action_not_executed" else "用户拒绝后续操作"
            happened = (
                "用户主动拒绝了本步动作；对应命令没有执行。"
                if kind == "action_not_executed"
                else "轨迹记录了用户拒绝消息；不据此推翻已经记录的命令执行结果。"
            )
        else:
            happened = (
                "轨迹明确记录该动作未执行。" if kind == "action_not_executed" else "轨迹记录了用户对执行流程的干预。"
            )
        trigger = (
            "依据明确的未执行 Observation；有用户干预消息时同时保留其原因。占位返回码 -1 不当作执行异常。"
            if kind == "action_not_executed"
            else "依据轨迹的用户干预类型和原始消息。"
        )
        evidence.extend(reason for reason in data.get("reason", []) if reason)
        if not data.get("reason"):
            evidence.append("没有记录具体原因，不推断为用户拒绝。")
    elif kind == "suspected_no_progress":
        happened, trigger = (
            f"连续 {data['count']} 步的同一命令返回相同失败特征，已记录的代码状态保持不变。",
            "要求至少相邻三步、每步只有该动作、同一工作目录、相同命令与非零返回码、非空失败特征、完整前后快照一致且没有人工介入。这是启发式风险，不是确定无进展。",
        )
        evidence.append("相同失败特征：" + data["signature"].splitlines()[0][:180])
        evidence.append("已记录的代码快照一致；快照范围之外的环境变化仍不可判断。")
    elif kind == "termination":
        if event.severity == "info" and data.get("exit_status") in CONTROLLED_ENDINGS | HUMAN_INTERRUPTS:
            title = "Agent 受控结束" if data["exit_status"] in {"LimitsExceeded", "TimeExceeded"} else "Agent 运行中断"
        happened, trigger = (
            f"Agent 以“{ending(data.get('exit_status'))}”结束。",
            "依据 Run 结束状态及 Run 调用明确记录的异常；运行限制和人工中断单独记录，不按异常崩溃统计。工具退出码不决定 Run 状态。",
        )
        if data.get("run_errors"):
            happened = f"Run 调用明确记录了异常；保存的结束状态为“{ending(data.get('exit_status'))}”。"
        exit_extra = data.get("exit_message", {}).get("extra", {})
        if reason := exit_extra.get("exception_str"):
            evidence.append(str(reason).splitlines()[0])
        for error in data.get("run_errors", []):
            attrs = error["attributes"]
            evidence.append(f"Run 调用记录异常：{attrs.get('error_type') or '异常类型未记录'}")
            if reason := attrs.get("error"):
                evidence.append(str(reason).splitlines()[0])
    else:
        happened, trigger = "发现运行诊断记录。", event.rule
    if kind in {"api_error", "format_error", "tool_error", "tool_timeout"}:
        for field in ("error_type", "exception_type", "error", "exception_info"):
            if value := data.get(field):
                if str(value) not in evidence:
                    evidence.append(str(value).splitlines()[0])
    return Diagnostic(
        event,
        title,
        event_group(event),
        SEVERITIES.get(event.severity, "记录"),
        "启发式风险" if kind == "suspected_no_progress" else "记录事实",
        happened,
        trigger,
        [line for line in evidence if line],
        subsequent(event, monitor),
    )
