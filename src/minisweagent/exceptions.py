class InterruptAgentFlow(Exception):
    """Raised to interrupt the agent flow and add messages."""
    # 抛出此异常以中断智能体的执行流程，并添加消息。

    def __init__(self, *messages: dict):
        self.messages = messages
        super().__init__()


class Submitted(InterruptAgentFlow):
    """Raised when the agent has completed its task."""
    # 智能体完成任务时抛出。


class LimitsExceeded(InterruptAgentFlow):
    """Raised when the agent has exceeded its cost or step limit."""
    # 智能体超出费用或执行步数限制时抛出。


class TimeExceeded(LimitsExceeded):
    """Raised when the agent has exceeded its wall-clock time limit."""
    # 智能体的实际运行时间超出限制时抛出。


class UserInterruption(InterruptAgentFlow):
    """Raised when the user interrupts the agent."""
    # 用户中断智能体时抛出。


class FormatError(InterruptAgentFlow):
    """Raised when the LM's output is not in the expected format."""
    # 语言模型的输出不符合预期格式时抛出。
