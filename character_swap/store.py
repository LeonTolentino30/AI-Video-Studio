"""SQLite persistence for Character Swap, independent of text-to-video tables.

All spending values are integer estimate units chosen by the caller (for
example, microdollars). Reservations are not actual provider charges. They
remain counted even after failure: this foundation never assumes a refund.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import re
from pathlib import Path
import sqlite3
import uuid

from .providers.base import CharacterEditRequest


class SafetyError(Exception):
    """A local authorization, budget, or job-state check rejected an action."""


@dataclass(frozen=True)
class Job:
    id: str
    idempotency_key: str
    provider: str
    model: str
    request: CharacterEditRequest
    prepared_request: CharacterEditRequest | None
    status: str
    provider_task_id: str | None
    estimated_units: int
    reserved_units: int
    error_code: str | None
    error_message: str | None
    video_url: str | None
    revision: int
    owner_id: str | None = None


class JobStore:
    """Use an explicitly supplied persistent SQLite file, not an in-memory DB."""

    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("A persistent SQLite file is required")
        self.path = str(path)
        with self._transaction() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS character_swap_jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    prepared_json TEXT,
                    status TEXT NOT NULL,
                    provider_task_id TEXT,
                    estimated_units INTEGER NOT NULL CHECK (estimated_units > 0),
                    reserved_units INTEGER NOT NULL DEFAULT 0 CHECK (reserved_units >= 0),
                    error_code TEXT,
                    error_message TEXT,
                    video_url TEXT,
                    revision INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(character_swap_jobs)")}
            if "owner_id" not in columns:
                connection.execute("ALTER TABLE character_swap_jobs ADD COLUMN owner_id TEXT")

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _job(row) -> Job:
        if row is None:
            raise SafetyError("Character Swap job not found")
        values = dict(row)
        values["request"] = CharacterEditRequest(**json.loads(values.pop("request_json")))
        prepared = values.pop("prepared_json")
        values["prepared_request"] = CharacterEditRequest(**json.loads(prepared)) if prepared else None
        return Job(**values)

    @staticmethod
    def encode_request(request: CharacterEditRequest) -> str:
        return json.dumps(asdict(request), sort_keys=True)

    def get(self, job_id: str) -> Job:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            return self._job(connection.execute(
                "SELECT * FROM character_swap_jobs WHERE id = ?", (job_id,)
            ).fetchone())
        finally:
            connection.close()

    def get_owned(self, job_id: str, owner_id: str) -> Job:
        """Never claim legacy rows or disclose another session's job."""
        if not isinstance(owner_id, str) or not re.fullmatch(r"[0-9a-f]{64}", owner_id):
            raise SafetyError("Character Swap job not found")
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            return self._job(connection.execute(
                "SELECT * FROM character_swap_jobs WHERE id = ? AND owner_id = ?",
                (job_id, owner_id),
            ).fetchone())
        finally:
            connection.close()

    def create(self, key: str, provider: str, model: str,
               request: CharacterEditRequest, estimated_units: int, *, owner_id: str | None = None) -> Job:
        # None preserves trusted offline callers; ownerless jobs are never public.
        if owner_id is not None and (not isinstance(owner_id, str) or not re.fullmatch(r"[0-9a-f]{64}", owner_id)):
            raise SafetyError("Invalid job owner")
        if not all(isinstance(value, str) and value.strip() for value in (key, provider, model)):
            raise SafetyError("Idempotency key, provider, and model are required")
        if type(estimated_units) is not int or not 0 < estimated_units <= 2**63 - 1:
            raise SafetyError("Estimate must be a positive SQLite integer")
        encoded = self.encode_request(request)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM character_swap_jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row:
                if (row["provider"], row["model"], row["request_json"], row["estimated_units"], row["owner_id"]) != (
                    provider, model, encoded, estimated_units, owner_id
                ):
                    raise SafetyError("Idempotency key already belongs to a different request")
                return self._job(row)
            job_id = uuid.uuid4().hex
            connection.execute("""
                INSERT INTO character_swap_jobs
                    (id, idempotency_key, provider, model, request_json, status, estimated_units, owner_id)
                VALUES (?, ?, ?, ?, ?, 'created', ?, ?)
            """, (job_id, key, provider, model, encoded, estimated_units, owner_id))
            return self._job(connection.execute(
                "SELECT * FROM character_swap_jobs WHERE id = ?", (job_id,)
            ).fetchone())

    @staticmethod
    def _check_budget(connection, job: Job, per_job: int, total: int, *, adding: bool):
        # Sum in Python to avoid SQLite SUM overflow on many large reservations.
        reserved = sum(row[0] for row in connection.execute(
            "SELECT reserved_units FROM character_swap_jobs"
        ))
        if job.estimated_units > per_job or reserved + (job.estimated_units if adding else 0) > total:
            raise SafetyError("Character Swap spending limit exceeded")

    def authorize(self, job_id: str, per_job: int, total: int) -> Job:
        with self._transaction() as connection:
            job = self._job(connection.execute(
                "SELECT * FROM character_swap_jobs WHERE id = ?", (job_id,)
            ).fetchone())
            self._check_budget(connection, job, per_job, total, adding=job.status == "created")
            if job.status == "created":
                connection.execute("""
                    UPDATE character_swap_jobs SET status = 'authorized',
                        reserved_units = estimated_units, revision = revision + 1 WHERE id = ?
                """, (job_id,))
            return self._job(connection.execute(
                "SELECT * FROM character_swap_jobs WHERE id = ?", (job_id,)
            ).fetchone())

    def claim(self, job_id: str, allowed: tuple[str, ...], state: str,
              per_job: int, total: int) -> tuple[Job, bool]:
        """Claim a provider operation exactly once before leaving the transaction."""
        with self._transaction() as connection:
            job = self._job(connection.execute(
                "SELECT * FROM character_swap_jobs WHERE id = ?", (job_id,)
            ).fetchone())
            if job.status == "created":
                raise SafetyError("Explicit server-side authorization is required")
            if job.status not in allowed:
                return job, False
            if job.reserved_units != job.estimated_units:
                raise SafetyError("Job has no spending reservation")
            self._check_budget(connection, job, per_job, total, adding=False)
            connection.execute("""
                UPDATE character_swap_jobs SET status = ?, revision = revision + 1 WHERE id = ?
            """, (state, job_id))
            return self._job(connection.execute(
                "SELECT * FROM character_swap_jobs WHERE id = ?", (job_id,)
            ).fetchone()), True

    def update(self, job: Job, **changes) -> Job:
        """Compare-and-set prevents stale polling from overwriting newer results."""
        permitted = {"prepared_json", "status", "provider_task_id", "error_code", "error_message", "video_url"}
        if not changes or not changes.keys() <= permitted:
            raise ValueError("Invalid job update")
        assignments = ", ".join(f"{name} = ?" for name in changes)
        with self._transaction() as connection:
            connection.execute(
                f"UPDATE character_swap_jobs SET {assignments}, revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (*changes.values(), job.id, job.revision),
            )
            return self._job(connection.execute(
                "SELECT * FROM character_swap_jobs WHERE id = ?", (job.id,)
            ).fetchone())
