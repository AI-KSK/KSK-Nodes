"""批次执行器：ComfyBridge（ComfyUI 进程内桥）+ BatchRunner（任务集调度）。

硬性约束（契约 §6）：

* **绝无定时自动提交** —— 本模块不创建任何后台线程 / 定时器 / daemon timer；
  只有 ``/submit``、``/resume``、``/jobs`` 轮询等显式请求触发 ``tick()`` 时才推进；
* ``tick()`` 只处理 ``status='running'`` 的任务集；
* 默认 in-flight=1（``Config.max_in_flight``），硬上限 4；
* 恢复对账：输出目录已有产物 → 直接 completed，**已完成不重做**。

ComfyBridge 惰性 import ``server.PromptServer`` / ``execution`` / ``comfyui_version``，
在没有 ComfyUI 的环境（纯单测）下也能安全创建；所有方法都有降级分支。
"""

from __future__ import annotations

import asyncio
import copy
import glob
import inspect
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import HARD_MAX_IN_FLIGHT
from . import catalog as catalog_module
from . import config as config_module
from . import planner
from .ledger import Ledger, LedgerError, utc_now

LOGGER = logging.getLogger("AIKSK.PoseCostumeMatrix.runner")

CLIENT_ID = "aiksk-pc-runner"
OUTPUT_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
LOST_GRACE_SECONDS = 30.0
RECOVER_MTIME_GRACE = 120.0


