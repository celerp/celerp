# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One job table for every AI task: ai_batch_jobs gains what a question job needs,
and a job's progress is its steps.

ai_batch_jobs gains kind (documents | question), mode (interactive | background),
question_id, started_at, promoted_at and cancel_requested_at. The steps of a job are
kept in ai_job_steps, one per file of a reading job: each existing job gets one step
per file from its stored per-file results (read: done, not read: failed; a file a job
still running had not reached: pending, which the next start sends under the id the
job already used for it), and the per-file list and the four counters
(total_files, completed_files, failed_files, credits_consumed) are dropped, since
the steps now say all of that. The assistant's tables are created at startup, not by
a revision, so a database that has never started the app has no ai_batch_jobs to
change. Existing rows are reading jobs that run in the background.

Revision ID: h4c5d6e7f8a9
Revises: h2a3b4c5d6e7
"""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "h4c5d6e7f8a9"
down_revision = "h2a3b4c5d6e7"
branch_labels = None
depends_on = None

def _columns() -> list[sa.Column]:
    return [
        sa.Column("kind", sa.String(16), nullable=False, server_default="documents"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="background"),
        sa.Column("question_id", sa.Uuid(as_uuid=True), nullable=True),
        *(sa.Column(name, sa.DateTime(timezone=True), nullable=True)
          for name in ("started_at", "promoted_at", "cancel_requested_at")),
    ]


_COUNTERS = ("total_files", "completed_files", "failed_files", "credits_consumed")
_RESULT_FIELDS = ("filename", "answer", "extraction", "error")

_jobs = sa.table(
    "ai_batch_jobs", sa.column("id", sa.Uuid(as_uuid=True)), sa.column("status", sa.String),
    sa.column("file_ids", sa.JSON), sa.column("results", sa.JSON),
    *(sa.column(name, sa.Integer) for name in _COUNTERS),
)
_steps = sa.table(
    "ai_job_steps", sa.column("id", sa.Uuid(as_uuid=True)), sa.column("job_id", sa.Uuid(as_uuid=True)),
    sa.column("seq", sa.Integer), sa.column("key", sa.String), sa.column("operation_id", sa.Uuid(as_uuid=True)),
    sa.column("step_id", sa.Uuid(as_uuid=True)), sa.column("generation", sa.Integer),
    sa.column("state", sa.String), sa.column("result", sa.JSON), sa.column("credits", sa.Integer),
    sa.column("charge_known", sa.Boolean), sa.column("updated_at", sa.DateTime(timezone=True)),
)


def _create_steps_table() -> None:
    op.create_table(
        "ai_job_steps",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("job_id", sa.Uuid(as_uuid=True), sa.ForeignKey("ai_batch_jobs.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("operation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("step_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("credits", sa.Integer, nullable=False),
        sa.Column("charge_known", sa.Boolean, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "key", name="uq_ai_job_steps_key"),
    )


def _steps_from_results(job) -> list[dict]:
    """One step per file of a job from its stored per-file results."""
    by_file = {entry.get("file_id"): entry for entry in (job.results or {}).get("files") or []}
    unreached = "pending" if job.status in ("pending", "running") else "failed"
    now = datetime.now(timezone.utc)
    out = []
    for seq, file_id in enumerate(dict.fromkeys(job.file_ids or [])):
        entry = by_file.get(file_id)
        op_id = uuid.uuid5(job.id, file_id)
        out.append({
            "id": uuid.uuid4(), "job_id": job.id, "seq": seq, "key": file_id, "operation_id": op_id,
            "step_id": op_id, "generation": 0,
            "state": unreached if entry is None else ("done" if entry.get("status") == "success" else "failed"),
            "result": None if entry is None else {k: entry.get(k) for k in _RESULT_FIELDS if entry.get(k) is not None},
            "credits": int((entry or {}).get("credits") or 0), "charge_known": False, "updated_at": now,
        })
    return out


def upgrade() -> None:
    """Adds the columns a table created at startup from the current model lacks and
    moves per-file progress into steps, so running it on either kind of table leaves
    the same schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("ai_batch_jobs"):
        return
    present = {c["name"] for c in inspector.get_columns("ai_batch_jobs")}
    for column in _columns():
        if column.name not in present:
            op.add_column("ai_batch_jobs", column)
    if not inspector.has_table("ai_job_steps"):
        _create_steps_table()
    stepped = set(bind.execute(sa.select(_steps.c.job_id).distinct()).scalars())
    for job in bind.execute(sa.select(_jobs.c.id, _jobs.c.status, _jobs.c.file_ids, _jobs.c.results)).all():
        if job.id not in stepped:
            rows = _steps_from_results(job)
            if rows:
                bind.execute(_steps.insert(), rows)
        if "files" in (job.results or {}):
            kept = {k: v for k, v in job.results.items() if k != "files"}
            bind.execute(_jobs.update().where(_jobs.c.id == job.id).values(results=kept or None))
    for name in _COUNTERS:
        if name in present:
            op.drop_column("ai_batch_jobs", name)


def downgrade() -> None:
    """Restores the counters and the per-file results from the steps, then drops them."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("ai_batch_jobs"):
        return
    present = {c["name"] for c in inspector.get_columns("ai_batch_jobs")}
    for name in _COUNTERS:
        if name not in present:
            op.add_column("ai_batch_jobs", sa.Column(name, sa.Integer, nullable=False, server_default="0"))
    if inspector.has_table("ai_job_steps"):
        steps: dict = {}
        for step in bind.execute(sa.select(_steps).order_by(_steps.c.job_id, _steps.c.seq)).all():
            steps.setdefault(step.job_id, []).append(step)
        for job in bind.execute(sa.select(_jobs.c.id, _jobs.c.results)).all():
            mine = steps.get(job.id, [])
            files = [{"file_id": s.key, "status": "success" if s.state == "done" else "error",
                      "credits": s.credits, **(s.result or {})} for s in mine if s.state not in ("pending", "running")]
            done = sum(1 for s in mine if s.state == "done")
            bind.execute(_jobs.update().where(_jobs.c.id == job.id).values(
                total_files=len(mine), completed_files=done, failed_files=len(files) - done,
                credits_consumed=sum(s.credits for s in mine), results={**(job.results or {}), "files": files}))
        op.drop_table("ai_job_steps")
    for column in _columns():
        if column.name in present:
            op.drop_column("ai_batch_jobs", column.name)
