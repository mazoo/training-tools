import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TaskStatus:
    task_id: str
    status: str = "running"  # running | done | error | rate_limited
    activities_processed: int = 0
    activities_total: int = 0
    strava_calls_made: int = 0
    error: str | None = None
    retry_after: datetime | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_tasks: dict[str, TaskStatus] = {}


def create_task() -> TaskStatus:
    task_id = str(uuid.uuid4())[:8]
    task = TaskStatus(task_id=task_id)
    _tasks[task_id] = task
    return task


def get_task(task_id: str) -> TaskStatus | None:
    return _tasks.get(task_id)
