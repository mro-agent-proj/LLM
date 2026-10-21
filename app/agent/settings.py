import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    tool_timeout: float = 10
    tool_retries: int = 1
    model_timeout: float = 45
    max_tool_calls: int = 8
    max_reinvestigation: int = 2

    def __post_init__(self) -> None:
        if self.tool_timeout <= 0 or self.model_timeout <= 0:
            raise ValueError("Timeouts must be positive")
        if self.tool_retries < 0 or self.max_reinvestigation < 0 or self.max_tool_calls < 1:
            raise ValueError("Invalid retry/call limits")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            tool_timeout=float(os.getenv("TOOL_TIMEOUT_SECONDS", "10")),
            tool_retries=int(os.getenv("TOOL_RETRIES", "1")),
            model_timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "45")),
            max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "8")),
            max_reinvestigation=int(os.getenv("MAX_REINVESTIGATION", "2")),
        )
