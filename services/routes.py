"""HTTP 路由（前缀 ``/aiksk_pc``，契约 §6）。

* ``build_routes(app_or_route_table)``：可注册到 aiohttp ``Application``（测试），
  也可注册到 ComfyUI ``PromptServer.instance.routes``（``RouteTableDef``）；
* ``register_routes()``：幂等；没有 ComfyUI 时安全返回 False；
* 所有阻塞 IO（图库索引、SQLite、runner tick）都走 ``asyncio.to_thread``；
* 错误统一 ``{"error": {"code", "message", "detail"}}``；
* ``/plan`` 是纯 dry-run，**绝不入队**；``/submit`` 服务端重新校验后再入账本。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from aiohttp import web

from . import PLUGIN_VERSION
from . import catalog as catalog_module
from . import config as config_module
from . import planner
from .ledger import ACTIVE_JOB_STATUSES, JOB_STATUSES, TASK_STATUSES, Ledger, LedgerError
from .runner import BatchRunner, ComfyBridge, RunnerError
from .service import BackgroundRunner

LOGGER = logging.getLogger("AIKSK.PoseCostumeMatrix.routes")

ROUTE_PREFIX = "/aiksk_pc"
STATE_KEY = "aiksk_pc_state"
OVERRIDES_KEY = "aiksk_pc_overrides"

MAX_BODY_TASKS = 10_000
WARN_TASKS = 100
MAX_JSON_BYTES = 16 * 1024 * 1024
RATE_LIMIT_WINDOW = 60.0
RATE_LIMIT_MAX = 120
ALLOW_REMOTE = os.environ.get("AIKSK_PC_ALLOW_REMOTE", "0") == "1"
API_TOKEN = os.environ.get("AIKSK_PC_API_TOKEN", "")
_RATE_LOCK = threading.RLock()
_RATE_BUCKETS: dict[str, list[float]] = {}

# 本地模型清单（与 workflows/*_API.json 中的 loader 文件名一致）
MODEL_SPECS: dict[str, dict] = {
    "qwen21": {
        "label": "Qwen-Image 2.1",
        "required": {
            "diffusion_models": "qwen_image_2.1_int8_convrot.safetensors",
            "text_encoders": "qwen3vl_8b_int8_convrot.safetensors",
            "vae": "qwen_image_2.1_vae_bf16.safetensors",
        },
        "optional": {
            "loras": "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors",
            "controlnet": "Qwen-Image-2.1-Fun-Controlnet-Union-ComfyUI.safetensors",
        },
    },
    "flux3klein": {
        "label": "FLUX.3 Klein",
        "required": {
            "diffusion_models": "flux3_image_klein_sd_int8_convrot.safetensors",
            "text_encoders": "qwen3vl_4b_int8_convrot.safetensors",
            "vae": "flux2-vae.safetensors",
        },
        "optional": {
            "diffusion_models_alt": "flux3_image_klein_base_int8_convrot.safetensors",
        },
    },
}

_STATUS_BY_CODE = {
    "BAD_REQUEST": 400,
    "BAD_JSON": 400,
    "PLAN_INVALID": 400,
    "PAIR_INVALID": 400,
    "PAIR_MODE_INVALID": 400,
    "MANUAL_INVALID": 400,
    "MANUAL_REQUIRED": 400,
    "SELECTION_INVALID": 400,
    "SELECTION_TOO_LARGE": 400,
    "NO_PAIRS": 400,
    "NO_MODEL": 400,
    "MODEL_INVALID": 400,
    "SAMPLES_INVALID": 400,
    "SEED_INVALID": 400,
    "SEED_POLICY_INVALID": 400,
    "PROMPT_INVALID": 400,
    "SOURCE_INVALID": 400,
    "KIND_INVALID": 400,
    "VARIANT_INVALID": 400,
    "BAD_SIZE": 400,
    "BAD_RESIZE_MODE": 400,
    "GALLERY_MISSING": 400,
    "INDEX_INVALID": 400,
    "TEMPLATE_INVALID": 400,
    "TEMPLATE_MISSING": 400,
    "CONFIRM_REQUIRED": 400,
    "CONFIRM_LARGE_REQUIRED": 400,
    "TOO_MANY_TASKS": 413,
    "TOO_MANY_PAIRS": 413,
    "IMAGE_TOO_LARGE": 413,
    "ASSET_NOT_FOUND": 404,
    "ASSET_MISSING": 404,
    "ASSET_INVALID": 404,
    "IMAGE_INVALID": 422,
    "JOB_NOT_FOUND": 404,
    "TASK_NOT_FOUND": 404,
    "JOB_CONFLICT": 409,
    "STATUS_INVALID": 409,
    "FORBIDDEN_ORIGIN": 403,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "COMFY_UNAVAILABLE": 503,
    "TORCH_MISSING": 503,
    "PIL_MISSING": 503,
    "INTERNAL": 500,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, detail: Any = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.status = status or _STATUS_BY_CODE.get(code, 400)


def _json(payload: Any, *, status: int = 200, headers: Mapping[str, str] | None = None) -> web.Response:
    merged = {"Cache-Control": "no-store"}
    if headers:
        merged.update(headers)
    return web.json_response(
        payload,
        status=status,
        headers=merged,
        dumps=lambda value: json.dumps(value, ensure_ascii=False),
    )


def _error_response(code: str, message: str, detail: Any = None, status: int | None = None) -> web.Response:
    return _json(
        {"error": {"code": code, "message": message, "detail": detail}},
        status=status or _STATUS_BY_CODE.get(code, 400),
    )


# ---------------------------------------------------------------------------
# 应用状态（惰性创建：只有真正收到请求时才打开 SQLite / 读取图库）
# ---------------------------------------------------------------------------


class AppState:
    def __init__(self, app: web.Application | None,
                 *,
                 config: config_module.Config | None = None,
                 ledger: Ledger | None = None,
                 runner: BatchRunner | None = None,
                 bridge: ComfyBridge | None = None) -> None:
        self.config = config or config_module.get_config()
        self.ledger = ledger if ledger is not None else Ledger(config=self.config)
        if runner is not None:
            self.runner = runner
        else:
            self.runner = BatchRunner(self.ledger, bridge=bridge, config=self.config)

    @property
    def bridge(self) -> Any:
        return getattr(self.runner, "bridge", None)


# 用模块级弱引用表保存每个 app 的状态，避免往已启动的 aiohttp Application 里写 key
# （那会触发 aiohttp 的 deprecation warning）。
_APP_STATES: "weakref.WeakKeyDictionary[web.Application, AppState]" = weakref.WeakKeyDictionary()
_APP_OVERRIDES: "weakref.WeakKeyDictionary[web.Application, dict]" = weakref.WeakKeyDictionary()
_STATE_LOCK = threading.RLock()
_GLOBAL_STATE: "AppState | None" = None
_GLOBAL_BACKGROUND: "BackgroundRunner | None" = None
_GLOBAL_LOCK = threading.RLock()


def _now() -> str:
    from .ledger import utc_now

    return utc_now()


def get_global_state() -> AppState:
    """进程级唯一状态：HTTP 请求与后台推进器共用同一个 ledger/runner。"""
    global _GLOBAL_STATE
    with _GLOBAL_LOCK:
        if _GLOBAL_STATE is None:
            _GLOBAL_STATE = AppState(None)
        return _GLOBAL_STATE


def ensure_background(state: AppState | None = None) -> dict:
    """确保服务端推进器在运行（审计 P0-1）；不会自动创建新任务集。"""
    global _GLOBAL_BACKGROUND
    state = state or get_global_state()
    with _GLOBAL_LOCK:
        if _GLOBAL_BACKGROUND is None or _GLOBAL_BACKGROUND.state is not state:
            _GLOBAL_BACKGROUND = BackgroundRunner(state)
        service = _GLOBAL_BACKGROUND

    def _start() -> None:
        try:
            service.start()
        except Exception:  # pragma: no cover
            LOGGER.exception("[AIKSK-PC] 启动后台推进器失败")

    try:
        from server import PromptServer  # type: ignore

        loop = getattr(PromptServer.instance, "loop", None)
    except Exception:
        loop = None
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_start)
    # 测试环境没有 ComfyUI 事件循环时不要偷偷启动（会与显式 tick 的测试竞争）；
    # 需要后台推进的测试会直接实例化 BackgroundRunner。
    return service.status()


def get_background_status() -> dict | None:
    service = _GLOBAL_BACKGROUND
    return service.status() if service is not None else None


def _ensure_state(app: web.Application, overrides: Mapping[str, Any] | None) -> AppState:
    with _STATE_LOCK:
        state = _APP_STATES.get(app)
        if isinstance(state, AppState):
            return state
        raw = dict(overrides or _APP_OVERRIDES.get(app) or {})
        # ComfyUI RouteTableDef 路径下 build_routes 传入的全 None overrides 必须视为“无覆盖”，
        # 否则每个请求会创建第二个 AppState/runner（审计 P0-1 的双调度根因）。
        options = {key: value for key, value in raw.items() if value is not None}
        if not options:
            global _GLOBAL_STATE
            if _GLOBAL_STATE is None:
                _GLOBAL_STATE = AppState(app)
            _APP_STATES[app] = _GLOBAL_STATE
            return _GLOBAL_STATE
        state = AppState(app, **options)
        _APP_STATES[app] = state
        return state


def get_state(app: web.Application) -> AppState:
    """测试/集成用：强制创建并返回应用状态。"""
    return _ensure_state(app, _APP_OVERRIDES.get(app) or {})


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------


def _mutating_access(request: web.Request) -> web.Response | None:
    """审计 P1-4：写接口默认仅本机；远程需显式开启并带 token。"""
    remote = (request.remote or "").split("%")[0]
    if remote in ("127.0.0.1", "::1", "localhost", ""):
        return None
    if not ALLOW_REMOTE:
        return _error_response("REMOTE_FORBIDDEN", "写接口仅允许本机访问（远程请配置 AIKSK_PC_ALLOW_REMOTE=1 + token）", {"remote": remote}, 403)
    if API_TOKEN and request.headers.get("X-AIKSK-Token") != API_TOKEN:
        return _error_response("UNAUTHORIZED", "缺少或错误的 X-AIKSK-Token", None, 401)
    return None


def _rate_limit(request: web.Request) -> web.Response | None:
    remote = (request.remote or "unknown").split("%")[0]
    now = time.time()
    with _RATE_LOCK:
        bucket = [stamp for stamp in _RATE_BUCKETS.get(remote, []) if now - stamp < RATE_LIMIT_WINDOW]
        if len(bucket) >= RATE_LIMIT_MAX:
            _RATE_BUCKETS[remote] = bucket
            return _error_response("RATE_LIMITED", "写请求过于频繁，请稍后重试", {"window": RATE_LIMIT_WINDOW, "max": RATE_LIMIT_MAX}, 429)
        bucket.append(now)
        _RATE_BUCKETS[remote] = bucket
    return None


def _same_site(request: web.Request) -> web.Response | None:
    """同站 Origin 校验（CSRF 防护）；无 Origin 头（同源 fetch 经常省略）则放行。"""
    origin = request.headers.get("Origin")
    if not origin:
        return None
    try:
        host = urlsplit(origin).netloc.lower()
    except ValueError:
        host = ""
    request_host = (request.host or "").lower()
    if host and request_host and host != request_host:
        return _error_response("FORBIDDEN_ORIGIN", "仅接受来自本页面的请求", {"origin": origin, "host": request.host})
    return None


async def _read_json(request: web.Request) -> dict:
    if request.content_type not in ("application/json", "text/json"):
        raise ApiError("UNSUPPORTED_MEDIA_TYPE", "请使用 application/json 提交")
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        raise ApiError("PAYLOAD_TOO_LARGE", f"请求体超过 {MAX_JSON_BYTES // (1024 * 1024)} MB 上限", None, 413)
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApiError("BAD_REQUEST", f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise ApiError("BAD_REQUEST", "请求体必须是 JSON 对象")
    return body


def _classify_exception(exc: BaseException) -> web.Response:
    if isinstance(exc, ApiError):
        return _error_response(exc.code, exc.message, exc.detail, exc.status)
    code = getattr(exc, "code", None)
    message = str(exc) or type(exc).__name__
    if isinstance(code, str) and code:
        return _error_response(code, message, getattr(exc, "detail", None))
    if isinstance(exc, (planner.PlanError, catalog_module.CatalogError, LedgerError, RunnerError)):
        return _error_response(getattr(exc, "code", "INTERNAL"), message)
    LOGGER.exception("[AIKSK-PC] 路由内部错误")
    return _error_response("INTERNAL", f"服务器内部错误：{type(exc).__name__}: {exc}", None, 500)


def _handle(inner):
    async def wrapper(request: web.Request) -> web.Response:
        try:
            if request.method in ("POST", "PUT", "DELETE", "PATCH"):
                blocked = _mutating_access(request) or _rate_limit(request) or _same_site(request)
                if blocked is not None:
                    return blocked
            return await inner(request)
        except BaseException as exc:  # noqa: BLE001 - 统一转错误 JSON
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _classify_exception(exc)

    wrapper.__name__ = getattr(inner, "__name__", "aiksk_pc_handler")
    wrapper.__doc__ = getattr(inner, "__doc__", None)
    return wrapper


def _count_ksk_nodes() -> dict:
    """统计 ComfyUI 里 KSK 相关节点数量（契约 §8-11 应为 0）。"""
    try:
        import nodes as comfy_nodes  # type: ignore
    except Exception:
        return {"loaded": False, "count": 0, "names": []}
    mapping = getattr(comfy_nodes, "NODE_CLASS_MAPPINGS", None) or {}
    names = []
    try:
        for name in mapping:
            upper = str(name).upper()
            if upper.startswith("KSK") or upper.startswith("AI_KSK") or upper.startswith("AI-KSK"):
                names.append(str(name))
    except Exception:  # pragma: no cover
        pass
    return {"loaded": True, "count": len(names), "names": sorted(names)[:20]}


def _collect_status(state: AppState) -> dict:
    galleries = catalog_module.gallery_status(state.config)
    try:
        ledger_stats = state.ledger.stats()
    except Exception as exc:  # pragma: no cover - 账本异常不阻塞 /status
        ledger_stats = {"error": str(exc)}
    bridge = state.bridge
    bridge_status = None
    if bridge is not None and hasattr(bridge, "status_snapshot"):
        try:
            bridge_status = bridge.status_snapshot()
        except Exception:  # pragma: no cover
            bridge_status = None
    runner_status = None
    if hasattr(state.runner, "status_snapshot"):
        try:
            runner_status = state.runner.status_snapshot()
        except Exception:  # pragma: no cover
            runner_status = None
    return {
        "ok": True,
        "plugin": "KSK_PoseCostumeMatrixBatch",
        "version": PLUGIN_VERSION,
        "comfyui_version": getattr(bridge, "version", None) if bridge is not None else None,
        "gallery": galleries,
        "ksk": _count_ksk_nodes(),
        "bridge": bridge_status,
        "ledger": ledger_stats,
        "runner": runner_status,
        "background": get_background_status(),
        "config": state.config.snapshot(),
        "prefix": ROUTE_PREFIX,
    }


def _plan_preview(state: AppState, body: Mapping[str, Any]) -> dict:
    warnings: list[str] = []
    plan_value = body.get("plan", body)
    plan = planner.build_plan(plan_value, warnings=warnings)
    counts = planner.plan_counts(plan)
    planner.validate_limits(counts)
    warnings.extend(planner.plan_warnings(plan))
    errors: list[dict] = []
    templates_ok = False
    templates = body.get("templates")
    if templates:
        try:
            planner.resolve_templates(plan, templates, warnings=warnings)
            templates_ok = True
        except planner.PlanError as exc:
            errors.append({"code": exc.code, "message": exc.message})
    try:
        limit = int(body.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    preview = planner.tasks_preview(plan, limit=limit)
    return {
        "ok": not errors,
        "counts": counts,
        "tasks_preview": preview,
        "warnings": warnings,
        "errors": errors,
        "templates_ok": templates_ok,
        "plan": plan,
    }


# ---------------------------------------------------------------------------
# 路由实现
# ---------------------------------------------------------------------------


def _model_verification_path(config: "config_module.Config | None") -> Path:
    try:
        base = config.resolve_ledger_path() if config is not None else config_module.get_config().resolve_ledger_path()
        return Path(base).parent / "model_verification.json"
    except Exception:  # pragma: no cover
        return Path("model_verification.json")


def _read_model_verification(config: "config_module.Config | None") -> dict:
    path = _model_verification_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_model_verification(config, payload: dict) -> None:
    path = _model_verification_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _workflow_structure_flags() -> dict[str, bool]:
    """检查各模型工作流所需节点类是否已在 ComfyUI 注册。"""
    try:
        import nodes as comfy_nodes  # type: ignore

        mapping = set(getattr(comfy_nodes, "NODE_CLASS_MAPPINGS", {}) or {})
    except Exception:
        mapping = set()
    required = {
        "qwen21": ("UNETLoader", "CLIPLoader", "VAELoader", "TextEncodeQwenImage21", "KSampler"),
        "flux3klein": ("UNETLoader", "CLIPLoader", "VAELoader", "ReferenceMultiLatent", "CLIPTextEncode", "KSampler"),
    }
    return {key: bool(classes) and set(classes) <= mapping for key, classes in required.items()}


def _collect_models(config: "config_module.Config | None" = None) -> dict:
    """检查模型文件 + 工作流结构 + 本机出图验证状态（审计 P1-3 三态）。"""
    payload: dict = {"ok": True, "models": {}, "folders": {}, "verification_file": str(_model_verification_path(config))}
    try:
        import folder_paths  # type: ignore
    except Exception as exc:  # pragma: no cover - CPU/无 ComfyUI 测试环境
        payload["ok"] = False
        payload["error"] = f"folder_paths 不可用：{exc}"
        for key, spec in MODEL_SPECS.items():
            payload["models"][key] = {
                "label": spec["label"], "ready": False, "status": "DEPENDENCY_ERROR",
                "file_visible": False, "workflow_structure_ok": False, "load_verified": False,
                "required": {}, "missing": list(spec["required"].values()),
            }
        return payload

    def _available(folder: str) -> set[str]:
        try:
            return set(folder_paths.get_filename_list(folder))
        except Exception:
            return set()

    cache: dict[str, set[str]] = {}
    file_stats: list[str] = []
    structure = _workflow_structure_flags()
    for key, spec in MODEL_SPECS.items():
        required: dict[str, dict] = {}
        missing: list[str] = []
        full_paths: list[Path] = []
        for folder, filename in spec["required"].items():
            names = cache.setdefault(folder, _available(folder))
            found = filename in names
            full_path = None
            if found:
                try:
                    full_path = folder_paths.get_full_path(folder, filename)
                except Exception:
                    full_path = None
            stat_text = ""
            if full_path and Path(full_path).is_file():
                path = Path(full_path)
                stat = path.stat()
                stat_text = f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"
                full_paths.append(path)
                file_stats.append(stat_text)
            required[f"{folder}/{filename}"] = {
                "present": found, "folder": folder, "filename": filename,
                "path": str(full_path) if full_path else None,
            }
            if not found:
                missing.append(filename)
        optional = {}
        for folder, filename in spec.get("optional", {}).items():
            real_folder = folder.replace("_alt", "")
            names = cache.setdefault(real_folder, _available(real_folder))
            optional[f"{real_folder}/{filename}"] = filename in names
        file_visible = not missing
        structure_ok = bool(structure.get(key, False))
        payload["models"][key] = {
            "label": spec["label"],
            "ready": bool(file_visible and structure_ok),
            "file_visible": bool(file_visible),
            "workflow_structure_ok": bool(structure_ok),
            "required": required,
            "optional": optional,
            "missing": missing,
        }
    fingerprint = hashlib.sha1(("aiksk.pc.models.v2|" + "|".join(sorted(file_stats))).encode("utf-8")).hexdigest()
    verification = _read_model_verification(config)
    verified_models = (verification.get("models") or {}) if verification.get("fingerprint") == fingerprint else {}
    for key, model in payload["models"].items():
        record = verified_models.get(key) if isinstance(verified_models, dict) else None
        model["load_verified"] = bool(record and record.get("verified_at"))
        model["verified_at"] = record.get("verified_at") if record else None
        model["verification_evidence"] = record.get("evidence") if record else None
        if model["file_visible"] and model["workflow_structure_ok"]:
            model["status"] = "READY" if model["load_verified"] else "FILE_VISIBLE_UNVERIFIED"
        elif not model["file_visible"]:
            model["status"] = "MISSING_WEIGHTS"
        else:
            model["status"] = "DEPENDENCY_ERROR"
    payload["fingerprint"] = fingerprint
    payload["verified_fingerprint_match"] = bool(verification.get("fingerprint") == fingerprint)
    for folder in ("diffusion_models", "text_encoders", "vae", "loras", "controlnet"):
        payload["folders"][folder] = sorted(cache.get(folder) or _available(folder))
    payload["ready_count"] = sum(1 for model in payload["models"].values() if model["file_visible"])
    payload["verified_count"] = sum(1 for model in payload["models"].values() if model["load_verified"])
    return payload


def _build_handlers(overrides: Mapping[str, Any]) -> dict:
    async def get_status(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        payload = await asyncio.to_thread(_collect_status, state)
        return _json(payload)

    async def get_models(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        payload = await asyncio.to_thread(_collect_models, state.config)
        return _json(payload, headers={"Cache-Control": "no-cache"})

    async def post_models_verify(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        body = await _read_json(request)
        names = body.get("models") or list(MODEL_SPECS)
        if not isinstance(names, list) or not names:
            raise ApiError("BAD_REQUEST", "models 必须是模型 key 列表")
        unknown = [name for name in names if name not in MODEL_SPECS]
        if unknown:
            raise ApiError("MODEL_INVALID", f"未知模型：{unknown}")
        payload = await asyncio.to_thread(_collect_models, state.config)
        not_visible = [name for name in names if not payload["models"][name]["file_visible"]]
        if not_visible:
            raise ApiError("MISSING_WEIGHTS", f"模型文件不可见，无法登记验证：{not_visible}")
        verification = _read_model_verification(state.config)
        models_record = dict(verification.get("models") or {}) if verification.get("fingerprint") == payload["fingerprint"] else {}
        evidence = body.get("evidence") if isinstance(body.get("evidence"), dict) else {}
        timestamp = _now()
        for name in names:
            record = dict(models_record.get(name) or {})
            record["verified_at"] = timestamp
            record["evidence"] = {**evidence, "fingerprint": payload["fingerprint"]}
            models_record[name] = record
        _write_model_verification(state.config, {
            "schema": "aiksk.pc.model_verification.v1",
            "fingerprint": payload["fingerprint"],
            "updated_at": timestamp,
            "models": models_record,
        })
        refreshed = await asyncio.to_thread(_collect_models, state.config)
        return _json({"ok": True, "verified": names, "models": refreshed["models"],
                      "fingerprint": refreshed["fingerprint"], "verification_file": refreshed["verification_file"]})

    async def get_catalogue(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        kind = request.query.get("kind", "pose")
        payload = await asyncio.to_thread(catalog_module.catalogue_payload, kind, state.config)
        return _json(payload, headers={"Cache-Control": "no-cache"})

    async def get_image(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        kind = request.query.get("kind", "pose")
        item_id = request.query.get("id", "")
        variant = request.query.get("variant", "thumb")
        if not item_id:
            raise ApiError("BAD_REQUEST", "缺少图片 id")
        path = await asyncio.to_thread(catalog_module.image_path, kind, item_id, variant, state.config)
        return web.FileResponse(
            path,
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    async def post_plan(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        body = await _read_json(request)
        payload = await asyncio.to_thread(_plan_preview, state, body)
        return _json(payload)

    async def post_submit(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        body = await _read_json(request)
        if body.get("confirm") is not True:
            raise ApiError("CONFIRM_REQUIRED", "提交批量任务必须显式 confirm=true")
        warnings: list[str] = []
        plan = await asyncio.to_thread(planner.build_plan, body.get("plan", body), warnings=warnings)
        counts = planner.plan_counts(plan)
        await asyncio.to_thread(planner.validate_limits, counts)
        if counts["tasks"] > MAX_BODY_TASKS:
            raise ApiError("TOO_MANY_TASKS", f"任务数 {counts['tasks']} 超过上限 {MAX_BODY_TASKS}", counts)
        if counts["tasks"] > WARN_TASKS and body.get("confirm_large") is not True:
            raise ApiError(
                "CONFIRM_LARGE_REQUIRED",
                f"任务数 {counts['tasks']} 超过 {WARN_TASKS}，必须再传 confirm_large=true",
                counts,
            )
        warnings.extend(planner.plan_warnings(plan))
        templates, template_warnings = await asyncio.to_thread(
            planner.resolve_templates, plan, body.get("templates"), warnings=[]
        )
        warnings.extend(template_warnings)
        start = body.get("start", True) is not False
        result = await asyncio.to_thread(
            state.ledger.create_job_set,
            plan,
            templates,
            in_flight=body.get("in_flight"),
            start=bool(start),
        )
        tick_summary = None
        if result.get("created") and start:
            def _advance() -> dict:
                state.runner.ensure_started()
                return state.runner.tick()

            tick_summary = await asyncio.to_thread(_advance)
        ensure_background(state)
        return _json(
            {
                "ok": True,
                "job_set_id": result["job_set_id"],
                "created": bool(result["created"]),
                "deduplicated": bool(result.get("deduplicated")),
                "counts": result["counts"],
                "status": result["status"],
                "warnings": warnings,
                "tick": tick_summary,
            }
        )

    async def get_jobs(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        status = request.query.get("status") or None
        try:
            limit = int(request.query.get("limit") or 50)
        except ValueError:
            limit = 50
        await asyncio.to_thread(state.runner.tick)
        ensure_background(state)

        def _collect() -> list[dict]:
            jobs = state.ledger.list_job_sets(limit=limit, status=status)
            for job in jobs:
                job["counts"] = state.ledger.task_counts(job["job_set_id"])
            return jobs

        jobs = await asyncio.to_thread(_collect)
        return _json({"ok": True, "count": len(jobs), "jobs": jobs})

    async def get_job(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        job_set_id = request.match_info.get("job_set_id", "")
        include_tasks = request.query.get("tasks", "0") not in ("0", "false", "False", "")
        try:
            limit = int(request.query.get("limit") or 200)
        except ValueError:
            limit = 200
        try:
            offset = int(request.query.get("offset") or 0)
        except ValueError:
            offset = 0
        offset = max(0, offset)
        tick_summary = await asyncio.to_thread(state.runner.tick)

        def _collect() -> dict:
            job = state.ledger.get_job_set(job_set_id)
            counts = job["counts"]
            payload = {
                "ok": True,
                "job": job,
                "counts": counts,
                "tick": tick_summary,
                "tasks": [],
                "next_offset": None,
            }
            if include_tasks:
                tasks = state.ledger.list_tasks(job_set_id, limit=limit, offset=offset)
                payload["tasks"] = tasks
                total = int(counts.get("total") or 0)
                payload["next_offset"] = offset + len(tasks) if offset + len(tasks) < total else None
            return payload

        payload = await asyncio.to_thread(_collect)
        return _json(payload)

    async def job_action(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        job_set_id = request.match_info.get("job_set_id", "")
        action = request.match_info.get("action", "")
        if action not in ("pause", "resume", "cancel", "retry_failed"):
            raise ApiError("BAD_REQUEST", f"未知操作：{action}（支持 pause/resume/cancel/retry_failed）")
        method = getattr(state.runner, action, None)
        if method is None:  # pragma: no cover - 防御
            raise ApiError("INTERNAL", f"runner 不支持操作：{action}")
        result = await asyncio.to_thread(method, job_set_id)
        if action in ("resume", "retry_failed"):
            ensure_background(state)
        job = await asyncio.to_thread(state.ledger.get_job_set, job_set_id)
        return _json({"ok": True, "action": action, "job_set_id": job_set_id,
                      "status": job["status"], "result": result, "job": job})

    async def get_report(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        job_set_id = request.match_info.get("job_set_id", "")
        report = await asyncio.to_thread(state.ledger.report, job_set_id)
        return _json(report)

    async def get_manifest(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        job_set_id = request.match_info.get("job_set_id", "")
        manifest = await asyncio.to_thread(state.ledger.manifest, job_set_id)
        filename = f"aiksk_pc_manifest_{job_set_id}.json"
        return _json(
            manifest,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def post_recover(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        body: dict = {}
        if request.content_length:
            try:
                body = await _read_json(request)
            except ApiError:
                body = {}
        job_set_id = body.get("job_set_id") or None
        summary = await asyncio.to_thread(state.runner.recover, job_set_id)
        ensure_background(state)
        return _json({"ok": True, "recover": summary, "background": get_background_status()})

    async def get_routes_info(request: web.Request) -> web.Response:
        state = _ensure_state(request.app, overrides)
        specs = _default_route_specs()
        return _json({
            "ok": True,
            "prefix": ROUTE_PREFIX,
            "routes": sorted(path for _method, path in specs.values()),
            "job_statuses": list(JOB_STATUSES),
            "active_job_statuses": list(ACTIVE_JOB_STATUSES),
            "task_statuses": list(TASK_STATUSES),
            "plugin": "KSK_PoseCostumeMatrixBatch",
            "version": PLUGIN_VERSION,
            "state": state.config.snapshot(),
        })

    return {
        "status": get_status,
        "models": get_models,
        "models_verify": post_models_verify,
        "catalogue": get_catalogue,
        "image": get_image,
        "plan": post_plan,
        "submit": post_submit,
        "jobs": get_jobs,
        "job": get_job,
        "job_action": job_action,
        "report": get_report,
        "manifest": get_manifest,
        "recover": post_recover,
        "routes_info": get_routes_info,
    }


def _default_route_specs() -> dict:
    """``{name: (method, path)}``；路径均带 ``/aiksk_pc`` 前缀。"""
    return {
        "status": ("get", f"{ROUTE_PREFIX}/status"),
        "models": ("get", f"{ROUTE_PREFIX}/models"),
        "models_verify": ("post", f"{ROUTE_PREFIX}/models/verify"),
        "catalogue": ("get", f"{ROUTE_PREFIX}/catalogue"),
        "image": ("get", f"{ROUTE_PREFIX}/image"),
        "plan": ("post", f"{ROUTE_PREFIX}/plan"),
        "submit": ("post", f"{ROUTE_PREFIX}/submit"),
        "jobs": ("get", f"{ROUTE_PREFIX}/jobs"),
        "job": ("get", f"{ROUTE_PREFIX}/jobs/{{job_set_id}}"),
        "job_action": ("post", f"{ROUTE_PREFIX}/jobs/{{job_set_id}}/{{action}}"),
        "report": ("get", f"{ROUTE_PREFIX}/jobs/{{job_set_id}}/report"),
        "manifest": ("get", f"{ROUTE_PREFIX}/jobs/{{job_set_id}}/manifest"),
        "recover": ("post", f"{ROUTE_PREFIX}/queue/recover"),
        "routes_info": ("get", f"{ROUTE_PREFIX}/routes"),
    }


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def _route_existing_paths(target: Any) -> list[str]:
    """尽量列出已有路由路径（Application / UrlDispatcher / RouteTableDef）。"""
    router = getattr(target, "router", None)
    candidate = router if router is not None else target
    paths: list[str] = []
    routes_method = getattr(candidate, "routes", None)
    if callable(routes_method):
        try:
            for route in routes_method():
                resource = getattr(route, "resource", None)
                path = getattr(resource, "canonical", None)
                if path:
                    paths.append(str(path))
            return paths
        except Exception:  # pragma: no cover
            pass
    for attr in ("_items", "_routes"):
        items = getattr(candidate, attr, None)
        if items is None:
            continue
        try:
            for item in items:
                path = getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else None)
                if path:
                    paths.append(str(path))
        except Exception:  # pragma: no cover
            continue
    return paths


def _add_route(target: Any, method: str, path: str, handler) -> None:
    router = getattr(target, "router", None)
    if router is not None and hasattr(router, f"add_{method}"):
        getattr(router, f"add_{method}")(path, handler)
        return
    if hasattr(target, f"add_{method}"):
        getattr(target, f"add_{method}")(path, handler)
        return
    decorator = getattr(target, method, None)
    if decorator is None:  # pragma: no cover - 防御
        raise ApiError("INTERNAL", f"无法注册路由：{method.upper()} {path}")
    decorator(path)(handler)


def build_routes(target: Any, *, config: config_module.Config | None = None,
                 ledger: Ledger | None = None, runner: BatchRunner | None = None,
                 bridge: ComfyBridge | None = None) -> Any:
    """把 ``/aiksk_pc`` 路由注册到 ``Application`` 或 ``RouteTableDef``（幂等）。"""
    if target is None:
        raise ApiError("INTERNAL", "build_routes 需要 aiohttp Application 或 RouteTableDef")
    specs = _default_route_specs()
    existing = set(_route_existing_paths(target))
    if specs["status"][1] in existing:
        return target
    overrides = {"config": config, "ledger": ledger, "runner": runner, "bridge": bridge}
    app = target if isinstance(target, web.Application) else None
    if app is not None:
        with _STATE_LOCK:
            _APP_OVERRIDES[app] = overrides
    handlers = _build_handlers(overrides)
    for name, (method, path) in specs.items():
        _add_route(target, method, path, _handle(handlers[name]))
    return target


_REGISTERED_TARGETS: set[int] = set()


def register_routes(app: Any = None) -> bool:
    """注册到 ComfyUI（幂等）；没有 ComfyUI 时安全返回 False。"""
    target = app
    if target is None:
        try:
            from server import PromptServer  # type: ignore

            target = PromptServer.instance.routes
        except Exception:
            return False
    if target is None:  # pragma: no cover
        return False
    marker = id(target)
    try:
        build_routes(target)
    except Exception as exc:  # pragma: no cover - 防御
        LOGGER.warning("[AIKSK-PC] 注册路由失败：%s", exc)
        return False
    _REGISTERED_TARGETS.add(marker)

    # 审计 P0-1：ComfyUI 启动后自动恢复已确认的任务集并由服务端推进（无页面轮询也继续）。
    def _boot_background() -> None:
        try:
            state = get_global_state()
            ensure_background(state)
        except Exception as exc:  # pragma: no cover - 启动阶段失败不能拖垮 ComfyUI
            LOGGER.warning("[AIKSK-PC] 后台推进器启动失败：%s", exc)

    try:
        from server import PromptServer  # type: ignore

        loop = getattr(PromptServer.instance, "loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(_boot_background)
        else:  # pragma: no cover
            _boot_background()
    except Exception:  # pragma: no cover
        pass
    return True


__all__ = [
    "ApiError",
    "AppState",
    "ROUTE_PREFIX",
    "STATE_KEY",
    "build_routes",
    "get_state",
    "register_routes",
]
