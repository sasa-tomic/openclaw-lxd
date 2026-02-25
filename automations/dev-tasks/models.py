"""Shared data models for dev-tasks."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PipelineState:
    """Tracks current position in the dev pipeline."""

    status: str = "idle"
    current_task_id: Optional[str] = None
    current_task_title: Optional[str] = None
    project: Optional[str] = None
    verify_attempts: int = 0
    max_verify_attempts: int = 3
    impl_session_key: Optional[str] = None
    verify_session_key: Optional[str] = None
    batch_started_at: Optional[str] = None
    completed_tasks: list = field(default_factory=list)
    failed_task: Optional[str] = None
    error_message: Optional[str] = None
    running_by: str = "unknown"
