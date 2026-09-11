"""User-facing result shared by the terminal and agent implementations."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentTurn:
    outputs: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    should_exit: bool = False
    text_streamed: bool = False
    streamed_outputs: tuple[str, ...] = ()
