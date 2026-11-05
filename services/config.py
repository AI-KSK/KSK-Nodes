"""AIKSK Pose × Costume 节点后端运行配置。

所有路径 / 限流 / 超时均可通过环境变量覆盖，便于：
* 在不同 ComfyUI 部署（便携版、源码版、测试 base）之间迁移；
* 后端单元测试注入临时目录，不碰真实图库与真实账本。

设计约束：
* 本模块只依赖标准库；没有 ComfyUI 时也能安全 import。
* 不创建任何目录；真正需要落盘的动作（账本初始化、输出扫描）由调用方显式触发。
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path

# 节点包根目录：.../comfyui_nodes/KSK_PoseCostumeMatrixBatch
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
# 工程根目录：.../AIKSK_PoseCostume_4090_48G
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

# ---- 环境变量名（契约 §4 / §6） -------------------------------------------
ENV_POSE_ROOT = "AIKSK_PC_POSE_ROOT"
ENV_COSTUME_ROOT = "AIKSK_PC_COSTUME_ROOT"
ENV_LEDGER_PATH = "AIKSK_PC_LEDGER_PATH"
ENV_OUTPUT_ROOT = "AIKSK_PC_OUTPUT_ROOT"
ENV_MAX_IN_FLIGHT = "AIKSK_PC_MAX_IN_FLIGHT"
ENV_POLL_INTERVAL = "AIKSK_PC_POLL_INTERVAL"
ENV_TASK_TIMEOUT = "AIKSK_PC_TASK_TIMEOUT"
ENV_QUEUE_ARITY = "AIKSK_PC_QUEUE_ARITY"
ENV_RECOVER_ON_START = "AIKSK_PC_RECOVER_ON_START"
ENV_POLL_GRACE = "AIKSK_PC_POLL_GRACE"

# 默认值（与 services/__init__.py 的常量保持一致）
DEFAULT_MAX_IN_FLIGHT = 1
HARD_MAX_IN_FLIGHT = 4
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_POLL_GRACE = 5.0
DEFAULT_TASK_TIMEOUT = 3600.0

_TRUE_WORDS = {"1", "true", "yes", "on", "y", "是"}
_FALSE_WORDS = {"0", "false", "no", "off", "n", "否"}


def _folder_paths():
    """惰性返回 ComfyUI 的 folder_paths 模块；不存在时返回 None。"""
    try:
        import folder_paths  # type: ignore

        return folder_paths
    except Exception:  # pragma: no cover - 无 ComfyUI 环境
        return None


def comfyui_root() -> Path | None:
    """返回 ComfyUI 根目录（尽量推导）；无法判断时返回 None。"""
    module = _folder_paths()
    if module is None:
        return None
    base = getattr(module, "base_path", None)
    if base:
        return Path(base).resolve()
    filename = getattr(module, "__file__", None)
    if filename:
        return Path(filename).resolve().parent
    return None


def custom_nodes_dirs() -> list[Path]:
    """返回 ComfyUI 的 custom_nodes 目录候选（可能有多个，取决于额外路径配置）。"""
    module = _folder_paths()
    dirs: list[Path] = []
    if module is not None:
        try:
            mapping = getattr(module, "folder_names_and_paths", {})
            entry = mapping.get("custom_nodes")
            if entry and entry[0]:
                dirs.extend(Path(item).expanduser() for item in entry[0])
        except Exception:  # pragma: no cover - 版本差异
            pass
    root = comfyui_root()
    if root is not None:
        dirs.append(root / "custom_nodes")
    unique: list[Path] = []
    seen: set[str] = set()
    for item in dirs:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def detected_comfyui_root() -> Path | None:
    return comfyui_root()


def user_directory() -> Path:
    """ComfyUI 用户目录；无 ComfyUI 时退回工程根 ``user/``。"""
    module = _folder_paths()
    if module is not None:
        try:
            value = module.get_user_directory()
            if value:
                return Path(value).expanduser().resolve()
        except Exception:  # pragma: no cover
            pass
    return (PROJECT_ROOT / "user").resolve()


def output_directory() -> Path:
    """ComfyUI 输出目录（产物对账用）；无 ComfyUI 时退回工程根 ``output/``。"""
    module = _folder_paths()
    if module is not None:
        try:
            value = module.get_output_directory()
            if value:
                return Path(value).expanduser().resolve()
        except Exception:  # pragma: no cover
            pass
    return (PROJECT_ROOT / "output").resolve()


def default_ledger_path() -> Path:
    """账本默认位置：``<user>/aiksk_pc/ledger.sqlite3``。"""
    return user_directory() / "aiksk_pc" / "ledger.sqlite3"


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    return Path(os.path.expandvars(raw)).expanduser()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    return default


def clamp_in_flight(value: int) -> int:
    """in-flight 并发：默认 1，硬上限 4（契约 §6 限流）。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_IN_FLIGHT
    return max(1, min(HARD_MAX_IN_FLIGHT, value))


