from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Channel = Literal["standalone_web", "portal_sim", "wecom_sim"]


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    channel: Channel = "standalone_web"
    task_id: str | None = None


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str
    trace_id: str | None = None
    rating: Literal["helpful", "not_helpful"]
    reason: str | None = None
    comment: str | None = Field(default=None, max_length=1000)


class TaskCreateRequest(BaseModel):
    scenario_id: str
    channel: Channel = "standalone_web"
    actor_type: Literal["demo_student", "demo_provider", "demo_reviewer"] = "demo_student"
    form_data: dict[str, Any] = Field(default_factory=dict)


class StudentTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    channel: Channel = "standalone_web"
    form_data: dict[str, Any] = Field(default_factory=dict)


class TaskDataRequest(BaseModel):
    form_data: dict[str, Any] = Field(default_factory=dict)
    reference_date: str | None = Field(default=None, min_length=10, max_length=10)


class TaskMaterialsRequest(BaseModel):
    materials: dict[str, Any] = Field(default_factory=dict)
    form_data: dict[str, Any] | None = None
    expected_version: int = Field(ge=1)


class ConfirmRequest(BaseModel):
    idempotency_key: str = Field(min_length=4, max_length=120)
    confirmed: bool = True
    expected_version: int | None = Field(default=None, ge=1)
    preview_version: int | None = Field(default=None, ge=1)


class ReconfirmationRequest(BaseModel):
    impact_event_id: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=4, max_length=120)
    confirmed: bool = True
    expected_version: int = Field(ge=1)


class StudentReconfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: bool = True
    expected_version: int = Field(ge=1)


class AdvanceRequest(BaseModel):
    target_status: Literal[
        "under_simulated_review",
        "correction_required",
        "completed_simulated",
        "rejected_simulated",
        "approved_simulated",
        "leave_closed_simulated",
        "awaiting_offline_confirmation",
        "archive_pending",
    ]
    actor_type: Literal["demo_student", "demo_provider", "demo_reviewer"] = "demo_reviewer"
    reason: str = Field(default="", max_length=1000)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=120)


class GovernanceAdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: Literal[
        "under_simulated_review",
        "correction_required",
        "completed_simulated",
        "rejected_simulated",
        "approved_simulated",
        "leave_closed_simulated",
        "awaiting_offline_confirmation",
        "archive_pending",
    ]
    reason: str = Field(default="", max_length=1000)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=4, max_length=120)


class LeaveCloseRequest(BaseModel):
    return_date: str = Field(min_length=10, max_length=20)
    confirmed: bool = True


class WorkStudyJobCreateRequest(BaseModel):
    actor_type: Literal["demo_provider"] = "demo_provider"
    title: str = Field(min_length=2, max_length=120)
    department: str = Field(min_length=2, max_length=120)
    location: str = Field(min_length=2, max_length=120)
    schedule: str = Field(min_length=2, max_length=200)
    stipend: str = Field(min_length=2, max_length=120)
    qualification: str = Field(min_length=2, max_length=300)
    deadline: str = Field(min_length=10, max_length=20)
    internal_note: str = Field(default="", max_length=500)


class WorkStudyJobUpdateRequest(BaseModel):
    actor_type: Literal["demo_provider"] = "demo_provider"
    title: str | None = Field(default=None, min_length=2, max_length=120)
    department: str | None = Field(default=None, min_length=2, max_length=120)
    location: str | None = Field(default=None, min_length=2, max_length=120)
    schedule: str | None = Field(default=None, min_length=2, max_length=200)
    stipend: str | None = Field(default=None, min_length=2, max_length=120)
    qualification: str | None = Field(default=None, min_length=2, max_length=300)
    deadline: str | None = Field(default=None, min_length=10, max_length=20)
    internal_note: str | None = Field(default=None, max_length=500)


class WorkStudyJobTransitionRequest(BaseModel):
    actor_type: Literal["demo_provider"] = "demo_provider"
    target_status: Literal["under_review", "correction_required", "published", "closed"]


class AnnouncementChangeRequest(BaseModel):
    source_id: str = "src-venue-v1"
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=120)


class SourceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    review_note: str = Field(min_length=2, max_length=1000)
    expected_version: int = Field(ge=1)


class PublicationBusinessReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    review_note: str = Field(min_length=2, max_length=1000)
    expected_version: int = Field(ge=1)


class PublicationActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_note: str = Field(min_length=2, max_length=1000)
    expected_version: int = Field(ge=1)


class PublicationRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=1000)
    expected_version: int = Field(ge=1)


class EvaluationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str = "core12-v1"
    publication_id: str | None = Field(default=None, min_length=1, max_length=120)
    model_profile: str = Field(default="deterministic-no-llm-v1", min_length=1, max_length=120)
    runtime_profile: str = "deterministic-agent-v1"
    environment: Literal["mock"] = "mock"
    run_mode: Literal["smoke", "regression", "release", "replay", "diagnostic"] = "release"
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=120)
    bad_case_id: str | None = Field(default=None, min_length=1, max_length=120)
    judge_profile: str | None = Field(default=None, min_length=1, max_length=120)


class EvaluationRunControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class EvaluationHumanReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_scope: Literal["technical", "business"]
    decision: Literal["approve", "reject", "uncertain", "needs_review"]
    note: str = Field(min_length=1, max_length=1000)
    expected_version: int = Field(ge=1)


class BadCaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: Literal["user_feedback", "evaluation", "source_change", "security_test", "manual_review"] = "manual_review"
    input_text: str = Field(min_length=1, max_length=2000)
    sanitized_input: str | None = Field(default=None, max_length=2000)
    trace_id: str | None = Field(default=None, max_length=120)
    publication_id: str | None = Field(default=None, max_length=120)
    category: str = Field(min_length=1, max_length=120)
    severity: Literal["P0", "P1", "P2"] = "P1"
    expected: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)
    owner_role: Literal["evaluation_engineer", "business_knowledge_reviewer"] = "evaluation_engineer"


class BadCaseReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class BadCaseRepairRequest(BaseModel):
    repair_note: str = Field(min_length=1, max_length=1000)
    candidate_fix_type: Literal["knowledge", "rule", "retrieval", "context", "prompt", "contract", "code", "dataset", "none"] = "code"
    candidate_fix_ref: str | None = Field(default=None, max_length=240)


class BadCaseCloseRequest(BaseModel):
    close_note: str = Field(min_length=1, max_length=1000)