class RunnerError(ValueError):
    """带错误码的执行器错误。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover
        return self.message


def _maybe_await(value):
    return value


def _parse_iso(value: Any) -> float | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ComfyUI 桥
# ---------------------------------------------------------------------------


class ComfyBridge:
    """ComfyUI 进程内桥（惰性 import + 版本兼容 + 全方法降级安全）。"""

    def __init__(self, config: config_module.Config | None = None, *, modules: Mapping[str, Any] | None = None) -> None:
        self.config = config or config_module.get_config()
        self._modules = dict(modules or {})
        self._loaded = False
        self._load_error: str | None = None
        self._server_module = None
        self._execution_module = None
        self._version_module = None
        self._arity: int | None = None
        self._lock = threading.RLock()

    # -- 惰性导入 ------------------------------------------------------

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                self._server_module = self._modules.get("server") or __import__("server")
                self._execution_module = self._modules.get("execution") or __import__("execution")
            except Exception as exc:  # 无 ComfyUI 环境
                self._load_error = f"无法导入 ComfyUI 模块：{exc}"
                self._server_module = self._modules.get("server")
                self._execution_module = self._modules.get("execution")
                return
            try:
                self._version_module = self._modules.get("comfyui_version") or __import__("comfyui_version")
            except Exception:  # 版本模块可选
                self._version_module = None

    @property
    def available(self) -> bool:
        self._load()
        if self._server_module is None:
            return False
        try:
            instance = getattr(self._server_module, "PromptServer", None)
            instance = instance.instance if instance is not None else None
            return instance is not None and getattr(instance, "prompt_queue", None) is not None
        except Exception:  # pragma: no cover
            return False

    @property
    def load_error(self) -> str | None:
        self._load()
        return self._load_error

    @property
    def version(self) -> str | None:
        self._load()
        module = self._version_module
        if module is None:
            return None
        return str(getattr(module, "__version__", "") or "") or None

    @property
    def server(self):
        self._load()
        return self._server_module

    @property
    def execution(self):
        self._load()
        return self._execution_module

    def instance(self):
        self._load()
        module = self._server_module
        if module is None:
            return None
        server_class = getattr(module, "PromptServer", None)
        return getattr(server_class, "instance", None) if server_class is not None else None

    def prompt_queue(self):
        instance = self.instance()
        return getattr(instance, "prompt_queue", None) if instance is not None else None

    # -- 队列元组兼容 --------------------------------------------------

    def queue_arity(self) -> int:
        """探测 ``prompt_queue.put`` 元组长度：6（新）/5/4（旧）。"""
        with self._lock:
            if self._arity is not None:
                return self._arity
            if self.config.queue_arity in (4, 5, 6):
                self._arity = int(self.config.queue_arity)
                return self._arity
            self._load()
            detected = self._detect_arity_from_source()
            if detected is None:
                detected = self._detect_arity_from_version()
            self._arity = detected or 5
            return self._arity

    def _detect_arity_from_source(self) -> int | None:
        server_module = self._server_module
        filename = getattr(server_module, "__file__", None) if server_module is not None else None
        if not filename:
            return None
        root = Path(filename).resolve().parent
        for candidate in (root / "main.py", root / "execution.py"):
            try:
                text = candidate.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "sensitive = item[5]" in text or "item[5]" in text:
                return 6
            if "e.execute(item[2]" in text and "item[4]" in text:
                return 5
            if "e.execute(item[2]" in text and "item[3]" in text:
                return 4
        return None

    def _detect_arity_from_version(self) -> int | None:
        version = self.version
        if not version:
            return None
        try:
            parts = [int(part) for part in version.split(".")[:3]]
        except ValueError:
            return None
        while len(parts) < 3:
            parts.append(0)
        # 0.42.x 起队列项带 sensitive 字段（6 元组）；更早版本是 5 元组。
        if tuple(parts) >= (0, 42, 0):
            return 6
        return 5

    def build_queue_item(self, prompt_id: str, prompt: Mapping[str, Any], outputs_to_execute: Iterable[str],
                         *, extra_data: Mapping[str, Any] | None = None) -> tuple:
        arity = self.queue_arity()
        number = time.time()
        extra = dict(extra_data or {"client_id": CLIENT_ID})
        extra.setdefault("client_id", CLIENT_ID)
        extra.setdefault("create_time", int(time.time() * 1000))
        outputs = list(outputs_to_execute or [])
        if arity >= 6:
            return (number, prompt_id, dict(prompt), extra, outputs, {})
        if arity == 5:
            return (number, prompt_id, dict(prompt), extra, outputs)
        return (number, prompt_id, dict(prompt), extra)

    # -- 校验（兼容 async） --------------------------------------------

    def _run_awaitable(self, awaitable):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        # 当前线程已有事件循环：放到独立线程里跑，避免 "already running" 错误。
        result_box: dict[str, Any] = {}

        def _runner() -> None:
            try:
                result_box["value"] = asyncio.run(awaitable)
            except BaseException as exc:  # pragma: no cover - 透传
                result_box["error"] = exc

        thread = threading.Thread(target=_runner, name="aiksk-pc-validate", daemon=True)
        thread.start()
        thread.join(timeout=120)
        if "error" in result_box:
            raise result_box["error"]
        if "value" not in result_box:  # pragma: no cover
            raise RunnerError("VALIDATE_TIMEOUT", "ComfyUI 校验超时")
        return result_box["value"]

    def validate(self, prompt: Mapping[str, Any], prompt_id: str | None = None) -> dict:
        """调用 ``execution.validate_prompt``；``async def`` 与普通函数都支持。

        返回 ``{"ok", "error", "outputs", "node_errors"}``；无 ComfyUI 时视为通过。
        """
        self._load()
        execution = self._execution_module
        validate_fn = getattr(execution, "validate_prompt", None) if execution is not None else None
        if validate_fn is None:
            return {"ok": True, "error": None, "outputs": self._guess_outputs(prompt), "node_errors": {}}
        prompt_id = prompt_id or str(uuid.uuid4())
        try:
            if inspect.iscoroutinefunction(validate_fn):
                result = self._run_awaitable(validate_fn(prompt_id, dict(prompt), None))
            else:
                result = validate_fn(prompt_id, dict(prompt), None)
                if inspect.isawaitable(result):
                    result = self._run_awaitable(result)
        except RunnerError:
            raise
        except Exception as exc:
            raise RunnerError("VALIDATE_FAILED", f"ComfyUI 工作流校验异常：{exc}") from exc
        return self._parse_validate(result)

    def _parse_validate(self, result: Any) -> dict:
        if isinstance(result, Mapping):
            return {
                "ok": bool(result.get("ok", True)),
                "error": result.get("error"),
                "outputs": list(result.get("outputs") or []),
                "node_errors": result.get("node_errors") or {},
            }
        if isinstance(result, (tuple, list)):
            ok = bool(result[0]) if result else True
            error = result[1] if len(result) > 1 else None
            outputs = result[2] if len(result) > 2 else []
            node_errors = result[3] if len(result) > 3 else {}
            return {
                "ok": ok,
                "error": error,
                "outputs": list(outputs or []),
                "node_errors": node_errors or {},
            }
        return {"ok": True, "error": None, "outputs": self._guess_outputs({}), "node_errors": {}}

    def _guess_outputs(self, prompt: Mapping[str, Any]) -> list[str]:
        classes = {"SaveImage", "SaveImageAdvanced", "PreviewImage", "SaveAnimatedWEBP"}
        return [
            str(node_id)
            for node_id, node in (prompt or {}).items()
            if isinstance(node, dict) and node.get("class_type") in classes
        ]

    # -- 入队 ----------------------------------------------------------

    def submit(self, prompt: Mapping[str, Any], *, prompt_id: str | None = None,
               extra_data: Mapping[str, Any] | None = None) -> str:
        """通过标准 ``POST /prompt`` 提交（审计 P1-1），返回服务器 prompt_id。"""
        if not isinstance(prompt, Mapping) or not prompt:
            raise RunnerError("TEMPLATE_INVALID", "提交的工作流为空")
        prompt_id = str(prompt_id or uuid.uuid4())
        instance = self.instance()
        port = getattr(instance, "port", None) if instance is not None else None
        if instance is not None and port:
            return self._submit_http(prompt, prompt_id=prompt_id, extra_data=extra_data)
        injected = self._modules.get("server") is not None
        if os.environ.get("AIKSK_PC_ALLOW_INTERNAL_QUEUE", "0") != "1" and not injected:
            raise RunnerError(
                "COMFY_UNAVAILABLE",
                "无法通过标准 /prompt 提交（未发现运行中的 PromptServer）；生产环境禁止内部队列猜测",
            )
        queue = self.prompt_queue()
        if queue is None:
            raise RunnerError("COMFY_UNAVAILABLE", "ComfyUI 队列不可用（PromptServer 未启动）")
        validation = self.validate(prompt, prompt_id)
        if not validation["ok"]:
            detail = validation.get("error") or validation.get("node_errors") or {}
            message = detail.get("message") if isinstance(detail, Mapping) else None
            raise RunnerError("TEMPLATE_INVALID", f"ComfyUI 校验未通过：{message or detail}")
        outputs = validation.get("outputs") or self._guess_outputs(prompt)
        queue.put(self.build_queue_item(prompt_id, prompt, outputs))
        return prompt_id

    def _submit_http(self, prompt: Mapping[str, Any], *, prompt_id: str,
                     extra_data: Mapping[str, Any] | None = None) -> str:
        instance = self.instance()
        port = int(getattr(instance, "port") or 8188)
        payload = {
            "prompt": dict(prompt),
            "client_id": CLIENT_ID,
            "prompt_id": prompt_id,
            "extra_data": dict(extra_data or {}),
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/prompt",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read() or b"{}")
            except Exception:  # noqa: BLE001
                body = {}
            error = body.get("error") if isinstance(body, dict) else {}
            message = error.get("message") if isinstance(error, Mapping) else str(error)
            raise RunnerError(
                "TEMPLATE_INVALID",
                f"标准 /prompt 校验拒绝：{message or exc.reason}",
                {"status": exc.code, "node_errors": (body or {}).get("node_errors")},
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise RunnerError(
                "SUBMIT_UNKNOWN",
                f"POST /prompt 结果未知（连接中断/超时）：{exc}；将按 prompt_id 对账，不盲目重投",
                {"prompt_id": prompt_id},
            ) from exc
        return str(data.get("prompt_id") or prompt_id)

    # -- 历史 / 队列快照 / 中断 ---------------------------------------

    def history(self, prompt_id: str) -> dict | None:
        """返回 history 条目（含 status/outputs）；不可用时返回 None。"""
        queue = self.prompt_queue()
        if queue is None or not prompt_id:
            return None
        try:
            getter = getattr(queue, "get_history", None)
            if callable(getter):
                data = getter(prompt_id=prompt_id)
                if isinstance(data, Mapping):
                    entry = data.get(prompt_id)
                    if entry is None and data.get("prompt") is not None:
                        entry = data
                    return dict(entry) if entry is not None else None
            raw = getattr(queue, "history", None)
            if isinstance(raw, Mapping):
                entry = raw.get(prompt_id)
                return dict(entry) if entry is not None else None
        except Exception:  # pragma: no cover - 降级
            LOGGER.debug("读取 ComfyUI history 失败", exc_info=True)
        return None

    def queue_snapshot(self) -> dict:
        """返回 ``{"available", "running": [pid], "queued": [pid]}``（永不抛错）。"""
        queue = self.prompt_queue()
        result = {"available": queue is not None, "running": [], "queued": []}
        if queue is None:
            return result
        try:
            getter = getattr(queue, "get_current_queue_volatile", None) or getattr(queue, "get_current_queue", None)
            if not callable(getter):
                return result
            running, queued = getter()
            result["running"] = [self._item_prompt_id(item) for item in (running or []) if self._item_prompt_id(item)]
            result["queued"] = [self._item_prompt_id(item) for item in (queued or []) if self._item_prompt_id(item)]
        except Exception:  # pragma: no cover - 降级
            LOGGER.debug("读取 ComfyUI 队列快照失败", exc_info=True)
        return result

    @staticmethod
    def _item_prompt_id(item: Any) -> str | None:
        if isinstance(item, str):
            return item
        try:
            if isinstance(item, (tuple, list)) and len(item) > 1 and isinstance(item[1], str):
                return item[1]
            if isinstance(item, Mapping):
                value = item.get("prompt_id")
                return str(value) if value else None
        except Exception:  # pragma: no cover
            return None
        return None

    def interrupt(self, prompt_id: str | None = None) -> bool:
        """中断指定 prompt（或当前任务）。失败返回 False，不抛异常。"""
        queue = self.prompt_queue()
        if queue is None:
            return False
        try:
            if prompt_id:
                interrupter = getattr(queue, "interrupt_if_running", None)
                if callable(interrupter):
                    return bool(interrupter(prompt_id))
                running = self.queue_snapshot()["running"]
                if prompt_id not in running:
                    return False
            nodes_module = self._modules.get("nodes") or __import__("nodes")
            interrupter = getattr(nodes_module, "interrupt_processing", None)
            if callable(interrupter):
                interrupter()
                return True
        except Exception:  # pragma: no cover - 降级
            LOGGER.debug("中断 ComfyUI 任务失败", exc_info=True)
        return False

    def delete_queued(self, prompt_id: str) -> bool:
        """从 ComfyUI 队列删除尚未开始的任务（取消时尽力而为）。"""
        queue = self.prompt_queue()
        if queue is None or not prompt_id:
            return False
        try:
            deleter = getattr(queue, "delete_queue_item", None)
            if callable(deleter):
                return bool(deleter(lambda item: self._item_prompt_id(item) == prompt_id))
        except Exception:  # pragma: no cover
            LOGGER.debug("删除排队任务失败", exc_info=True)
        return False

    def parse_history(self, entry: Mapping[str, Any]) -> dict:
        """解析 history 条目（审计 P0-2：保留真实 node_id 与按节点分组输出）。"""
        status = entry.get("status") if isinstance(entry, Mapping) else None
        status_str = None
        completed = False
        error_message = None
        if isinstance(status, Mapping):
            status_str = status.get("status_str")
            completed = bool(status.get("completed"))
            messages = status.get("messages") or []
            if status_str == "error":
                error_message = _format_history_messages(messages)
        outputs = entry.get("outputs") if isinstance(entry, Mapping) else None
        images: list[dict] = []
        outputs_by_node: dict[str, list[dict]] = {}
        for node_id, node_output in (outputs or {}).items():
            if not isinstance(node_output, Mapping):
                continue
            node_images = outputs_by_node.setdefault(str(node_id), [])
            for image in node_output.get("images") or []:
                if not isinstance(image, Mapping):
                    continue
                record = {
                    "filename": image.get("filename"),
                    "subfolder": image.get("subfolder") or "",
                    "type": image.get("type") or "output",
                    "node_id": str(node_id),
                }
                images.append(record)
                node_images.append(record)
        saved = [image for image in images if image["type"] == "output" and image.get("filename")]
        return {
            "status_str": status_str,
            "completed": completed,
            "images": images,
            "saved_images": saved,
            "outputs_by_node": outputs_by_node,
            "error": error_message,
        }

    def expected_save_ids(self, task: Mapping[str, Any]) -> list[str]:
        prefixes = task.get("save_prefixes") or {}
        if isinstance(prefixes, Mapping) and prefixes:
            return [str(node_id) for node_id in prefixes.keys()]
        return []

    def extract_task_outputs(self, task: Mapping[str, Any], parsed: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        expected = self.expected_save_ids(task)
        if not expected:
            saved = parsed.get("saved_images") or []
            return [self.output_path_for(image) for image in saved], []
        by_node = parsed.get("outputs_by_node") or {}
        paths: list[str] = []
        missing: list[str] = []
        for node_id in expected:
            candidates = [
                image for image in (by_node.get(node_id) or [])
                if image.get("type") == "output" and image.get("filename")
            ]
            if not candidates:
                missing.append(node_id)
                continue
            paths.extend(self.output_path_for(image) for image in candidates)
        return paths, missing

    @staticmethod
    def outputs_readable(paths: Iterable[str]) -> bool:
        for value in paths:
            path = Path(value)
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    return False
                with path.open("rb") as handle:
                    head = handle.read(12)
                if not (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8") or head[:4] == b"RIFF"):
                    return False
            except OSError:
                return False
        return True

    def output_path_for(self, image: Mapping[str, Any]) -> str:
        root = self.config.resolve_output_root()
        subfolder = str(image.get("subfolder") or "").replace("\\", "/")
        filename = str(image.get("filename") or "")
        if subfolder:
            return str(root / subfolder / filename)
        return str(root / filename)

    def status_snapshot(self) -> dict:
        queue = self.queue_snapshot()
        return {
            "available": self.available,
            "version": self.version,
            "queue_arity": self.queue_arity(),
            "queued": len(queue["queued"]),
            "running": len(queue["running"]),
            "load_error": self.load_error,
        }


def _format_history_messages(messages: Any) -> str:
    try:
        parts = []
        for message in messages or []:
            if isinstance(message, (list, tuple)) and len(message) == 2 and isinstance(message[1], Mapping):
                payload = message[1]
                if payload.get("exception_message"):
                    parts.append(str(payload["exception_message"]).strip()[:500])
                elif payload.get("traceback"):
                    parts.append(str(payload["traceback"])[-500:])
            elif isinstance(message, str):
                parts.append(message[:500])
        return " | ".join(parts)[:1500]
    except Exception:  # pragma: no cover
        return ""


# ---------------------------------------------------------------------------
# 批次执行器
# ---------------------------------------------------------------------------


class BatchRunner:
    """任务集调度器；所有推进都由显式 ``tick()`` 驱动（无后台定时提交）。"""

    def __init__(self, ledger: Ledger | None = None, bridge: ComfyBridge | None = None,
                 config: config_module.Config | None = None) -> None:
        self.config = config or config_module.get_config()
        self.ledger = ledger if ledger is not None else Ledger(config=self.config)
        self.bridge = bridge if bridge is not None else ComfyBridge(self.config)
        self._started = False
        self._start_lock = threading.RLock()
        self._tick_lock = threading.RLock()
        self._recover_summary: dict | None = None
        self._last_tick: dict | None = None
        self._last_tick_at: str | None = None

    # -- 生命周期 ------------------------------------------------------

    def ensure_started(self) -> dict:
        """进程内首次调用时做一次启动对账（输出目录产物对账）。"""
        with self._start_lock:
            if self._started:
                return {"started": False, "recover": self._recover_summary}
            self._started = True
            if not self.config.recover_on_start:
                self._recover_summary = {"skipped": True, "reason": "配置关闭了启动对账"}
                return {"started": True, "recover": self._recover_summary}
            try:
                self._recover_summary = self.recover()
            except Exception as exc:  # 启动对账失败不能阻塞插件
                LOGGER.warning("[AIKSK-PC] 启动恢复对账失败：%s", exc)
                self._recover_summary = {"error": str(exc)}
        return {"started": True, "recover": self._recover_summary}

    def recover(self, job_set_id: str | None = None) -> dict:
        """重启对账（审计 P0-3）：只信 prompt_id 对应的 history 产物，不再用文件名前缀猜。"""
        snapshot = self.bridge.queue_snapshot() if self.bridge.available else {"running": [], "queued": []}
        active = set(snapshot.get("running") or []) | set(snapshot.get("queued") or [])
        inflight = self.ledger.inflight_tasks(job_set_id)
        recovered: dict[str, list[str]] = {}
        for task in inflight:
            prompt_id = task.get("prompt_id")
            if not prompt_id or not self.bridge.available:
                continue
            entry = self.bridge.history(prompt_id)
            if entry is None:
                continue
            parsed = self.bridge.parse_history(entry)
            if parsed["status_str"] == "error":
                self.ledger.mark_failed(
                    task["task_key"], "EXECUTION_ERROR",
                    parsed.get("error") or "ComfyUI 执行失败", prompt_id=prompt_id,
                )
                continue
            paths, missing = self.extract_task_outputs(task, parsed)
            if paths and not missing and self.outputs_readable(paths):
                recovered[task["task_key"]] = paths
                continue
            if missing:
                self.ledger.mark_failed(
                    task["task_key"], "OUTPUT_PARTIAL",
                    f"最终保存节点缺少产物：{', '.join(missing)}", prompt_id=prompt_id,
                )
                continue
            if parsed["completed"] or parsed["status_str"] == "success":
                self.ledger.mark_failed(
                    task["task_key"], "OUTPUT_MISSING",
                    "任务已完成但未找到声明的最终保存节点输出", prompt_id=prompt_id,
                )
                continue
            # 历史已出现但尚未完成：仍视为活跃
            active.add(prompt_id)
        summary = self.ledger.recover(
            recovered_outputs=recovered,
            active_prompt_ids=active,
            job_set_id=job_set_id,
            retry_missing=True,
        )
        summary["queue_snapshot"] = snapshot
        summary["scan_checked"] = len(inflight)
        self.ledger.record_event(
            "recover",
            f"对账完成：产物恢复 {summary.get('recovered_completed', 0)} / 安全重投 {summary.get('requeued', 0)}"
            f" / 未知 {summary.get('unknown', 0)} / 仍活跃 {summary.get('still_active', 0)}",
            job_set_id=job_set_id,
        )
        return summary

    # -- 主循环 --------------------------------------------------------

    def tick(self) -> dict:
        """推进一次：轮询 in-flight、补投任务、任务集收尾。

        只有路由显式调用时才会执行；没有后台定时器（契约 §6）。
        """
        self.ensure_started()
        with self._tick_lock:
            summary: dict = {
                "started_at": utc_now(),
                "polled": 0,
                "completed": 0,
                "failed": 0,
                "submitted": 0,
                "job_sets": [],
                "bridge": self.bridge.status_snapshot() if hasattr(self.bridge, "status_snapshot") else None,
            }
            snapshot = self.bridge.queue_snapshot() if self.bridge.available else {"running": [], "queued": []}
            active = set(snapshot.get("running") or []) | set(snapshot.get("queued") or [])
            # 暂停只停“新任务”：paused 任务集里已在跑的 in-flight 仍然轮询/收尾，但不补投。
            poll_ids = self.ledger.running_job_ids()
            paused_ids = [
                row["job_set_id"]
                for row in self.ledger.list_job_sets(limit=200, status="paused")
            ]
            for paused_id in paused_ids:
                if paused_id not in poll_ids:
                    poll_ids.append(paused_id)
            for job_id in poll_ids:
                job_summary = {"job_set_id": job_id, "polled": 0, "completed": 0, "failed": 0, "submitted": 0}
                try:
                    self._poll_job(job_id, active, job_summary)
                    job_status = self.ledger.job_set_row(job_id)["status"]
                    if self.bridge.available and job_status == "running":
                        self._dispatch_job(job_id, job_summary)
                except LedgerError as exc:
                    job_summary["error"] = {"code": exc.code, "message": exc.message}
                except Exception as exc:  # pragma: no cover - 单任务集失败不影响其它
                    LOGGER.exception("[AIKSK-PC] tick 处理任务集 %s 失败", job_id)
                    job_summary["error"] = {"code": "RUNNER_ERROR", "message": str(exc)}
                status = None
                try:
                    status = self.ledger.finalize_job(job_id)
                except LedgerError:  # pragma: no cover
                    pass
                job_summary["status"] = status or self.ledger.job_set_row(job_id)["status"]
                for key in ("polled", "completed", "failed", "submitted"):
                    summary[key] += int(job_summary.get(key, 0))
                summary["job_sets"].append(job_summary)
            summary["finished_at"] = utc_now()
            self._last_tick = summary
            self._last_tick_at = summary["finished_at"]
            return summary

    def expected_save_ids(self, task: Mapping[str, Any]) -> list[str]:
        prefixes = task.get("save_prefixes") or {}
        if isinstance(prefixes, Mapping) and prefixes:
            return [str(node_id) for node_id in prefixes.keys()]
        return []

    def extract_task_outputs(self, task: Mapping[str, Any], parsed: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        """只取该任务声明的最终保存节点输出（审计 P0-2）。"""
        expected = self.expected_save_ids(task)
        if not expected:
            saved = parsed.get("saved_images") or []
            return [self.bridge.output_path_for(image) for image in saved], []
        by_node = parsed.get("outputs_by_node") or {}
        paths: list[str] = []
        missing: list[str] = []
        for node_id in expected:
            candidates = [
                image for image in (by_node.get(node_id) or [])
                if image.get("type") == "output" and image.get("filename")
            ]
            if not candidates:
                missing.append(node_id)
                continue
            paths.extend(self.bridge.output_path_for(image) for image in candidates)
        return paths, missing

    @staticmethod
    def outputs_readable(paths: Iterable[str]) -> bool:
        for value in paths:
            path = Path(value)
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    return False
                with path.open("rb") as handle:
                    head = handle.read(12)
                if not (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8") or head[:4] == b"RIFF"):
                    return False
            except OSError:
                return False
        return True

    def _poll_job(self, job_id: str, active_prompt_ids: set[str], job_summary: dict) -> None:
        tasks = self.ledger.inflight_tasks(job_id)
        if not tasks:
            return
        now = time.time()
        for task in tasks:
            job_summary["polled"] += 1
            prompt_id = task.get("prompt_id")
            if not prompt_id:
                self.ledger.mark_unknown(task["task_key"], "任务已领取但从未获得 prompt_id")
                job_summary["failed"] += 1
                continue
            entry = self.bridge.history(prompt_id) if self.bridge.available else None
            if entry is not None:
                parsed = self.bridge.parse_history(entry)
                if parsed["status_str"] == "error":
                    self.ledger.mark_failed(
                        task["task_key"], "EXECUTION_ERROR",
                        parsed.get("error") or "ComfyUI 执行失败", prompt_id=prompt_id,
                    )
                    job_summary["failed"] += 1
                    continue
                paths, missing = self.extract_task_outputs(task, parsed)
                if paths and not missing and self.outputs_readable(paths):
                    self.ledger.mark_completed(task["task_key"], paths, prompt_id=prompt_id)
                    job_summary["completed"] += 1
                    continue
                if missing:
                    self.ledger.mark_failed(
                        task["task_key"], "OUTPUT_PARTIAL",
                        f"最终保存节点缺少产物：{', '.join(missing)}（实际 {len(paths)} 张）", prompt_id=prompt_id,
                    )
                    job_summary["failed"] += 1
                    continue
                if paths and not self.outputs_readable(paths):
                    self.ledger.mark_failed(
                        task["task_key"], "OUTPUT_INVALID", "产物文件不存在或无法解码", prompt_id=prompt_id,
                    )
                    job_summary["failed"] += 1
                    continue
                if parsed["completed"] or parsed["status_str"] == "success":
                    self.ledger.mark_failed(
                        task["task_key"], "OUTPUT_MISSING",
                        "任务已完成但未找到声明的最终保存节点输出", prompt_id=prompt_id,
                    )
                    job_summary["failed"] += 1
                    continue
                # 已进入历史但还没产出：继续等
                continue
            if prompt_id in active_prompt_ids:
                if task["status"] == "submitted":
                    self.ledger.mark_running(task["task_key"], prompt_id)
                continue
            # 不在历史、不在队列：宽限期内等等再看，否则判 unknown
            enqueued = _parse_iso(task.get("enqueued_at")) or _parse_iso(task.get("updated_at")) or now
            if now - enqueued < max(self.config.poll_grace, 0.0):
                continue
            entry = self.bridge.history(prompt_id) if self.bridge.available else None
            if entry is not None:
                parsed = self.bridge.parse_history(entry)
                paths, missing = self.extract_task_outputs(task, parsed)
                if paths and not missing and self.outputs_readable(paths):
                    self.ledger.mark_completed(task["task_key"], paths, prompt_id=prompt_id)
                    job_summary["completed"] += 1
                    continue
                if missing:
                    self.ledger.mark_failed(
                        task["task_key"], "OUTPUT_PARTIAL",
                        f"最终保存节点缺少产物：{', '.join(missing)}", prompt_id=prompt_id,
                    )
                    job_summary["failed"] += 1
                    continue
            self.ledger.mark_unknown(task["task_key"], "任务不在 ComfyUI 队列/历史中（可能服务重启），等待恢复")
            job_summary["failed"] += 1
            # 超时：中断并判失败
            if enqueued and now - enqueued > self.config.task_timeout:
                self.bridge.interrupt(prompt_id) if self.bridge.available else None
                self.ledger.mark_failed(
                    task["task_key"], "TIMEOUT",
                    f"任务超过 {int(self.config.task_timeout)} 秒未完成，已尝试中断",
                    prompt_id=prompt_id,
                )
                job_summary["failed"] += 1

    def _dispatch_job(self, job_id: str, job_summary: dict) -> None:
        job = self.ledger.get_job_set(job_id)
        templates = self.ledger.job_templates(job_id)
        while True:
            # 审计 P0-2：prompt_id 与“领取”同事务落账，消除并发预登记窗口。
            prompt_id = str(uuid.uuid4())
            # 队列拆分：任务集可请求 1~4 条独立 prompt 同时排队；config 值是下限保护，
            # 不能再用它覆盖 job 的 in_flight（否则 UI 请求 4 条也会退回 1 条）。
            # 任务集显式 in_flight 优先；config.max_in_flight 只在任务集未指定时作为默认值。
            job_limit = int(job.get("in_flight") or self.config.max_in_flight or 1)
            task = self.ledger.claim_next_task(
                job_id, max_in_flight=job_limit, prompt_id=prompt_id,
            )
            if task is None:
                return
            task_json = task.get("task") or {}
            try:
                plan = job["plan"]
                prompt, info = self._prepare_prompt(job_id, plan, templates, task_json)
                self.ledger.prepare_submission(
                    task["task_key"], prompt_id,
                    node_id=info.get("node_id"),
                    save_prefixes=info.get("save_prefixes"),
                )
                try:
                    submitted_id = self.bridge.submit(
                        prompt, prompt_id=prompt_id,
                        extra_data={"aiksk_pc": {"job_set_id": job_id, "task_key": task["task_key"]}},
                    )
                except TypeError:
                    submitted_id = self.bridge.submit(prompt, prompt_id=prompt_id)
                if submitted_id:
                    prompt_id = str(submitted_id)
                self.ledger.mark_running(
                    task["task_key"], prompt_id,
                    node_id=info.get("node_id"),
                    save_prefixes=info.get("save_prefixes"),
                )
                job_summary["submitted"] += 1
            except planner.PlanError as exc:
                self.ledger.mark_failed(task["task_key"], exc.code, exc.message)
                job_summary["failed"] += 1
            except RunnerError as exc:
                self.ledger.mark_failed(task["task_key"], exc.code, exc.message)
                job_summary["failed"] += 1
            except catalog_module.CatalogError as exc:
                self.ledger.mark_failed(task["task_key"], exc.code, exc.message)
                job_summary["failed"] += 1
            except Exception as exc:  # pragma: no cover - 防御
                LOGGER.exception("[AIKSK-PC] 提交任务失败 %s", task["task_key"])
                self.ledger.mark_failed(task["task_key"], "SUBMIT_FAILED", f"提交任务异常：{exc}")
                job_summary["failed"] += 1

    def _prepare_prompt(self, job_id: str, plan: Mapping[str, Any], templates: Mapping[str, Any],
                        task_json: Mapping[str, Any]) -> tuple[dict, dict]:
        key = "dual" if plan.get("dual_compare") else str(task_json.get("model"))
        template = templates.get(key)
        if not template or not template.get("prompt"):
            raise RunnerError("TEMPLATE_MISSING", f"任务集缺少模板：{key}")
        try:
            pose_item = catalog_module.item_info("pose", task_json["pose_id"], self.config)
            costume_item = catalog_module.item_info("costume", task_json["costume_id"], self.config)
        except catalog_module.CatalogError as exc:
            raise RunnerError(exc.code, exc.message) from exc
        # 与节点 run() 的渲染完全一致：external_prompt 非空优先，否则用
        # 图库标签（姿势：姿势名称 / 角度；服装：英文关键词）替换 {pose}/{costume}。
        external_text = planner.template_external_prompt(template["prompt"], template.get("node_id"))
        if external_text:
            rendered = external_text
        else:
            rendered = planner.render_prompt(plan.get("prompt_template"), pose_item, costume_item)
        prompt, info = planner.apply_task_to_prompt(
            template["prompt"],
            task_json,
            plan,
            job_id=job_id,
            node_id=template.get("node_id"),
            rendered_prompt=rendered,
        )
        info["task_key"] = task_json.get("task_key")
        return prompt, info

    # -- 控制 ----------------------------------------------------------

    def pause(self, job_set_id: str) -> dict:
        return self.ledger.pause_job(job_set_id)

    def resume(self, job_set_id: str) -> dict:
        reset = self.ledger.reset_unknown(job_set_id)
        job = self.ledger.resume_job(job_set_id)
        tick_summary = self.tick()
        return {"job_set_id": job_set_id, "status": job["status"], "reset_unknown": reset,
                "tick": tick_summary}

    def cancel(self, job_set_id: str) -> dict:
        result = self.ledger.cancel_job(job_set_id)
        interrupted = 0
        if self.bridge.available:
            for prompt_id in result.get("interrupt_prompt_ids") or []:
                if self.bridge.interrupt(prompt_id):
                    interrupted += 1
                self.bridge.delete_queued(prompt_id)
        result["interrupted"] = interrupted
        return result

    def retry_failed(self, job_set_id: str) -> dict:
        result = self.ledger.retry_failed(job_set_id)
        result["tick"] = self.tick()
        return result

    def has_active_work(self) -> bool:
        """后台服务是否还需要继续推进：running 任务集或有 in-flight 任务。"""
        try:
            running = self.ledger.list_job_sets(limit=200, status="running")
            inflight = self.ledger.inflight_tasks()
        except Exception:  # pragma: no cover - 账本异常时不再空转
            return False
        return bool(running) or bool(inflight)

    def status_snapshot(self) -> dict:
        return {
            "started": self._started,
            "recover": self._recover_summary,
            "last_tick": self._last_tick,
            "last_tick_at": self._last_tick_at,
            "bridge": self.bridge.status_snapshot() if hasattr(self.bridge, "status_snapshot") else None,
            "config": self.config.snapshot(),
        }


__all__ = [
    "BatchRunner",
    "ComfyBridge",
    "CLIENT_ID",
    "RunnerError",
    "OUTPUT_IMAGE_EXTENSIONS",
]

