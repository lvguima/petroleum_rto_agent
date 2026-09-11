"""Versioned application state owned exclusively by the LangGraph checkpoint."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain.agents.middleware import AgentState
from langchain_core.messages import HumanMessage, message_to_dict, messages_from_dict
from pydantic import BaseModel, ConfigDict, Field

from petroleum_rto.domain_model.models import ModelSelection, model_profile
from petroleum_rto.rto.runtime.steady import context_ref, load_prepared_comparison


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SavedModel(Record):
    model_id: str
    thinking: Literal["default", "on", "off"]
    effort: str | None
    output_tokens: int = Field(gt=0)


class SavedContext(Record):
    summary: dict[str, Any] | None = None
    covered_until: str | None = None
    protected_id: str | None = None
    results: dict[str, str] = Field(default_factory=dict)
    summary_calls: int = Field(default=0, ge=0)
    total_summaries: int = Field(default=0, ge=0)
    last_estimate: int | None = Field(default=None, ge=0)


class SavedPlan(Record):
    ref: str
    version: int = Field(gt=0)
    prepared: dict[str, Any]
    displayed_turn: int | None = Field(default=None, ge=1)
    status: Literal[
        "awaiting_display", "awaiting_confirmation", "revision_required", "approved", "completed"
    ]
    result: dict[str, Any] | None = None


class SessionData(Record):
    schema_version: Literal["2.0.0"] = "2.0.0"
    model: SavedModel
    segment_start: int = Field(default=0, ge=0)
    turn_id: int = Field(default=0, ge=0)
    user_message: str = ""
    snapshots: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pending: SavedPlan | None = None
    last_result: dict[str, Any] | None = None
    plan_version: int = Field(default=0, ge=0)
    context: SavedContext = Field(default_factory=SavedContext)
    reported_summaries: int = Field(default=0, ge=0)
    awaiting_model_choice: bool = False
    action: Literal["chat", "review", "execute", "resume"] = "chat"


def merge_session(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Tools publish changed fields, never independently maintained session copies."""
    result = {**left, **right}
    if "snapshots" in right:
        result["snapshots"] = {**left.get("snapshots", {}), **right["snapshots"]}
    return result


class SessionState(AgentState[Any]):
    session: Annotated[dict[str, Any], merge_session]


def save_selection(selection: ModelSelection) -> dict[str, Any]:
    return SavedModel(
        model_id=selection.profile.model_id,
        thinking=selection.thinking,
        effort=selection.effort,
        output_tokens=selection.output_tokens,
    ).model_dump()


def read_selection(data: dict[str, Any]) -> ModelSelection:
    saved = SavedModel.model_validate(data)
    return ModelSelection(
        model_profile(saved.model_id), saved.thinking, saved.effort, saved.output_tokens
    )


def new_session(selection: ModelSelection) -> dict[str, Any]:
    return SessionData(model=SavedModel.model_validate(save_selection(selection))).model_dump()


def validate_session(data: Any, message_count: int) -> dict[str, Any]:
    """Validate disk state before it is made available to tools or model requests."""

    def complete(value: Any, schema: type[Record]) -> None:
        if not isinstance(value, dict) or set(value) != set(schema.model_fields):
            raise ValueError("saved session fields are missing or unknown")

    complete(data, SessionData)
    complete(data["model"], SavedModel)
    complete(data["context"], SavedContext)
    if data["pending"] is not None:
        complete(data["pending"], SavedPlan)
    saved = SessionData.model_validate(data)
    read_selection(saved.model.model_dump())
    if saved.segment_start > message_count:
        raise ValueError("session segment exceeds original messages")
    for fingerprint, value in saved.snapshots.items():
        if context_ref(value) != fingerprint:
            raise ValueError("snapshot fingerprint differs")
    if saved.pending is not None:
        plan = saved.pending
        prepared = load_prepared_comparison(plan.prepared)
        if plan.ref != f"plan-{plan.version}-{prepared.fingerprint[:12]}":
            raise ValueError("saved plan identity differs")
        if plan.version != saved.plan_version or (plan.displayed_turn or 0) > saved.turn_id:
            raise ValueError("saved plan version or display turn differs")
        if (
            plan.status in {"awaiting_confirmation", "approved", "completed"}
            and plan.displayed_turn is None
        ):
            raise ValueError("saved plan has not been displayed")
        if plan.result is not None and plan.status != "completed":
            raise ValueError("Uncompleted plan has a final result")
        if plan.status == "completed" and plan.result is None:
            raise ValueError("saved completed plan lacks a result")
    if saved.context.summary is not None:
        summary = messages_from_dict([saved.context.summary])[0]
        if not isinstance(summary, HumanMessage) or not summary.text.strip():
            raise ValueError("invalid saved summary")
    import hashlib

    for ref, text in saved.context.results.items():
        if ref != "tool-result-" + hashlib.sha256(text.encode()).hexdigest():
            raise ValueError("saved tool text fingerprint differs")
    return saved.model_dump()


def context_snapshot(component: Any) -> dict[str, Any]:
    return SavedContext(
        summary=message_to_dict(component.summary) if component.summary else None,
        **{key: getattr(component, key) for key in SavedContext.model_fields if key != "summary"},
    ).model_dump()


def restore_context(component: Any, data: dict[str, Any]) -> None:
    saved = SavedContext.model_validate(data)
    for key, value in saved.model_dump().items():
        setattr(
            component, key, messages_from_dict([value])[0] if key == "summary" and value else value
        )
