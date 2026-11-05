"""SQLite 任务账本：job_sets / templates / tasks / events（契约 §6）。

关键语义：

* **幂等建 job_set**：同一 ``job_set_id`` + 同一 ``plan_hash`` → ``created=False``；
* **跨活动任务集去重**：已有相同 ``plan_hash`` 的 pending/running/paused 任务集时直接复用；
* **原子领取**：``claim_next_task`` 在同一事务内检查 in-flight 配额并占用下一条 pending；
* **状态机**：pending → submitted → running → completed / failed / cancelled / unknown；
* **恢复对账**：``recover(recovered_outputs=...)`` 有产物直接 completed（已完成不重做），
  无产物且不在队列中 → unknown 等 resume；
* **恢复后重试**：``retry_failed`` 把 failed/unknown 重置为 pending。

WAL + foreign_keys + busy_timeout；连接用 ``check_same_thread=False`` + 进程内锁，
同时服务 aiohttp 线程池与 runner 的同步 tick。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import HARD_MAX_IN_FLIGHT
from . import config as config_module
from . import planner

SCHEMA_VERSION = 1
LEDGER_SCHEMA = "aiksk.pc.ledger.v1"
MANIFEST_SCHEMA = "aiksk.pc.manifest.v1"

TASK_STATUSES = ("pending", "submitted", "running", "completed", "failed", "cancelled", "unknown")
_UNSET = object()
TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled")
ACTIVE_TASK_STATUSES = ("submitted", "running")
JOB_STATUSES = ("pending", "running", "paused", "completed", "failed", "cancelled")
ACTIVE_JOB_STATUSES = ("pending", "running", "paused")
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_sets (
        id TEXT PRIMARY KEY,
        plan_hash TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        templates_json TEXT NOT NULL,
        status TEXT NOT NULL,
        mode TEXT,
        graph_model TEXT,
        dual_compare INTEGER NOT NULL DEFAULT 0,
        pair_count INTEGER NOT NULL DEFAULT 0,
        task_count INTEGER NOT NULL DEFAULT 0,
        completed_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        in_flight INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS templates (
        job_set_id TEXT NOT NULL REFERENCES job_sets(id) ON DELETE CASCADE,
        model_key TEXT NOT NULL,
        node_id TEXT,
        prompt_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_set_id, model_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_key TEXT PRIMARY KEY,
        job_set_id TEXT NOT NULL REFERENCES job_sets(id) ON DELETE CASCADE,
        order_index INTEGER NOT NULL,
        task_json TEXT NOT NULL,
        status TEXT NOT NULL,
        prompt_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        output_paths TEXT,
        save_prefixes TEXT,
        node_id TEXT,
        error_code TEXT,
        error_message TEXT,
        enqueued_at TEXT,
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_job_status ON tasks(job_set_id, status, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_prompt ON tasks(prompt_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON job_sets(status, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_set_id TEXT,
        task_key TEXT,
        kind TEXT NOT NULL,
        message TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_set_id, id)",
)


class LedgerError(ValueError):
    """带错误码的账本错误。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover
        return self.message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(text: Any, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _counts_from_rows(rows: Iterable[sqlite3.Row]) -> dict:
    counts = {status: 0 for status in TASK_STATUSES}
    for row in rows:
        counts[row["status"]] = int(row["n"])
    counts["total"] = sum(counts[status] for status in TASK_STATUSES)
    return counts


class Ledger:
    """线程安全的 SQLite 账本。"""

    def __init__(self, path: str | Path | None = None, *, config: config_module.Config | None = None) -> None:
        self.config = config or config_module.get_config()
        self.path = Path(path).expanduser() if path else self.config.resolve_ledger_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:  # pragma: no cover - 特殊文件系统
                pass
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self.init_schema()

    # -- 基础 ----------------------------------------------------------

    def init_schema(self) -> None:
        with self._lock:
            for statement in _DDL:
                self._conn.execute(statement)
            row = self._conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                try:
                    existing = int(row["value"])
                except (TypeError, ValueError):
                    existing = 0
                if existing > SCHEMA_VERSION:
                    raise LedgerError(
                        "LEDGER_VERSION",
                        f"账本版本 {existing} 高于本插件支持的 {SCHEMA_VERSION}，请升级插件",
                    )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _read(self) -> sqlite3.Connection:
        return self._conn

    # -- 事件 ----------------------------------------------------------

    def record_event(self, kind: str, message: str = "", *, job_set_id: str | None = None,
                     task_key: str | None = None, conn: sqlite3.Connection | None = None) -> None:
        timestamp = utc_now()
        if conn is not None:
            conn.execute(
                "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                (job_set_id, task_key, str(kind), str(message)[:4000], timestamp),
            )
            return
        with self._tx() as own:
            own.execute(
                "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                (job_set_id, task_key, str(kind), str(message)[:4000], timestamp),
            )

    def events(self, job_set_id: str | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if job_set_id:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE job_set_id=? ORDER BY id DESC LIMIT ?",
                    (job_set_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- job_set -------------------------------------------------------

    def create_job_set(
        self,
        plan: Mapping[str, Any],
        templates: Mapping[str, Any] | None = None,
        *,
        job_set_id: str | None = None,
        in_flight: int | None = None,
        start: bool = False,
    ) -> dict:
        """幂等创建任务集（契约 §6）。

        返回 ``{job_set_id, created, deduplicated, counts, status, plan_hash}``。
        """
        normalized_plan = planner.validate_plan(plan)
        normalized_templates = planner.normalize_templates(templates or {})
        plan_hash = planner.plan_hash(normalized_plan, normalized_templates)
        counts = planner.plan_counts(normalized_plan)
        planner.validate_limits(counts)
        base_id = job_set_id or f"pcjs_{plan_hash[:16]}"
        if not isinstance(base_id, str) or not base_id:
            raise LedgerError("JOB_INVALID", "job_set_id 必须是非空字符串")
        in_flight = max(1, min(HARD_MAX_IN_FLIGHT, int(in_flight or self.config.max_in_flight or 1)))
        timestamp = utc_now()
        with self._tx() as conn:
            existing = conn.execute("SELECT * FROM job_sets WHERE id=?", (base_id,)).fetchone()
            if existing is not None:
                if existing["plan_hash"] != plan_hash:
                    raise LedgerError("JOB_CONFLICT", f"任务集 ID 冲突（{base_id}），请换一个 job_set_id")
                if existing["status"] in ACTIVE_JOB_STATUSES:
                    return {
                        "job_set_id": base_id,
                        "created": False,
                        "deduplicated": True,
                        "counts": counts,
                        "status": existing["status"],
                        "plan_hash": plan_hash,
                    }
                # 相同计划已结束（completed/failed/cancelled）：允许重跑，落到下面生成后缀 ID。
            for row in conn.execute(
                "SELECT id, status FROM job_sets WHERE plan_hash=? ORDER BY created_at",
                (plan_hash,),
            ).fetchall():
                if row["status"] in ACTIVE_JOB_STATUSES:
                    return {
                        "job_set_id": row["id"],
                        "created": False,
                        "deduplicated": True,
                        "counts": counts,
                        "status": row["status"],
                        "plan_hash": plan_hash,
                    }
            # 相同计划历史已结束：允许重跑，生成不冲突的新 ID。
            final_id = base_id
            if conn.execute("SELECT 1 FROM job_sets WHERE id=?", (final_id,)).fetchone():
                index = 2
                while conn.execute("SELECT 1 FROM job_sets WHERE id=?", (f"{base_id}_{index}",)).fetchone():
                    index += 1
                final_id = f"{base_id}_{index}"
            tasks = planner.expand_tasks(normalized_plan, job_id=final_id)
            status = "running" if start else "pending"
            conn.execute(
                """
                INSERT INTO job_sets(id, plan_hash, plan_json, templates_json, status, mode, graph_model,
                                     dual_compare, pair_count, task_count, in_flight, created_at, updated_at, started_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    final_id,
                    plan_hash,
                    _json_dumps(normalized_plan),
                    _json_dumps(normalized_templates),
                    status,
                    normalized_plan["pair_mode"],
                    normalized_plan["graph_model"],
                    1 if normalized_plan["dual_compare"] else 0,
                    normalized_plan["pair_count"],
                    counts["tasks"],
                    in_flight,
                    timestamp,
                    timestamp,
                    timestamp if start else None,
                ),
            )
            for model_key, template in normalized_templates.items():
                conn.execute(
                    "INSERT INTO templates(job_set_id, model_key, node_id, prompt_json, created_at) VALUES(?,?,?,?,?)",
                    (final_id, model_key, template.get("node_id"), _json_dumps(template["prompt"]), timestamp),
                )
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO tasks(task_key, job_set_id, order_index, task_json, status, attempts, updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (task["task_key"], final_id, int(task["index"]), _json_dumps(task), "pending", 0, timestamp),
                )
            conn.execute(
                "INSERT INTO events(job_set_id, kind, message, created_at) VALUES(?,?,?,?)",
                (final_id, "job_created", f"任务集创建：{counts['tasks']} 个任务 / {counts['pairs']} 组", timestamp),
            )
        self.record_event("job_created", f"{final_id} {status}", job_set_id=final_id)
        return {
            "job_set_id": final_id,
            "created": True,
            "deduplicated": False,
            "counts": counts,
            "status": status,
            "plan_hash": plan_hash,
        }

    def job_set_row(self, job_set_id: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM job_sets WHERE id=?", (job_set_id,)).fetchone()
            if row is None:
                raise LedgerError("JOB_NOT_FOUND", f"任务集不存在：{job_set_id}")
            return dict(row)

    def job_exists(self, job_set_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM job_sets WHERE id=?", (job_set_id,)).fetchone() is not None

    def get_job_set(self, job_set_id: str) -> dict:
        row = self.job_set_row(job_set_id)
        plan = _json_loads(row.pop("plan_json"), {})
        templates_json = row.pop("templates_json", "{}")
        templates = _json_loads(templates_json, {})
        counts = self.task_counts(job_set_id)
        return {
            "job_set_id": row["id"],
            "id": row["id"],
            "status": row["status"],
            "plan": plan,
            "templates": {key: {"node_id": value.get("node_id")} for key, value in (templates or {}).items()},
            "counts": counts,
            "plan_hash": row["plan_hash"],
            "mode": row["mode"],
            "graph_model": row["graph_model"],
            "dual_compare": bool(row["dual_compare"]),
            "pair_count": int(row["pair_count"]),
            "task_count": int(row["task_count"]),
            "in_flight": int(row["in_flight"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
        }

    def list_job_sets(self, limit: int = 50, status: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if status:
                if status not in JOB_STATUSES:
                    raise LedgerError("STATUS_INVALID", f"未知任务集状态：{status}")
                rows = self._conn.execute(
                    "SELECT id, status, mode, graph_model, dual_compare, pair_count, task_count, "
                    "completed_count, failed_count, created_at, updated_at, finished_at, error "
                    "FROM job_sets WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, status, mode, graph_model, dual_compare, pair_count, task_count, "
                    "completed_count, failed_count, created_at, updated_at, finished_at, error "
                    "FROM job_sets ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["job_set_id"] = item.pop("id")
            result.append(item)
        return result

    def set_job_status(self, job_set_id: str, status: str, *, error: str | None = None,
                       conn: sqlite3.Connection | None = None) -> dict:
        if status not in JOB_STATUSES:
            raise LedgerError("STATUS_INVALID", f"未知任务集状态：{status}")
        timestamp = utc_now()
        finished = timestamp if status in TERMINAL_JOB_STATUSES else None

        def _apply(connection: sqlite3.Connection) -> dict:
            row = connection.execute("SELECT * FROM job_sets WHERE id=?", (job_set_id,)).fetchone()
            if row is None:
                raise LedgerError("JOB_NOT_FOUND", f"任务集不存在：{job_set_id}")
            connection.execute(
                "UPDATE job_sets SET status=?, updated_at=?, finished_at=COALESCE(?, finished_at), "
                "error=COALESCE(?, error), started_at=CASE WHEN ?='running' AND started_at IS NULL "
                "THEN ? ELSE started_at END WHERE id=?",
                (status, timestamp, finished, error, status, timestamp, job_set_id),
            )
            connection.execute(
                "INSERT INTO events(job_set_id, kind, message, created_at) VALUES(?,?,?,?)",
                (job_set_id, "job_status", f"{row['status']} → {status}", timestamp),
            )
            updated = connection.execute("SELECT * FROM job_sets WHERE id=?", (job_set_id,)).fetchone()
            return dict(updated)

        if conn is not None:
            return _apply(conn)
        with self._tx() as own:
            return _apply(own)

    def pause_job(self, job_set_id: str) -> dict:
        row = self.job_set_row(job_set_id)
        if row["status"] not in ("pending", "running"):
            raise LedgerError("STATUS_INVALID", f"当前状态 {row['status']} 不能暂停")
        return self.set_job_status(job_set_id, "paused")

    def resume_job(self, job_set_id: str) -> dict:
        row = self.job_set_row(job_set_id)
        if row["status"] not in ("pending", "paused", "failed"):
            raise LedgerError("STATUS_INVALID", f"当前状态 {row['status']} 不能恢复")
        return self.set_job_status(job_set_id, "running")

    def cancel_job(self, job_set_id: str) -> dict:
        """取消任务集：未开始/已提交任务置 cancelled，返回需要中断的 prompt_id。"""
        timestamp = utc_now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM job_sets WHERE id=?", (job_set_id,)).fetchone()
            if row is None:
                raise LedgerError("JOB_NOT_FOUND", f"任务集不存在：{job_set_id}")
            if row["status"] in TERMINAL_JOB_STATUSES:
                raise LedgerError("STATUS_INVALID", f"任务集已结束（{row['status']}），不能取消")
            inflight = conn.execute(
                "SELECT task_key, prompt_id FROM tasks WHERE job_set_id=? AND status IN ('submitted','running')",
                (job_set_id,),
            ).fetchall()
            conn.execute(
                "UPDATE tasks SET status='cancelled', updated_at=?, error_code='CANCELLED', "
                "error_message='用户取消', finished_at=? WHERE job_set_id=? AND status IN "
                "('pending','submitted','running','unknown','failed')",
                (timestamp, timestamp, job_set_id),
            )
            conn.execute(
                "UPDATE job_sets SET status='cancelled', updated_at=?, finished_at=? WHERE id=?",
                (timestamp, timestamp, job_set_id),
            )
            conn.execute(
                "INSERT INTO events(job_set_id, kind, message, created_at) VALUES(?,?,?,?)",
                (job_set_id, "job_cancelled", "用户取消任务集", timestamp),
            )
            self._sync_counts(conn, job_set_id)
        return {"job_set_id": job_set_id, "status": "cancelled",
                "interrupt_prompt_ids": [row["prompt_id"] for row in inflight if row["prompt_id"]]}

    # -- 任务 ----------------------------------------------------------

    def task_counts(self, job_set_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE job_set_id=? GROUP BY status",
                (job_set_id,),
            ).fetchall()
        return _counts_from_rows(rows)

    def task_row(self, task_key: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_key=?", (task_key,)).fetchone()
        if row is None:
            raise LedgerError("TASK_NOT_FOUND", f"任务不存在：{task_key}")
        return self._task_dict(row)

    def _task_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        data["task"] = _json_loads(data.pop("task_json"), {})
        data["output_paths"] = _json_loads(data.pop("output_paths"), []) or []
        data["save_prefixes"] = _json_loads(data.pop("save_prefixes"), {}) or {}
        return data

    def list_tasks(self, job_set_id: str, limit: int = 200, offset: int = 0,
                   status: str | None = None) -> list[dict]:
        limit = max(0, min(int(limit), 1000))
        offset = max(0, int(offset))
        params: list[Any] = [job_set_id]
        clause = ""
        if status:
            if status not in TASK_STATUSES:
                raise LedgerError("STATUS_INVALID", f"未知任务状态：{status}")
            clause = " AND status=?"
            params.append(status)
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE job_set_id=?{clause} ORDER BY order_index LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def next_pending_task(self, job_set_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE job_set_id=? AND status='pending' ORDER BY order_index LIMIT 1",
                (job_set_id,),
            ).fetchone()
        return self._task_dict(row) if row else None

    def claim_next_task(self, job_set_id: str, *, max_in_flight: int | None = None,
                        prompt_id: str | None = None, node_id: str | None = None,
                        save_prefixes: Mapping[str, str] | None = None) -> dict | None:
        """原子领取一条 pending 任务；可同时持久化 prompt_id（审计 P0-2 的更强保证）。

        prompt_id 在同一个事务内与「领取」一起写入，避免“领取后、预登记前”的并发窗口。
        """
        timestamp = utc_now()
        with self._tx() as conn:
            job = conn.execute("SELECT status, in_flight FROM job_sets WHERE id=?", (job_set_id,)).fetchone()
            if job is None:
                raise LedgerError("JOB_NOT_FOUND", f"任务集不存在：{job_set_id}")
            if job["status"] != "running":
                return None
            limit = max(1, min(HARD_MAX_IN_FLIGHT, int(max_in_flight or job["in_flight"] or 1)))
            inflight = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE job_set_id=? AND status IN ('submitted','running')",
                (job_set_id,),
            ).fetchone()["n"]
            if int(inflight) >= limit:
                return None
            row = conn.execute(
                "SELECT * FROM tasks WHERE job_set_id=? AND status='pending' ORDER BY order_index LIMIT 1",
                (job_set_id,),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                "UPDATE tasks SET status='submitted', attempts=attempts+1, prompt_id=?, "
                "node_id=COALESCE(?, node_id), save_prefixes=COALESCE(?, save_prefixes), "
                "enqueued_at=?, updated_at=? WHERE task_key=? AND status='pending'",
                (str(prompt_id) if prompt_id else None, node_id,
                 _json_dumps(dict(save_prefixes)) if save_prefixes else None,
                 timestamp, timestamp, row["task_key"]),
            )
            if cursor.rowcount != 1:  # pragma: no cover - 并发保护
                return None
            updated = conn.execute("SELECT * FROM tasks WHERE task_key=?", (row["task_key"],)).fetchone()
            conn.execute(
                "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                (job_set_id, row["task_key"], "task_claimed", f"领取任务 #{row['order_index']}", timestamp),
            )
            return self._task_dict(updated)

    def prepare_submission(self, task_key: str, prompt_id: str, *, node_id: str | None = None,
                           save_prefixes: Mapping[str, str] | None = None) -> bool:
        """入队前先持久化确定性 prompt_id（审计 P0-2）。

        崩溃在 ``queue.put`` 之前时，重启对账可安全重投；崩溃在 put 之后时，
        该 prompt_id 会出现在队列/历史中，对账可精确接续，不会重复提交。
        """
        timestamp = utc_now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_key=?", (task_key,)).fetchone()
            if row is None:
                raise LedgerError("TASK_NOT_FOUND", f"任务不存在：{task_key}")
            if row["prompt_id"] and row["prompt_id"] != prompt_id:
                raise LedgerError("PROMPT_CONFLICT", f"任务已有 prompt_id：{row['prompt_id']}")
            if row["status"] not in ("submitted", "running"):
                raise LedgerError("STATUS_INVALID", f"任务状态不允许预登记 prompt_id：{row['status']}")
            conn.execute(
                "UPDATE tasks SET prompt_id=?, node_id=COALESCE(?, node_id), "
                "save_prefixes=COALESCE(?, save_prefixes), updated_at=? WHERE task_key=?",
                (str(prompt_id), node_id,
                 _json_dumps(dict(save_prefixes)) if save_prefixes else None, timestamp, task_key),
            )
            conn.execute(
                "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                (row["job_set_id"], task_key, "task_prepared", f"prompt_id={prompt_id}", timestamp),
            )
            return True

    def reset_for_retry(self, task_key: str, message: str = "对账后重新入队", *,
                        expected_prompt_id: str | None | object = _UNSET) -> bool:
        """把 submitted/unknown 任务放回 pending，保留 attempts，清空 prompt 关联。

        ``expected_prompt_id`` 用于并发对账：只有当前 prompt_id 仍与快照一致时才重置，
        避免陈旧的 recover 把已被新 tick 领取的任务再次打回 pending（审计 P0-2/P0-3）。
        """
        timestamp = utc_now()
        with self._tx() as conn:
            row = conn.execute("SELECT status, job_set_id, prompt_id FROM tasks WHERE task_key=?", (task_key,)).fetchone()
            if row is None:
                raise LedgerError("TASK_NOT_FOUND", f"任务不存在：{task_key}")
            if row["status"] not in ("submitted", "running", "unknown"):
                return False
            if expected_prompt_id is not _UNSET and (row["prompt_id"] or None) != (expected_prompt_id or None):
                return False
            conn.execute(
                "UPDATE tasks SET status='pending', prompt_id=NULL, save_prefixes=NULL, "
                "enqueued_at=NULL, started_at=NULL, error_code=NULL, error_message=NULL, updated_at=? "
                "WHERE task_key=?",
                (timestamp, task_key),
            )
            conn.execute(
                "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                (row["job_set_id"], task_key, "task_requeued", str(message)[:500], timestamp),
            )
            self._sync_counts(conn, row["job_set_id"])
            return True

    def mark_running(self, task_key: str, prompt_id: str, *, node_id: str | None = None,
                     save_prefixes: Mapping[str, str] | None = None) -> bool:
        timestamp = utc_now()
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status='running', prompt_id=?, node_id=COALESCE(?, node_id), "
                "save_prefixes=COALESCE(?, save_prefixes), started_at=?, updated_at=? WHERE task_key=?",
                (str(prompt_id), node_id, _json_dumps(dict(save_prefixes)) if save_prefixes else None,
                 timestamp, timestamp, task_key),
            )
            row = conn.execute("SELECT job_set_id FROM tasks WHERE task_key=?", (task_key,)).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                    (row["job_set_id"], task_key, "task_submitted", f"prompt_id={prompt_id}", timestamp),
                )
            return cursor.rowcount == 1

    def mark_completed(self, task_key: str, output_paths: Iterable[str] = (), *,
                       prompt_id: str | None = None) -> bool:
        timestamp = utc_now()
        paths = [str(path) for path in output_paths]
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_key=?", (task_key,)).fetchone()
            if row is None:
                raise LedgerError("TASK_NOT_FOUND", f"任务不存在：{task_key}")
            conn.execute(
                "UPDATE tasks SET status='completed', output_paths=?, prompt_id=COALESCE(?, prompt_id), "
                "error_code=NULL, error_message=NULL, finished_at=?, updated_at=? WHERE task_key=?",
                (_json_dumps(paths), prompt_id, timestamp, timestamp, task_key),
            )
            conn.execute(
                "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                (row["job_set_id"], task_key, "task_completed", f"产物 {len(paths)} 个", timestamp),
            )
            self._sync_counts(conn, row["job_set_id"])
        self.finalize_job(row["job_set_id"])
        return True

    def mark_failed(self, task_key: str, code: str, message: str, *,
                    prompt_id: str | None = None, retryable: bool = True) -> bool:
        timestamp = utc_now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_key=?", (task_key,)).fetchone()
            if row is None:
                raise LedgerError("TASK_NOT_FOUND", f"任务不存在：{task_key}")
            conn.execute(
                "UPDATE tasks SET status='failed', error_code=?, error_message=?, "
                "prompt_id=COALESCE(?, prompt_id), finished_at=?, updated_at=? WHERE task_key=?",
                (str(code)[:80], str(message)[:2000], prompt_id, timestamp, timestamp, task_key),
            )
            conn.execute(
                "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                (row["job_set_id"], task_key, "task_failed", f"[{code}] {str(message)[:500]}", timestamp),
            )
            self._sync_counts(conn, row["job_set_id"])
        return True

    def mark_unknown(self, task_key: str, message: str = "任务状态未知，等待恢复") -> bool:
        timestamp = utc_now()
        with self._tx() as conn:
            row = conn.execute("SELECT status, job_set_id FROM tasks WHERE task_key=?", (task_key,)).fetchone()
            if row is None:
                raise LedgerError("TASK_NOT_FOUND", f"任务不存在：{task_key}")
            conn.execute(
                "UPDATE tasks SET status='unknown', error_code='UNKNOWN', error_message=?, updated_at=? WHERE task_key=?",
                (str(message)[:2000], timestamp, task_key),
            )
            conn.execute(
                "INSERT INTO events(job_set_id, task_key, kind, message, created_at) VALUES(?,?,?,?,?)",
                (row["job_set_id"], task_key, "task_unknown", str(message)[:500], timestamp),
            )
        return True

    def inflight_tasks(self, job_set_id: str | None = None) -> list[dict]:
        with self._lock:
            if job_set_id:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE job_set_id=? AND status IN ('submitted','running') "
                    "ORDER BY order_index",
                    (job_set_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status IN ('submitted','running') ORDER BY job_set_id, order_index"
                ).fetchall()
        return [self._task_dict(row) for row in rows]

    def inflight_prompt_ids(self, job_set_id: str) -> list[str]:
        return [task["prompt_id"] for task in self.inflight_tasks(job_set_id) if task.get("prompt_id")]

    def retry_failed(self, job_set_id: str) -> dict:
        """失败/未知任务重置为 pending，任务集恢复 running（契约 §6 失败可重试）。"""
        timestamp = utc_now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM job_sets WHERE id=?", (job_set_id,)).fetchone()
            if row is None:
                raise LedgerError("JOB_NOT_FOUND", f"任务集不存在：{job_set_id}")
            if row["status"] == "cancelled":
                raise LedgerError("STATUS_INVALID", "任务集已取消，请重新提交")
            cursor = conn.execute(
                "UPDATE tasks SET status='pending', prompt_id=NULL, output_paths=NULL, "
                "error_code=NULL, error_message=NULL, enqueued_at=NULL, started_at=NULL, finished_at=NULL, "
                "updated_at=? WHERE job_set_id=? AND status IN ('failed','unknown')",
                (timestamp, job_set_id),
            )
            retried = int(cursor.rowcount or 0)
            conn.execute(
                "UPDATE job_sets SET status='running', error=NULL, finished_at=NULL, updated_at=? WHERE id=?",
                (timestamp, job_set_id),
            )
            conn.execute(
                "INSERT INTO events(job_set_id, kind, message, created_at) VALUES(?,?,?,?)",
                (job_set_id, "job_retry", f"重试失败任务 {retried} 个", timestamp),
            )
            self._sync_counts(conn, job_set_id)
        return {"job_set_id": job_set_id, "retried": retried, "status": "running"}

    def reset_unknown(self, job_set_id: str) -> int:
        """resume 时把 unknown 任务放回 pending（无产物、需重跑）。"""
        timestamp = utc_now()
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status='pending', prompt_id=NULL, enqueued_at=NULL, started_at=NULL, "
                "error_code=NULL, error_message=NULL, updated_at=? WHERE job_set_id=? AND status='unknown'",
                (timestamp, job_set_id),
            )
            count = int(cursor.rowcount or 0)
            if count:
                conn.execute(
                    "INSERT INTO events(job_set_id, kind, message, created_at) VALUES(?,?,?,?)",
                    (job_set_id, "reset_unknown", f"unknown → pending {count} 个", timestamp),
                )
                self._sync_counts(conn, job_set_id)
        return count

    # -- 恢复 / 收尾 ----------------------------------------------------

    def recover(
        self,
        *,
        recovered_outputs: Mapping[str, Iterable[str]] | Callable[[dict], Iterable[str] | None] | None = None,
        active_prompt_ids: Iterable[str] | None = None,
        job_set_id: str | None = None,
        node_id: str | None = None,
        retry_missing: bool = False,
    ) -> dict:
        """重启对账（契约 §6）。

        * ``recovered_outputs``：task_key/prompt_id → 产物路径列表，或 ``fn(task) -> paths``；
        * ``active_prompt_ids``：当前仍在 ComfyUI 队列/运行中的 prompt_id 集合；
        * 有产物 → completed（**不重跑**）；仍在队列 → 保持 running；否则 → unknown。
        """
        active = {str(pid) for pid in (active_prompt_ids or []) if pid}
        tasks = self.inflight_tasks(job_set_id)
        if node_id:
            tasks = [task for task in tasks if str(task.get("node_id") or node_id) == str(node_id)]
        recovered_completed = 0
        unknown = 0
        requeued = 0
        still_active = 0
        touched_jobs: set[str] = set()
        for task in tasks:
            paths: Iterable[str] | None = None
            if callable(recovered_outputs):
                try:
                    paths = recovered_outputs(task)
                except Exception:  # pragma: no cover - 对账回调容错
                    paths = None
            elif isinstance(recovered_outputs, Mapping):
                candidate = None
                if task["task_key"] in recovered_outputs:
                    candidate = recovered_outputs[task["task_key"]]
                elif task.get("prompt_id") and task["prompt_id"] in recovered_outputs:
                    candidate = recovered_outputs[task["prompt_id"]]
                paths = candidate
            path_list = [str(path) for path in (paths or []) if path]
            touched_jobs.add(task["job_set_id"])
            if path_list:
                self.mark_completed(task["task_key"], path_list, prompt_id=task.get("prompt_id"))
                recovered_completed += 1
            elif task.get("prompt_id") and task["prompt_id"] in active:
                still_active += 1
            elif retry_missing:
                if self.reset_for_retry(task["task_key"], "重启对账：队列/历史中无此 prompt_id，安全重投",
                                        expected_prompt_id=task.get("prompt_id")):
                    requeued += 1
            else:
                self.mark_unknown(task["task_key"], "重启对账：队列与历史中均无此任务，且输出目录无产物")
                unknown += 1
        finalized = []
        for job_id in touched_jobs:
            status = self.finalize_job(job_id)
            if status:
                finalized.append({"job_set_id": job_id, "status": status})
        return {
            "recovered_completed": recovered_completed,
            "unknown": unknown,
            "requeued": requeued,
            "still_active": still_active,
            "finalized": finalized,
            "checked": len(tasks),
        }

    def finalize_job(self, job_set_id: str) -> str | None:
        """任务集收尾：所有任务终态后根据结果设置最终状态。"""
        timestamp = utc_now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM job_sets WHERE id=?", (job_set_id,)).fetchone()
            if row is None:
                raise LedgerError("JOB_NOT_FOUND", f"任务集不存在：{job_set_id}")
            counts = _counts_from_rows(
                conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks WHERE job_set_id=? GROUP BY status",
                    (job_set_id,),
                ).fetchall()
            )
            if counts["pending"] or counts["submitted"] or counts["running"]:
                return None
            if row["status"] == "cancelled":
                self._sync_counts(conn, job_set_id)
                return "cancelled"
            status = "completed" if counts["failed"] == 0 and counts["completed"] > 0 else (
                "failed" if counts["failed"] or counts["completed"] == 0 else "completed"
            )
            conn.execute(
                "UPDATE job_sets SET status=?, updated_at=?, finished_at=? WHERE id=?",
                (status, timestamp, timestamp, job_set_id),
            )
            conn.execute(
                "INSERT INTO events(job_set_id, kind, message, created_at) VALUES(?,?,?,?)",
                (job_set_id, "job_finished", f"{status}：完成 {counts['completed']}/{counts['total']}", timestamp),
            )
            self._sync_counts(conn, job_set_id)
        return status

    def _sync_counts(self, conn: sqlite3.Connection, job_set_id: str) -> None:
        counts = _counts_from_rows(
            conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE job_set_id=? GROUP BY status",
                (job_set_id,),
            ).fetchall()
        )
        conn.execute(
            "UPDATE job_sets SET completed_count=?, failed_count=?, updated_at=? WHERE id=?",
            (counts["completed"], counts["failed"], utc_now(), job_set_id),
        )

    # -- 报告 / 清单 ----------------------------------------------------

    def report(self, job_set_id: str, task_limit: int = 500) -> dict:
        job = self.get_job_set(job_set_id)
        tasks = self.list_tasks(job_set_id, limit=task_limit)
        summary = [
            {
                "order": task["order_index"],
                "task_key": task["task_key"],
                "status": task["status"],
                "pair_id": task["task"].get("pair_id"),
                "model": task["task"].get("model"),
                "sample_index": task["task"].get("sample_index"),
                "seed": task["task"].get("seed"),
                "prompt_id": task["prompt_id"],
                "attempts": task["attempts"],
                "outputs": task["output_paths"],
                "error": (
                    {"code": task["error_code"], "message": task["error_message"]}
                    if task["error_code"] or task["error_message"]
                    else None
                ),
            }
            for task in tasks
        ]
        return {
            "schema": "aiksk.pc.report.v1",
            "generated_at": utc_now(),
            "job_set": job,
            "counts": job["counts"],
            "tasks": summary,
            "events": self.events(job_set_id, limit=200),
        }

    def manifest(self, job_set_id: str) -> dict:
        job = self.get_job_set(job_set_id)
        tasks = self.list_tasks(job_set_id, limit=1000)
        return {
            "schema": MANIFEST_SCHEMA,
            "exported_at": utc_now(),
            "ledger_schema": LEDGER_SCHEMA,
            "job_set": {
                key: job[key]
                for key in (
                    "job_set_id", "status", "plan_hash", "mode", "graph_model", "dual_compare",
                    "pair_count", "task_count", "created_at", "updated_at", "started_at", "finished_at",
                )
            },
            "plan": job["plan"],
            "templates": job["templates"],
            "counts": job["counts"],
            "tasks": [
                {
                    "order": task["order_index"],
                    "task_key": task["task_key"],
                    "task": task["task"],
                    "status": task["status"],
                    "prompt_id": task["prompt_id"],
                    "attempts": task["attempts"],
                    "outputs": task["output_paths"],
                    "error": (
                        {"code": task["error_code"], "message": task["error_message"]}
                        if task["error_code"] or task["error_message"]
                        else None
                    ),
                }
                for task in tasks
            ],
        }

    # -- 统计 ----------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            jobs = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM job_sets GROUP BY status"
            ).fetchall()
            tasks = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            last = self._conn.execute(
                "SELECT id, status, updated_at FROM job_sets ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return {
            "path": str(self.path),
            "job_sets": {row["status"]: int(row["n"]) for row in jobs},
            "job_sets_total": sum(int(row["n"]) for row in jobs),
            "tasks": {row["status"]: int(row["n"]) for row in tasks},
            "tasks_total": sum(int(row["n"]) for row in tasks),
            "last_job_set": dict(last) if last else None,
            "schema_version": SCHEMA_VERSION,
        }

    def active_job_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM job_sets WHERE status IN ('pending','running','paused') ORDER BY created_at"
            ).fetchall()
        return [row["id"] for row in rows]

    def running_job_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM job_sets WHERE status='running' ORDER BY created_at"
            ).fetchall()
        return [row["id"] for row in rows]

    def job_templates(self, job_set_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT model_key, node_id, prompt_json FROM templates WHERE job_set_id=?", (job_set_id,)
            ).fetchall()
        result = {}
        for row in rows:
            result[row["model_key"]] = {
                "node_id": row["node_id"],
                "prompt": _json_loads(row["prompt_json"], {}),
            }
        return result


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "ACTIVE_TASK_STATUSES",
    "JOB_STATUSES",
    "LEDGER_SCHEMA",
    "Ledger",
    "LedgerError",
    "MANIFEST_SCHEMA",
    "SCHEMA_VERSION",
    "TASK_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "utc_now",
]