@dataclass(frozen=True)
class Config:
    """节点后端配置快照（不可变；测试用 ``with_overrides`` 派生新配置）。"""

    pose_root: Path | None = None
    """显式姿势图库根目录；None 表示按契约自动发现。"""

    costume_root: Path | None = None
    """显式服装图库根目录；None 表示按契约自动发现。"""

    ledger_path: Path | None = None
    """SQLite 账本路径；None 表示 ``<user>/aiksk_pc/ledger.sqlite3``。"""

    output_root: Path | None = None
    """产物对账根目录；None 表示 ComfyUI 输出目录。"""

    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    """同一任务集同时提交到 ComfyUI 队列的任务数（1..4）。"""

    poll_interval: float = DEFAULT_POLL_INTERVAL
    """两次 tick 之间的建议间隔（秒）；仅前端/路由轮询参考，不做后台定时提交。"""

    poll_grace: float = DEFAULT_POLL_GRACE
    """任务刚入队后的宽限时间（秒），宽限期内未出现在队列/历史中不算丢失。"""

    task_timeout: float = DEFAULT_TASK_TIMEOUT
    """单任务从入队到完成的最长等待（秒）；超时判失败并尝试中断。"""

    recover_on_start: bool = True
    """runner 首次启动时是否执行输出目录产物对账。"""

    queue_arity: int | None = None
    """PromptQueue 入队元组元素个数（4/5/6）；None 表示自动探测。"""

    def resolve_ledger_path(self) -> Path:
        return (self.ledger_path or default_ledger_path()).expanduser()

    def resolve_output_root(self) -> Path:
        return (self.output_root or output_directory()).expanduser()

    def resolve_pose_root(self) -> Path | None:
        return self.pose_root.expanduser() if self.pose_root else None

    def resolve_costume_root(self) -> Path | None:
        return self.costume_root.expanduser() if self.costume_root else None

    def normalized(self) -> "Config":
        return self.with_overrides(max_in_flight=clamp_in_flight(self.max_in_flight))

    def with_overrides(self, **overrides) -> "Config":
        legal = {field.name for field in dataclasses.fields(self)}
        unknown = set(overrides) - legal
        if unknown:
            raise TypeError(f"未知配置项：{sorted(unknown)}")
        clean = {key: value for key, value in overrides.items() if value is not None}
        result = dataclasses.replace(self, **clean) if clean else self
        return dataclasses.replace(
            result,
            max_in_flight=clamp_in_flight(result.max_in_flight),
            poll_interval=max(0.05, float(result.poll_interval)),
            poll_grace=max(0.0, float(result.poll_grace)),
            task_timeout=max(1.0, float(result.task_timeout)),
        )

    def snapshot(self) -> dict:
        """给 ``/status`` 与报告用的可 JSON 序列化快照（不泄露无关环境变量）。"""
        return {
            "pose_root": str(self.resolve_pose_root()) if self.pose_root else None,
            "costume_root": str(self.resolve_costume_root()) if self.costume_root else None,
            "ledger_path": str(self.resolve_ledger_path()),
            "output_root": str(self.resolve_output_root()),
            "max_in_flight": int(self.max_in_flight),
            "poll_interval": float(self.poll_interval),
            "poll_grace": float(self.poll_grace),
            "task_timeout": float(self.task_timeout),
            "recover_on_start": bool(self.recover_on_start),
            "queue_arity": int(self.queue_arity) if self.queue_arity else None,
        }


def load_config(**overrides) -> Config:
    """从环境变量 + 显式 overrides 组装配置。

    优先级：显式 overrides > 环境变量 > 默认值。
    """
    base = Config(
        pose_root=_env_path(ENV_POSE_ROOT),
        costume_root=_env_path(ENV_COSTUME_ROOT),
        ledger_path=_env_path(ENV_LEDGER_PATH),
        output_root=_env_path(ENV_OUTPUT_ROOT),
        max_in_flight=_env_int(ENV_MAX_IN_FLIGHT, DEFAULT_MAX_IN_FLIGHT, 1, HARD_MAX_IN_FLIGHT),
        poll_interval=_env_float(ENV_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, 0.05, 3600.0),
        poll_grace=_env_float(ENV_POLL_GRACE, DEFAULT_POLL_GRACE, 0.0, 3600.0),
        task_timeout=_env_float(ENV_TASK_TIMEOUT, DEFAULT_TASK_TIMEOUT, 1.0, 86400.0),
        recover_on_start=_env_bool(ENV_RECOVER_ON_START, True),
        queue_arity=_env_int(ENV_QUEUE_ARITY, 0, 0, 6) or None,
    )
    if overrides:
        base = base.with_overrides(**overrides)
    return base


_DEFAULT_CONFIG: Config | None = None


def get_config() -> Config:
    """进程内默认配置（惰性创建、线程安全读取）。"""
    global _DEFAULT_CONFIG
    if _DEFAULT_CONFIG is None:
        _DEFAULT_CONFIG = load_config()
    return _DEFAULT_CONFIG


def set_config(config: Config) -> Config:
    """替换进程默认配置（测试/路由注入用），返回旧配置。"""
    global _DEFAULT_CONFIG
    previous = _DEFAULT_CONFIG
    _DEFAULT_CONFIG = config.normalized()
    return previous


def reset_config() -> None:
    """清空默认配置缓存，下次 ``get_config`` 重新读取环境变量。"""
    global _DEFAULT_CONFIG
    _DEFAULT_CONFIG = None


__all__ = [
    "Config",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    "DEFAULT_MAX_IN_FLIGHT",
    "HARD_MAX_IN_FLIGHT",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_POLL_GRACE",
    "DEFAULT_TASK_TIMEOUT",
    "clamp_in_flight",
    "comfyui_root",
    "custom_nodes_dirs",
    "default_ledger_path",
    "detected_comfyui_root",
    "get_config",
    "load_config",
    "output_directory",
    "reset_config",
    "set_config",
    "user_directory",
]
