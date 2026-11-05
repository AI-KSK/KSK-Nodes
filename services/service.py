"""受控的服务端批量推进器（审计 P0-1）。

浏览器轮询只负责展示；真正的补投/轮询/收尾由本后台任务驱动。
约束：
* 只推进**已由用户 confirm 创建**的任务集，不会自动开新任务；
* 仍受 ``runner.tick`` 的 in-flight 上限、暂停/取消/重试语义约束；
* 没有活跃任务时自动退出，空闲时不常驻轮询。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from . import config as config_module
from .runner import BatchRunner

LOGGER = logging.getLogger("AIKSK.PoseCostumeMatrix.service")


class BackgroundRunner:
    """驱动一个 ``BatchRunner`` 的后台协程，接口幂等。"""

    def __init__(self, state, *, interval: float | None = None) -> None:
        self.state = state
        self.runner: BatchRunner = state.runner
        self.interval = float(interval if interval is not None else max(0.5, min(2.0, state.config.poll_interval / 2 if state.config else 1.0)))
        self._task: asyncio.Task | None = None
        self._lock = threading.RLock()
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._ticks = 0
        self._last_tick_at: str | None = None
        self._last_error: str | None = None

    # -- 生命周期 ------------------------------------------------------
    def start(self) -> bool:
        """在事件循环线程内调用；已有存活任务时返回 False。"""
        with self._lock:
            if self._task is not None and not self._task.done():
                return False
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                LOGGER.warning("[AIKSK-PC] BackgroundRunner 必须在事件循环内启动")
                return False
            self._task = loop.create_task(self.run_forever(), name="aiksk-pc-runner")
            self._started_at = _now()
            self._finished_at = None
            return True

    async def run_forever(self) -> dict:
        summary: dict = {"ticks": 0, "started_at": self._started_at, "reason": "idle"}
        try:
            await asyncio.to_thread(self.runner.ensure_started)
            while True:
                await asyncio.to_thread(self.runner.tick)
                self._ticks += 1
                summary["ticks"] = self._ticks
                self._last_tick_at = _now()
                active = await asyncio.to_thread(self.runner.has_active_work)
                if not active:
                    summary["reason"] = "no-active-work"
                    break
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            summary["reason"] = "cancelled"
            raise
        except Exception as exc:  # pragma: no cover - 后台任务不能拖垮服务
            self._last_error = f"{type(exc).__name__}: {exc}"
            summary["reason"] = "error"
            summary["error"] = self._last_error
            LOGGER.exception("[AIKSK-PC] 后台推进异常")
        finally:
            self._finished_at = _now()
            summary["finished_at"] = self._finished_at
        return summary

    async def join(self, timeout: float | None = None) -> None:
        task = self._task
        if task is not None:
            if timeout is None:
                await task
            else:
                await asyncio.wait_for(task, timeout=timeout)

    def stop(self) -> bool:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    # -- 观测 ----------------------------------------------------------
    def status(self) -> dict:
        task = self._task
        running = bool(task is not None and not task.done())
        return {
            "running": running,
            "started_at": self._started_at,
            "finished_at": None if running else self._finished_at,
            "last_tick_at": self._last_tick_at,
            "ticks": self._ticks,
            "interval": self.interval,
            "last_error": self._last_error,
        }


def _now() -> str:
    from .ledger import utc_now

    return utc_now()


__all__ = ["BackgroundRunner"]
