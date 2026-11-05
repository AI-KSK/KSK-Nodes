"""纯函数规划层：组合、计划 JSON、任务展开、seed 策略、模板替换。

本模块是整个节点后端的“无副作用内核”：

* 不 import ComfyUI / torch / aiohttp / 任何 KSK 代码；
* 不做文件 IO，不读写账本；
* 相同输入永远得到相同输出（可测试、可复现）。

契约：``docs/CONTRACT_node_v1.md`` §2 / §3 / §6 / §7。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from . import (
    DEFAULT_PROMPT,
    MAX_PAIRS,
    MAX_SAMPLES_PER_PAIR,
    MAX_TASKS,
    MODEL_KEYS,
    PAIR_MODES,
    SCHEMA_PLAN,
    SEED_POLICIES,
    WARN_OVER_TASKS,
)

MAX_SEED = 2**32 - 1
MAX_PROMPT_LEN = 20_000
MAX_IDS = 2_000
MAX_FILENAME_PREFIX = 180
MAX_PAIR_SEGMENT = 48

TEMPLATE_NODE_CLASS = "KSK_PoseCostumeMatrixBatch"
SAVE_NODE_CLASSES = ("SaveImage", "SaveImageAdvanced")
GENERATION_NODE_CLASSES = {
    "KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced",
    "RandomNoise", "BasicGuider", "CFGGuider", "DualCFGGuider", "SamplerEulerAncestral",
}
SEED_NODE_KEYS = {
    "KSampler": ("seed", "noise_seed"),
    "KSamplerAdvanced": ("noise_seed", "seed"),
    "RandomNoise": ("noise_seed", "seed"),
}
SEED_KEYS_ALLOWED = ("seed", "noise_seed")

TEMPLATE_EXTRA_DATA = {"aiksk_pc": {"version": 1}}

_SOURCE_ALIASES = {
    "builtin": "builtin",
    "内置素材库": "builtin",
    "内置": "builtin",
    "custom": "custom",
    "自定义素材库": "custom",
    "自定义": "custom",
}
_INVALID_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]")
_SAFE_PREFIX_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_TOKEN_RE = re.compile(r"\$\{(AIKSK_[A-Z_]+)\}")


class PlanError(ValueError):
    """带错误码的规划错误；``str(exc)`` 只返回中文消息。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 简单委托
        return self.message


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """稳定 JSON（键排序、无空格、保留中文），用于哈希与指纹。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def now_iso() -> str:
    """本地时区 ISO 时间字符串（精确到秒）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha1_hex(text: str, length: int = 6) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def safe_segment(value: Any, limit: int = MAX_PAIR_SEGMENT) -> str:
    """把 ID 片段清洗为 ``[A-Za-z0-9._-]``；截断时追加 6 位 sha1 防碰撞。"""
    text = "" if value is None else str(value)
    replaced = _INVALID_SEGMENT_RE.sub("_", text)
    stripped = replaced.strip("._-")
    if not stripped:
        stripped = "x"
    if len(stripped) > limit:
        digest = sha1_hex(text, 6)
        stripped = stripped[: max(1, limit - 6)] + digest
        stripped = stripped[:limit]
    return stripped


def pair_id_for(pose_id: Any, costume_id: Any) -> str:
    """契约 §3：``pair_id = "PC-" + safe(pose_id) + "-" + safe(costume_id)``。"""
    return f"PC-{safe_segment(pose_id)}-{safe_segment(costume_id)}"


def normalize_source(source: Any) -> str:
    """把来源规范成 ``builtin`` / ``custom``。"""
    if source is None:
        return "builtin"
    text = str(source).strip()
    if not text:
        return "builtin"
    lowered = text.lower()
    if lowered in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[lowered]
    if text in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[text]
    raise PlanError("SOURCE_INVALID", f"未知素材库来源：{text}（仅支持 builtin / custom）")


def _require_int(value: Any, field: str, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError(code, f"{field} 必须是整数，收到：{value!r}")
    return value


def _parse_json_value(value: Any, field: str) -> Any:
    """接受已经是对象的值，或 JSON 字符串（节点三个 STRING widget 的形态）。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            raise PlanError("BAD_JSON", f"{field} 不是合法 JSON：{exc}") from exc
    raise PlanError("BAD_JSON", f"{field} 必须是 JSON 对象/数组")


# ---------------------------------------------------------------------------
# 素材选择（pose_ids / costume_ids）
# ---------------------------------------------------------------------------


def parse_id_selection(value: Any, field: str = "ids") -> list[dict]:
    """解析 ``[{"id","source"}]`` 形式的选中清单（也接受纯字符串数组/JSON 文本）。"""
    parsed = _parse_json_value(value, field)
    if parsed is None:
        return []
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise PlanError("SELECTION_INVALID", f"{field} 必须是数组")
    result: list[dict] = []
    seen: set[str] = set()
    for raw in parsed:
        if isinstance(raw, str):
            item_id, source = raw, "builtin"
        elif isinstance(raw, dict):
            item_id = raw.get("id")
            source = raw.get("source", "builtin")
        else:
            raise PlanError("SELECTION_INVALID", f"{field} 中存在非法条目：{raw!r}")
        if not isinstance(item_id, str) or not item_id.strip():
            raise PlanError("SELECTION_INVALID", f"{field} 中存在空素材 ID")
        item_id = item_id.strip()
        if item_id in seen:
            continue
        seen.add(item_id)
        result.append({"id": item_id, "source": normalize_source(source)})
    if len(result) > MAX_IDS:
        raise PlanError("SELECTION_TOO_LARGE", f"{field} 最多 {MAX_IDS} 项，收到 {len(result)}")
    return result


def _id_value(entry: Any) -> Any:
    """从 JSON 的 pose/costume 条目中取 id（兼容字符串与对象）。"""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("id")
    return None


def _source_value(entry: Any) -> Any:
    if isinstance(entry, dict):
        return entry.get("source", "builtin")
    return "builtin"


def _normalize_pair_entry(raw: Any, index: int) -> dict:
    """把计划 JSON 里的一条 pair 规范成 ``{pose,costume}``（不带 order/pair_id）。"""
    if not isinstance(raw, dict):
        raise PlanError("PAIR_INVALID", f"pairs[{index}] 必须是对象")
    pose_raw = raw.get("pose")
    costume_raw = raw.get("costume")
    if pose_raw is None:
        pose_raw = raw.get("pose_id")
    if costume_raw is None:
        costume_raw = raw.get("costume_id")
    pose_id = _id_value(pose_raw)
    costume_id = _id_value(costume_raw)
    if not isinstance(pose_id, str) or not pose_id.strip():
        raise PlanError("PAIR_INVALID", f"pairs[{index}] 缺少有效姿势 ID")
    if not isinstance(costume_id, str) or not costume_id.strip():
        raise PlanError("PAIR_INVALID", f"pairs[{index}] 缺少有效服装 ID")
    return {
        "pose": {"id": pose_id.strip(), "source": normalize_source(_source_value(pose_raw))},
        "costume": {"id": costume_id.strip(), "source": normalize_source(_source_value(costume_raw))},
    }


def make_pair(pose: Mapping[str, Any], costume: Mapping[str, Any], order: int) -> dict:
    pose_id = pose["id"]
    costume_id = costume["id"]
    return {
        "pair_id": pair_id_for(pose_id, costume_id),
        "order": int(order),
        "pose": {"id": pose_id, "source": normalize_source(pose.get("source", "builtin"))},
        "costume": {"id": costume_id, "source": normalize_source(costume.get("source", "builtin"))},
    }


def _dedupe_pairs(pairs: Iterable[dict], warnings: list[str]) -> list[dict]:
    """按 ``(pose.id, costume.id)`` 去重保序，并重排 order。"""
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for pair in pairs:
        key = (pair["pose"]["id"], pair["costume"]["id"])
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        result.append(
            {
                "pair_id": pair.get("pair_id") or pair_id_for(*key),
                "order": len(result),
                "pose": dict(pair["pose"]),
                "costume": dict(pair["costume"]),
            }
        )
    if dropped:
        warnings.append(f"已去除 {dropped} 个重复的姿势×服装组合")
    return result


def _manual_index_entries(manual_pairs: Any) -> list[tuple[int, int]]:
    if manual_pairs is None:
        raise PlanError("MANUAL_REQUIRED", "manual 模式必须提供 manual_pairs（[[i,j]] 或 [{i,j}]）")
    parsed = _parse_json_value(manual_pairs, "manual_pairs")
    if not isinstance(parsed, list):
        raise PlanError("MANUAL_INVALID", "manual_pairs 必须是数组")
    result: list[tuple[int, int]] = []
    for index, entry in enumerate(parsed):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            i, j = entry
        elif isinstance(entry, dict):
            i = entry.get("i", entry.get("pose_index"))
            j = entry.get("j", entry.get("costume_index"))
        else:
            raise PlanError("MANUAL_INVALID", f"manual_pairs[{index}] 必须是 [i,j] 或 {{i,j}}")
        if isinstance(i, bool) or isinstance(j, bool) or not isinstance(i, int) or not isinstance(j, int):
            raise PlanError("MANUAL_INVALID", f"manual_pairs[{index}] 的下标必须是整数")
        result.append((i, j))
    return result


def build_pairs(
    pose_ids: Any,
    costume_ids: Any,
    mode: str = "cartesian",
    manual_pairs: Any = None,
    *,
    warnings: list[str] | None = None,
) -> list[dict]:
    """构造 pair 列表（契约 §3）。

    * ``cartesian``：姿势 × 服装；
    * ``zip``：短边对角，长度不一致时产生 warning；
    * ``manual``：``manual_pairs`` 提供 ``[[i,j]]`` 或 ``[{"i","j"}]``（0 基）。
    """
    warn = warnings if warnings is not None else []
    if mode not in PAIR_MODES:
        raise PlanError("PAIR_MODE_INVALID", f"未知组合方式：{mode}（仅支持 {' / '.join(PAIR_MODES)}）")
    poses = parse_id_selection(pose_ids, "pose_ids_json")
    costumes = parse_id_selection(costume_ids, "costume_ids_json")
    raw_pairs: list[dict] = []
    if mode == "cartesian":
        for pose in poses:
            for costume in costumes:
                raw_pairs.append(make_pair(pose, costume, len(raw_pairs)))
    elif mode == "zip":
        count = min(len(poses), len(costumes))
        if poses and costumes and len(poses) != len(costumes):
            warn.append(
                f"zip 模式长度不一致：{len(poses)} 个姿势 × {len(costumes)} 套服装，"
                f"仅生成前 {count} 对"
            )
        for index in range(count):
            raw_pairs.append(make_pair(poses[index], costumes[index], index))
    else:  # manual
        if not poses or not costumes:
            raise PlanError("NO_PAIRS", "manual 模式需要先选择姿势和服装")
        for i, j in _manual_index_entries(manual_pairs):
            if not (0 <= i < len(poses)):
                raise PlanError("MANUAL_INVALID", f"manual_pairs 姿势下标越界：{i}（共 {len(poses)} 项）")
            if not (0 <= j < len(costumes)):
                raise PlanError("MANUAL_INVALID", f"manual_pairs 服装下标越界：{j}（共 {len(costumes)} 项）")
            raw_pairs.append(make_pair(poses[i], costumes[j], len(raw_pairs)))
    pairs = _dedupe_pairs(raw_pairs, warn)
    if not pairs:
        raise PlanError("NO_PAIRS", "至少选择一张姿势图和一套服装")
    return pairs


# ---------------------------------------------------------------------------
# 模型 / 计划 JSON
# ---------------------------------------------------------------------------


def resolve_models(
    models: Any = None,
    *,
    model_qwen: bool | None = None,
    model_flux: bool | None = None,
) -> list[str]:
    """解析模型顺序；``models`` 优先，其次节点上的两个 BOOLEAN 开关。"""
    result: list[str] = []
    source = models
    if isinstance(source, str):
        source = [source]
    if source:
        if not isinstance(source, list):
            raise PlanError("MODEL_INVALID", "models 必须是数组")
        for value in source:
            if not isinstance(value, str) or value not in MODEL_KEYS:
                raise PlanError("MODEL_INVALID", f"未知模型：{value!r}（仅支持 qwen21 / flux3klein）")
            if value not in result:
                result.append(value)
    else:
        qwen = True if model_qwen is None else bool(model_qwen)
        flux = True if model_flux is None else bool(model_flux)
        for key in MODEL_KEYS:
            if (key == "qwen21" and qwen) or (key == "flux3klein" and flux):
                result.append(key)
    if not result:
        raise PlanError("NO_MODEL", "请至少选择一个模型（Qwen-Image 2.1 或 FLUX.3 Klein）")
    return result


def _normalize_external_templates(value: Any) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise PlanError("TEMPLATE_INVALID", f"external_templates 不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise PlanError("TEMPLATE_INVALID", "external_templates 必须是对象")
    result: dict = {}
    for key, template in value.items():
        if not isinstance(key, str) or not key:
            raise PlanError("TEMPLATE_INVALID", "external_templates 的键必须是模型名或 dual")
        if not isinstance(template, dict):
            raise PlanError("TEMPLATE_INVALID", f"external_templates.{key} 必须是对象")
        prompt = template.get("prompt")
        if not isinstance(prompt, dict) or not prompt:
            raise PlanError("TEMPLATE_INVALID", f"external_templates.{key}.prompt 必须是非空 API 工作流对象")
        result[key] = {"node_id": str(template["node_id"]) if template.get("node_id") is not None else None,
                       "prompt": prompt}
    return result


def build_plan(
    source: Any = None,
    *,
    pose_ids: Any = None,
    costume_ids: Any = None,
    pair_mode: str | None = None,
    manual_pairs: Any = None,
    models: Any = None,
    model_qwen: bool | None = None,
    model_flux: bool | None = None,
    samples_per_pair: Any = None,
    seed: Any = None,
    seed_policy: str | None = None,
    prompt_template: str | None = None,
    dual_compare: bool | None = None,
    graph_model: str | None = None,
    external_templates: Any = None,
    created_at: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """把任意输入（计划 JSON / 节点 widget 组合）规范成契约 §3 的 plan 对象。

    这是 planner 的入口函数：节点、``/plan``、``/submit`` 都走同一条路径，
    保证“预演看到的”和“真正执行的”完全一致。
    """
    warn = warnings if warnings is not None else []
    raw = _parse_json_value(source, "selection_json")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PlanError("PLAN_INVALID", "计划 JSON 必须是对象")

    mode = raw.get("pair_mode") or pair_mode or "cartesian"
    if not isinstance(mode, str) or mode not in PAIR_MODES:
        raise PlanError("PAIR_MODE_INVALID", f"未知组合方式：{mode!r}（仅支持 {' / '.join(PAIR_MODES)}）")

    raw_pairs = raw.get("pairs")
    if raw_pairs is not None and not isinstance(raw_pairs, list):
        raise PlanError("PAIR_INVALID", "plan.pairs 必须是数组")
    pairs: list[dict]
    if raw_pairs:
        normalized = [_normalize_pair_entry(entry, index) for index, entry in enumerate(raw_pairs)]
        pairs = _dedupe_pairs(normalized, warn)
        if len(pairs) > MAX_PAIRS:
            raise PlanError("TOO_MANY_PAIRS", f"组合数 {len(pairs)} 超过上限 {MAX_PAIRS}")
    else:
        ids_poses = raw.get("pose_ids") if raw.get("pose_ids") is not None else pose_ids
        ids_costumes = raw.get("costume_ids") if raw.get("costume_ids") is not None else costume_ids
        merged_manual = raw.get("manual_pairs") if raw.get("manual_pairs") is not None else manual_pairs
        pairs = build_pairs(ids_poses, ids_costumes, mode, merged_manual, warnings=warn)

    models_value = raw.get("models") if raw.get("models") else models
    model_list = resolve_models(models_value, model_qwen=model_qwen, model_flux=model_flux)

    samples_value = raw.get("samples_per_pair", samples_per_pair)
    if samples_value is None:
        samples_value = 1
    samples = _require_int(samples_value, "samples_per_pair", "SAMPLES_INVALID")
    if not 1 <= samples <= MAX_SAMPLES_PER_PAIR:
        raise PlanError("SAMPLES_INVALID", f"samples_per_pair 必须是 1~{MAX_SAMPLES_PER_PAIR} 的整数")

    seed_value = raw.get("seed", seed)
    if seed_value is None:
        seed_value = 9527
    seed_value = _require_int(seed_value, "seed", "SEED_INVALID")
    if not 0 <= seed_value <= MAX_SEED:
        raise PlanError("SEED_INVALID", f"seed 必须在 0~{MAX_SEED} 之间")

    policy = raw.get("seed_policy") or seed_policy or "fixed"
    if policy not in SEED_POLICIES:
        raise PlanError("SEED_POLICY_INVALID", f"未知 seed 策略：{policy!r}（仅支持 {' / '.join(SEED_POLICIES)}）")

    template_value = raw.get("prompt_template", prompt_template)
    if template_value is None:
        template_value = DEFAULT_PROMPT
    if not isinstance(template_value, str):
        raise PlanError("PROMPT_INVALID", "prompt_template 必须是字符串")
    template_value = template_value if template_value.strip() else DEFAULT_PROMPT
    if len(template_value) > MAX_PROMPT_LEN:
        raise PlanError("PROMPT_INVALID", f"prompt_template 超过 {MAX_PROMPT_LEN} 字符")

    dual = raw.get("dual_compare", dual_compare)
    dual = False if dual is None else bool(dual)

    graph = raw.get("graph_model", graph_model)
    if not graph:
        graph = "qwen21" if "qwen21" in model_list else model_list[0]
    if graph not in MODEL_KEYS:
        raise PlanError("MODEL_INVALID", f"graph_model 未知模型：{graph!r}")
    if not dual and graph not in model_list:
        warn.append(f"当前画布模型 {graph} 不在 models 中，节点开关将以 models 为准")
    if dual and len(model_list) != 2:
        warn.append(f"dual_compare 需要两个模型，当前 models={model_list}")

    templates_value = raw.get("external_templates") if raw.get("external_templates") is not None else external_templates
    external = _normalize_external_templates(templates_value)

    stamp = raw.get("created_at") or created_at or now_iso()
    if not isinstance(stamp, str):
        raise PlanError("PLAN_INVALID", "created_at 必须是字符串")

    plan = {
        "schema": SCHEMA_PLAN,
        "pair_mode": mode,
        "models": model_list,
        "samples_per_pair": samples,
        "seed": seed_value,
        "seed_policy": policy,
        "prompt_template": template_value,
        "dual_compare": dual,
        "graph_model": graph,
        "external_templates": external,
        "pairs": pairs,
        "pair_count": len(pairs),
        "created_at": stamp,
    }
    # compact 单任务清单（runner 写回 selection_json）会带上原始任务集的 pair_index/pair_total，
    # 让节点输出的 pair_index/pair_total 与整批任务一致（契约 §6）。
    raw_pair_total = raw.get("pair_total")
    if raw_pair_total is not None:
        try:
            plan["pair_total"] = max(len(pairs), int(raw_pair_total))
        except (TypeError, ValueError) as exc:
            raise PlanError("PLAN_INVALID", f"pair_total 必须是整数：{raw_pair_total!r}") from exc
    raw_pair_index = raw.get("pair_index")
    if raw_pair_index is not None:
        try:
            plan["pair_index"] = max(0, int(raw_pair_index))
        except (TypeError, ValueError) as exc:
            raise PlanError("PLAN_INVALID", f"pair_index 必须是整数：{raw_pair_index!r}") from exc
    counts = plan_counts(plan)
    if counts["tasks"] > MAX_TASKS:
        raise PlanError(
            "TOO_MANY_TASKS",
            f"本次计划共 {counts['tasks']} 个任务，超过上限 {MAX_TASKS}，请拆分后再提交",
            counts,
        )
    return plan


def validate_plan(plan: Any) -> dict:
    """校验并规范化一份完整计划（不修改入参）。"""
    return build_plan(plan, created_at=(plan or {}).get("created_at") if isinstance(plan, dict) else None)


# ---------------------------------------------------------------------------
# 数量 / 警告 / 任务展开
# ---------------------------------------------------------------------------


def plan_counts(plan: Mapping[str, Any]) -> dict:
    """契约 §3 / §6：``{pairs, models, samples, tasks, images}``。"""
    pairs = len(plan.get("pairs") or [])
    model_list = list(plan.get("models") or [])
    samples = int(plan.get("samples_per_pair") or 1)
    dual = bool(plan.get("dual_compare"))
    if dual:
        tasks = pairs * samples
        images = tasks * len(model_list)
    else:
        tasks = pairs * len(model_list) * samples
        images = tasks
    return {
        "pairs": pairs,
        "models": len(model_list),
        "samples": samples,
        "tasks": tasks,
        "images": images,
    }


def plan_warnings(plan: Mapping[str, Any]) -> list[str]:
    """根据计划内容生成中文提示（不阻断执行）。"""
    counts = plan_counts(plan)
    warnings: list[str] = []
    if counts["tasks"] > WARN_OVER_TASKS:
        warnings.append(
            f"预计生成 {counts['images']} 张图（{counts['tasks']} 个任务），超过 {WARN_OVER_TASKS}，"
            "提交时需要二次确认（confirm_large=true）"
        )
    if plan.get("dual_compare"):
        warnings.append("双模型对比模式：每个任务同时跑两条分支，单任务产出 2 张图")
    return warnings


def validate_limits(counts: Mapping[str, Any]) -> None:
    """硬上限校验：任务数/组合数超限抛错（契约 §3）。"""
    if int(counts.get("pairs") or 0) > MAX_PAIRS:
        raise PlanError("TOO_MANY_PAIRS", f"组合数超过上限 {MAX_PAIRS}")
    if int(counts.get("tasks") or 0) > MAX_TASKS:
        raise PlanError("TOO_MANY_TASKS", f"任务数超过上限 {MAX_TASKS}")


def resolve_seed(
    base_seed: int,
    policy: str,
    order_index: int,
    *,
    job_id: str = "local",
    pair_id: str = "",
    model: str = "",
    sample_index: int = 0,
) -> int:
    """契约 §3 的三种 seed 策略。

    ``fixed`` 现在也按 sample_index 递增（base + sample），保证“每组 N 张”是
    N 张不同 seed 的独立任务；仍完全可复现（审计/队列拆分要求）。
    """
    if policy == "fixed":
        return (int(base_seed) + int(sample_index)) % (2**32)
    if policy == "increment":
        return (int(base_seed) + int(order_index)) % (2**32)
    if policy == "random_recorded":
        material = f"aiksk.pc.seed.v1|{job_id}|{pair_id}|{model}|{sample_index}|{order_index}|{base_seed}"
        digest = hashlib.sha256(material.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % (2**32)
    raise PlanError("SEED_POLICY_INVALID", f"未知 seed 策略：{policy!r}")


def task_key(job_id: str, pair_id: str, model: str, sample_index: int) -> str:
    """契约 §3：``pc1:`` + sha256(job_id|pair_id|model|sample)[:24]。"""
    material = f"{job_id}|{pair_id}|{model}|{int(sample_index)}"
    return "pc1:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def filename_prefix(pair_id: str, seed: int, *, job_id: str = "", model: str = "",
                    sample_index: int = 0) -> str:
    """产物前缀（契约 §2.3 扩展 / 审计 P0-3）。

    批量任务必须全局唯一，避免固定 Seed 的多样本、跨任务集互相误认：
    ``AIKSK_PoseCostume/{job_short}/{pair_id}_{model}_s{seed}_i{sample}``；
    节点手动单跑（无 job_id/model）保持旧格式 ``.../{pair_id}_s{seed}``。
    """
    head = "AIKSK_PoseCostume/"
    job_value = str(job_id or "").strip()
    if job_value and job_value != "local":
        head += hashlib.sha1(job_value.encode("utf-8")).hexdigest()[:8] + "/"
    tail = f"_s{int(seed)}"
    model_value = _INVALID_SEGMENT_RE.sub("_", str(model or "")).strip("._-")[:24]
    sample_value = int(sample_index or 0)
    if model_value:
        tail = f"_{model_value}{tail}_i{sample_value}"
    body = _INVALID_SEGMENT_RE.sub("_", str(pair_id)).strip("._-") or "pair"
    room = MAX_FILENAME_PREFIX - len(head) - len(tail)
    if room < 1:
        return (head + tail)[:MAX_FILENAME_PREFIX]
    if len(body) > room:
        body = body[:room].rstrip("._-") or "pair"
    result = head + body + tail
    if len(result) > MAX_FILENAME_PREFIX:
        result = result[:MAX_FILENAME_PREFIX]
    return result


def is_safe_filename_prefix(value: str) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_FILENAME_PREFIX
        and _SAFE_PREFIX_RE.match(value) is not None
    )


def expand_tasks(plan: Mapping[str, Any], job_id: str = "local") -> list[dict]:
    """展开任务列表（契约 §3）：

    * 非对比：``pair → model(顺序) → sample``；
    * 对比：``pair → sample``，一个任务同时跑两条分支（2 张图）。
    """
    pairs = list(plan.get("pairs") or [])
    if not pairs:
        raise PlanError("NO_PAIRS", "计划中没有任何姿势×服装组合")
    model_list = list(plan.get("models") or [])
    if not model_list:
        raise PlanError("NO_MODEL", "计划中没有任何模型")
    samples = int(plan.get("samples_per_pair") or 1)
    dual = bool(plan.get("dual_compare"))
    base_seed = int(plan.get("seed") or 0)
    policy = str(plan.get("seed_policy") or "fixed")
    counts = plan_counts(plan)
    validate_limits(counts)

    tasks: list[dict] = []
    pair_total = int(plan.get("pair_total") or len(pairs))
    # compact 单任务清单用顶层 pair_index 保留原始下标（pairs 里只有一条、order=0）。
    pair_index_override = plan.get("pair_index") if len(pairs) == 1 and plan.get("pair_index") is not None else None
    for pair in pairs:
        pair_id = pair.get("pair_id") or pair_id_for(pair["pose"]["id"], pair["costume"]["id"])
        models_for_pair = ["dual"] if dual else model_list
        for model in models_for_pair:
            for sample_index in range(samples):
                order_index = len(tasks)
                seed = resolve_seed(
                    base_seed,
                    policy,
                    order_index,
                    job_id=job_id,
                    pair_id=pair_id,
                    model=model,
                    sample_index=sample_index,
                )
                tasks.append(
                    {
                        "task_key": task_key(job_id, pair_id, model, sample_index),
                        "index": order_index,
                        "pair_id": pair_id,
                        "pair_index": int(pair_index_override if pair_index_override is not None else pair.get("order", 0)),
                        "pair_total": pair_total,
                        "pose": dict(pair["pose"]),
                        "costume": dict(pair["costume"]),
                        "pose_id": pair["pose"]["id"],
                        "costume_id": pair["costume"]["id"],
                        "model": model,
                        "sample_index": sample_index,
                        "seed": seed,
                        "seed_policy": policy,
                        "workflow_name": "dual_compare" if dual else model,
                        "status": "pending",
                        "filename_prefix": filename_prefix(pair_id, seed, job_id=job_id, model=model, sample_index=sample_index),
                        "output_prefix": filename_prefix(pair_id, seed, job_id=job_id, model=model, sample_index=sample_index),
                        "dual_compare": dual,
                        "images": len(model_list) if dual else 1,
                    }
                )
    if len(tasks) != counts["tasks"]:  # pragma: no cover - 内部一致性保护
        raise PlanError(
            "PLAN_INVALID",
            f"任务展开数量不一致：展开 {len(tasks)}，计划 {counts['tasks']}",
        )
    return tasks


def tasks_preview(plan: Mapping[str, Any], limit: int = 20, offset: int = 0, job_id: str = "local") -> list[dict]:
    """给 ``/plan`` 的轻量任务预览。"""
    limit = max(0, min(int(limit), 200))
    offset = max(0, int(offset))
    tasks = expand_tasks(plan, job_id=job_id)
    sliced = tasks[offset : offset + limit]
    return [
        {
            key: task[key]
            for key in (
                "index",
                "task_key",
                "pair_id",
                "pair_index",
                "pair_total",
                "pose_id",
                "costume_id",
                "model",
                "sample_index",
                "seed",
                "seed_policy",
                "workflow_name",
                "status",
                "filename_prefix",
                "output_prefix",
                "images",
            )
        }
        for task in sliced
    ]


def compact_plan(plan: Mapping[str, Any], task: Mapping[str, Any]) -> dict:
    """把任务集计划压缩成“单任务清单”，写回节点 ``selection_json``（契约 §6）。"""
    pair = None
    for candidate in plan.get("pairs") or []:
        if candidate.get("pair_id") == task.get("pair_id") or (
            candidate["pose"]["id"] == task["pose_id"] and candidate["costume"]["id"] == task["costume_id"]
        ):
            pair = candidate
            break
    if pair is None:
        pair = {
            "pair_id": task["pair_id"],
            "order": 0,
            "pose": dict(task["pose"]),
            "costume": dict(task["costume"]),
        }
    if task.get("dual_compare"):
        models_scope = list(plan.get("models") or [])
    else:
        models_scope = [task["model"]]
    return {
        "schema": SCHEMA_PLAN,
        "pair_mode": "manual",
        "models": models_scope,
        "samples_per_pair": 1,
        "seed": int(task["seed"]),
        "seed_policy": "fixed",
        "prompt_template": plan.get("prompt_template") or DEFAULT_PROMPT,
        "dual_compare": bool(task.get("dual_compare")),
        "graph_model": plan.get("graph_model") or (models_scope[0] if models_scope else "qwen21"),
        "external_templates": {},
        "pairs": [
            {
                "pair_id": pair["pair_id"],
                "order": 0,
                "pose": dict(pair["pose"]),
                "costume": dict(pair["costume"]),
            }
        ],
        "pair_count": 1,
        "pair_index": int(task.get("pair_index", 0)),
        "pair_total": int(task.get("pair_total", 1)),
        "created_at": plan.get("created_at") or now_iso(),
    }


# ---------------------------------------------------------------------------
# 提示词渲染
# ---------------------------------------------------------------------------


def pose_prompt_text(item: Mapping[str, Any] | None) -> str:
    """``{pose}`` 渲染值：``姿势名称 / 角度``（与节点 run 的渲染完全一致）。"""
    if not isinstance(item, Mapping):
        return ""
    name = item.get("name") or item.get("姿势名称")
    angle = item.get("angle") or item.get("角度")
    name_text = str(name).strip() if name else ""
    angle_text = str(angle).strip() if angle else ""
    if name_text:
        return f"{name_text} / {angle_text}" if angle_text else name_text
    label = item.get("label") or item.get("id")
    label_text = str(label).strip() if label else ""
    if label_text and angle_text and angle_text not in label_text:
        return f"{label_text} / {angle_text}"
    return label_text


def costume_prompt_text(item: Mapping[str, Any] | None) -> str:
    """``{costume}`` 渲染值：优先英文关键词，其次中文关键词。"""
    if not isinstance(item, Mapping):
        return ""
    for key in ("keywords_en", "关键词", "keywords_zh", "关键词中文", "label", "id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tags = item.get("tags") or item.get("标签")
    if isinstance(tags, (list, tuple)) and tags:
        return " ".join(str(tag) for tag in tags)
    return ""


def render_prompt(
    template: str | None,
    pose_item: Mapping[str, Any] | None = None,
    costume_item: Mapping[str, Any] | None = None,
) -> str:
    """把 ``{pose}`` / ``{costume}`` 占位替换成真实素材描述。"""
    text = template if isinstance(template, str) and template.strip() else DEFAULT_PROMPT
    return (
        text.replace("{pose}", pose_prompt_text(pose_item))
        .replace("{costume}", costume_prompt_text(costume_item))
    )


# ---------------------------------------------------------------------------
# 模板（API 工作流）定位与替换
# ---------------------------------------------------------------------------


def normalize_templates(templates: Any) -> dict:
    """规范化 ``{model_key: {node_id, prompt}}`` 模板集合。"""
    if templates is None or templates == "":
        return {}
    if isinstance(templates, str):
        try:
            templates = json.loads(templates)
        except ValueError as exc:
            raise PlanError("TEMPLATE_INVALID", f"templates 不是合法 JSON：{exc}") from exc
    if not isinstance(templates, dict):
        raise PlanError("TEMPLATE_INVALID", "templates 必须是对象")
    result: dict = {}
    for key, value in templates.items():
        if not isinstance(key, str) or not key:
            raise PlanError("TEMPLATE_INVALID", "templates 的键必须是模型名或 dual")
        if not isinstance(value, dict):
            raise PlanError("TEMPLATE_INVALID", f"templates.{key} 必须是对象")
        prompt = value.get("prompt")
        if not isinstance(prompt, dict) or not prompt:
            raise PlanError("TEMPLATE_INVALID", f"templates.{key}.prompt 必须是非空 API 工作流对象")
        node_id = value.get("node_id")
        result[key] = {"node_id": str(node_id) if node_id is not None else None, "prompt": prompt}
    return result


def template_required_keys(plan: Mapping[str, Any]) -> list[str]:
    """本次计划需要哪些模板键。"""
    if plan.get("dual_compare"):
        return ["dual"]
    return list(plan.get("models") or [])


def _prompt_contains_class(prompt: Mapping[str, Any], class_name: str) -> list[str]:
    found = []
    for node_id, node in prompt.items():
        if isinstance(node, dict) and node.get("class_type") == class_name:
            found.append(str(node_id))
    return found


def _upstream_ids(prompt: Mapping[str, Any], roots) -> set[str]:
    seen: set[str] = set()
    stack = [str(item) for item in roots]
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in prompt:
            continue
        seen.add(node_id)
        node = prompt.get(node_id)
        if not isinstance(node, Mapping):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        for value in inputs.values():
            if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], str):
                if value[0] in prompt:
                    stack.append(value[0])
    return seen


def validate_workflow_template(
    prompt: Any,
    key: str,
    *,
    dual: bool = False,
    warnings: list[str] | None = None,
    allow_reference_only: bool = False,
) -> dict:
    """校验 API 工作流模板（审计 P0-2）：必须含本节点 + 最终保存节点，且保存节点上游经过生成分支。"""
    warn = warnings if warnings is not None else []
    if not isinstance(prompt, dict) or not prompt:
        raise PlanError("TEMPLATE_INVALID", f"模板 {key} 必须是非空 API 工作流对象")
    nodes = [str(node_id) for node_id in _prompt_contains_class(prompt, TEMPLATE_NODE_CLASS)]
    if not nodes:
        raise PlanError("TEMPLATE_INVALID", f"模板 {key} 中未找到 {TEMPLATE_NODE_CLASS} 节点")
    if len(nodes) > 1:
        raise PlanError(
            "TEMPLATE_AMBIGUOUS",
            f"模板 {key} 含有 {len(nodes)} 个本节点，必须显式指定目标节点",
            {"node_ids": nodes},
        )
    save_nodes = []
    for save_class in SAVE_NODE_CLASSES:
        save_nodes.extend(_prompt_contains_class(prompt, save_class))
    if not save_nodes:
        raise PlanError("TEMPLATE_INVALID", f"模板 {key} 中未找到 SaveImage/SaveImageAdvanced 节点")
    if dual and len(save_nodes) < 2:
        warn.append(f"双模型对比模板 {key} 只有 {len(save_nodes)} 个保存节点，预期每个模型分支各一个")
    for node_id, node in prompt.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise PlanError("TEMPLATE_INVALID", f"模板 {key} 的节点 {node_id} 缺少 inputs 对象")
    ancestors = _upstream_ids(prompt, save_nodes) | set(save_nodes)
    generation_nodes = sorted(
        node_id for node_id in ancestors
        if str(prompt[node_id].get("class_type")) in GENERATION_NODE_CLASSES
    )
    if not generation_nodes and not allow_reference_only:
        raise PlanError(
            "TEMPLATE_NO_GENERATION",
            f"模板 {key} 的保存节点上游没有任何模型生成节点（疑似只保存参考图），生产批量提交已拒绝",
            {"save_node_ids": save_nodes},
        )
    return {
        "node_ids": nodes,
        "save_node_ids": save_nodes,
        "generation_node_ids": generation_nodes,
        "expected_image_count": len(save_nodes),
        "reference_only": not bool(generation_nodes),
    }


def resolve_templates(
    plan: Mapping[str, Any],
    templates: Any = None,
    *,
    warnings: list[str] | None = None,
) -> tuple[dict, list[str]]:
    """合并 ``plan.external_templates`` 与本次传入的 templates，并校验必需模型。

    返回 ``(templates_normalized, warnings)``；缺失/非法抛 ``TEMPLATE_MISSING`` /
    ``TEMPLATE_INVALID``（契约 §6）。
    """
    warn = warnings if warnings is not None else []
    merged = normalize_templates(plan.get("external_templates") or {})
    merged.update(normalize_templates(templates))
    required = template_required_keys(plan)
    missing = [key for key in required if key not in merged]
    if missing:
        raise PlanError(
            "TEMPLATE_MISSING",
            f"缺少模板：{', '.join(missing)}（请在对应模型上绑定 API 工作流）",
            {"missing": missing, "required": required},
        )
    dual = bool(plan.get("dual_compare"))
    for key in required:
        info = validate_workflow_template(
            merged[key]["prompt"], key, dual=dual and key == "dual", warnings=warn,
        )
        merged[key]["final_save_node_ids"] = info["save_node_ids"]
        merged[key]["generation_node_ids"] = info["generation_node_ids"]
        merged[key]["expected_image_count"] = info["expected_image_count"]
        merged[key]["reference_only"] = info["reference_only"]
    return merged, warn


def _negative_prompt_node_ids(prompt: Mapping[str, Any]) -> set[str]:
    """找出“只被负向输入引用”的节点（避免把正向提示词写进负向编码器）。

    注意：``TextEncodeQwenImage21`` 这类节点同时输出正向/负向 conditioning
    （KSampler 的 positive=[486,0] / negative=[486,1]），只要它也被正向引用，
    就必须写入正向提示词。
    """
    positive_refs: set[str] = set()
    negative_refs: set[str] = set()
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for input_key, value in inputs.items():
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                continue
            if not isinstance(value[0], (str, int)) or isinstance(value[0], bool):
                continue
            target = str(value[0])
            lowered = str(input_key).lower()
            if "negative" in lowered:
                negative_refs.add(target)
            elif "positive" in lowered:
                positive_refs.add(target)
    return negative_refs - positive_refs


_ENCODE_CLASS_HINTS = ("TextEncode", "CLIPTextEncode")
_ENCODE_PROMPT_KEYS = ("prompt", "text")


def _text_encode_targets(prompt: Mapping[str, Any], skip: set[str]) -> list[tuple[str, str]]:
    """需要写入最终提示词的文本编码节点（契约 §6 扩展）。

    规则：
    * ``TextEncodeQwenImage21`` 的 ``prompt``（正向提示词）——总是更新；
    * ``CLIPTextEncode`` 的 ``text``——只更新正向节点，空文本 / 仅负向引用的节点跳过；
    * 其他 TextEncode 类节点按 ``prompt``/``text`` 兼容处理；
    * 不触碰 ``negative_prompt`` 等负向字段。
    """
    negative_only = _negative_prompt_node_ids(prompt)
    targets: list[tuple[str, str]] = []
    for node_id, node in prompt.items():
        node_id = str(node_id)
        if node_id in skip or node_id in negative_only or not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        if not any(hint in class_type for hint in _ENCODE_CLASS_HINTS):
            continue
        if "negative" in class_type.lower():
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if "TextEncodeQwenImage21" in class_type:
            if isinstance(inputs.get("prompt"), str):
                targets.append((node_id, "prompt"))
            continue
        for input_key in _ENCODE_PROMPT_KEYS:
            value = inputs.get(input_key)
            if not isinstance(value, str):
                continue
            # 空文本编码器（通常是占位/负向）不写入，避免污染模板。
            if "CLIPTextEncode" in class_type and not value.strip():
                continue
            targets.append((node_id, input_key))
            break
    return targets


def _upstream_model_hint(prompt: Mapping[str, Any], start_id: str) -> str | None:
    """从某个 SaveImage 节点向上游 BFS，猜它属于哪个模型分支（dual_compare 用）。"""
    seen: set[str] = set()
    queue: list[str] = [str(start_id)]
    hint: str | None = None
    while queue:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        node = prompt.get(node_id)
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            for input_key, value in sorted(inputs.items()):
                key_text = str(input_key).lower()
                if isinstance(value, str):
                    if any(token in key_text for token in ("unet", "clip", "vae", "model", "lora", "ckpt")):
                        lowered = value.lower()
                        if "flux" in lowered and hint is None:
                            hint = "flux3klein"
                        elif "qwen" in lowered and hint is None:
                            hint = "qwen21"
                elif isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], (str, int)):
                    queue.append(str(value[0]))
    return hint


def template_tokens(task: Mapping[str, Any], model_override: str | None = None) -> dict[str, str]:
    """契约 §7 的 ``${AIKSK_*}`` token 取值。"""
    return {
        "AIKSK_PAIR_ID": str(task["pair_id"]),
        "AIKSK_POSE_ID": str(task["pose_id"]),
        "AIKSK_COSTUME_ID": str(task["costume_id"]),
        "AIKSK_MODEL": str(model_override if model_override is not None else task["model"]),
        "AIKSK_SEED": str(int(task["seed"])),
        "AIKSK_INDEX": str(int(task["index"])),
    }


def replace_tokens(text: str, tokens: Mapping[str, str]) -> str:
    """替换 ``${AIKSK_*}`` token；未知 token 原样保留。"""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(tokens.get(key, match.group(0)))

    return _TOKEN_RE.sub(_replace, text)


def template_external_prompt(template_prompt: Mapping[str, Any], node_id: str | None = None) -> str:
    """读取模板里本节点 ``external_prompt`` 的非空值（用户手填提示词优先）。"""
    if not isinstance(template_prompt, Mapping) or not template_prompt:
        return ""
    candidates = _prompt_contains_class(template_prompt, TEMPLATE_NODE_CLASS)
    if not candidates:
        return ""
    chosen = str(node_id) if node_id is not None and str(node_id) in candidates else candidates[0]
    node = template_prompt.get(chosen)
    inputs = node.get("inputs") if isinstance(node, dict) else None
    value = inputs.get("external_prompt") if isinstance(inputs, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else ""


def apply_task_to_prompt(
    template_prompt: Mapping[str, Any],
    task: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    job_id: str = "local",
    node_id: str | None = None,
    rendered_prompt: str | None = None,
    warnings: list[str] | None = None,
) -> tuple[dict, dict]:
    """深拷贝模板 → 写入单任务参数 → 返回 ``(prompt, info)``。

    info 包含真正定位到的本节点 ID、各 SaveImage 解析后的 filename_prefix，
    供 runner 做产物对账（``save_prefixes``）。
    """
    warn = warnings if warnings is not None else []
    if not isinstance(template_prompt, dict) or not template_prompt:
        raise PlanError("TEMPLATE_INVALID", "模板必须是非空 API 工作流对象")
    prompt = copy.deepcopy(template_prompt)
    candidates = _prompt_contains_class(prompt, TEMPLATE_NODE_CLASS)
    if not candidates:
        raise PlanError("TEMPLATE_INVALID", f"模板中未找到 {TEMPLATE_NODE_CLASS} 节点")
    chosen: str
    if node_id is not None and str(node_id) in candidates:
        chosen = str(node_id)
    else:
        chosen = candidates[0]
        if node_id is not None:
            warn.append(f"模板记录的本节点 ID={node_id} 与工作流不一致，已自动使用 {chosen}")
    if len(candidates) > 1:
        warn.append(f"模板含有 {len(candidates)} 个本节点，仅替换 {chosen}")

    node = prompt[chosen]
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise PlanError("TEMPLATE_INVALID", f"本节点 {chosen} 缺少 inputs 对象")

    models_scope = list(plan.get("models") or []) if task.get("dual_compare") else [task["model"]]
    final_prompt = rendered_prompt if rendered_prompt is not None else plan.get("prompt_template")
    if not isinstance(final_prompt, str) or not final_prompt.strip():
        final_prompt = DEFAULT_PROMPT

    new_inputs = {
        "selection_json": json.dumps(compact_plan(plan, task), ensure_ascii=False),
        "active_index": 0,
        "pose_ids_json": json.dumps([dict(task["pose"])], ensure_ascii=False),
        "costume_ids_json": json.dumps([dict(task["costume"])], ensure_ascii=False),
        "pair_mode": "manual",
        "model_qwen": "qwen21" in models_scope,
        "model_flux": "flux3klein" in models_scope,
        "samples_per_pair": 1,
        "seed": int(task["seed"]),
        "prompt_template": final_prompt,
    }
    # 清空用户手动覆盖，保证批量任务严格按任务参数执行。
    if "external_prompt" in inputs and isinstance(inputs["external_prompt"], str):
        new_inputs["external_prompt"] = ""
    if "seed_override" in inputs:
        new_inputs["seed_override"] = -1
    inputs.update(new_inputs)

    # 队列拆分要求：每条 prompt 只生成 1 张，禁止工作流内部 batch 一次出 N 张。
    for node_data in prompt.values():
        if not isinstance(node_data, dict):
            continue
        node_inputs = node_data.get("inputs")
        if not isinstance(node_inputs, dict):
            continue
        if "batch_size" in node_inputs:
            node_inputs["batch_size"] = 1

    # KSampler / KSamplerAdvanced / RandomNoise 的 seed 替换。
    for node_data in prompt.values():
        if not isinstance(node_data, dict):
            continue
        class_type = str(node_data.get("class_type") or "")
        seed_keys = SEED_NODE_KEYS.get(class_type)
        if not seed_keys:
            continue
        node_inputs = node_data.get("inputs")
        if not isinstance(node_inputs, dict):
            continue
        for seed_key in seed_keys:
            if isinstance(node_inputs.get(seed_key), int) and not isinstance(node_inputs.get(seed_key), bool):
                node_inputs[seed_key] = int(task["seed"])

    # 文本编码器的最终提示词（外部模板的 FLUX/Qwen 分支依赖这里）。
    skip = {chosen} | _negative_prompt_node_ids(prompt)
    for target_id, target_key in _text_encode_targets(prompt, skip):
        prompt[target_id]["inputs"][target_key] = final_prompt

    # SaveImage filename_prefix 的 ${AIKSK_*} token 替换。
    save_prefixes: dict[str, str] = {}
    base_tokens = template_tokens(task)
    explicit_prefix = str(task.get("output_prefix") or task.get("filename_prefix") or "")
    used_prefixes: set[str] = set()
    for save_class in SAVE_NODE_CLASSES:
        for save_id in _prompt_contains_class(prompt, save_class):
            node_inputs = prompt[save_id].get("inputs")
            if not isinstance(node_inputs, dict):
                continue
            prefix = node_inputs.get("filename_prefix")
            if not isinstance(prefix, str):
                continue
            branch_model = _upstream_model_hint(prompt, save_id) if task.get("dual_compare") else None
            if explicit_prefix:
                candidate = explicit_prefix
                if branch_model:
                    candidate += "_" + safe_segment(str(branch_model))
                if candidate in used_prefixes:
                    candidate += "_" + safe_segment(str(save_id))
                candidate = candidate.replace("${AIKSK_", "").replace("}", "")
            else:
                tokens = dict(base_tokens)
                if branch_model:
                    tokens["AIKSK_MODEL"] = branch_model
                candidate = replace_tokens(prefix, tokens)
            used_prefixes.add(candidate)
            node_inputs["filename_prefix"] = candidate
            save_prefixes[save_id] = candidate

    info = {
        "node_id": chosen,
        "prompt_id_suffix": str(task["task_key"]),
        "save_prefixes": save_prefixes,
        "tokens": base_tokens,
        "job_id": job_id,
    }
    return prompt, info


# ---------------------------------------------------------------------------
# 指纹 / 哈希
# ---------------------------------------------------------------------------


def plan_hash(plan: Mapping[str, Any], templates: Mapping[str, Any] | None = None) -> str:
    """计划 + 模板的稳定哈希（幂等 job_set / 跨任务集去重用）。"""
    payload = {"plan": plan, "templates": templates or {}}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "PlanError",
    "MAX_SEED",
    "MAX_FILENAME_PREFIX",
    "TEMPLATE_NODE_CLASS",
    "apply_task_to_prompt",
    "build_pairs",
    "build_plan",
    "canonical_json",
    "compact_plan",
    "costume_prompt_text",
    "expand_tasks",
    "filename_prefix",
    "is_safe_filename_prefix",
    "make_pair",
    "normalize_source",
    "normalize_templates",
    "now_iso",
    "pair_id_for",
    "parse_id_selection",
    "plan_counts",
    "plan_hash",
    "plan_warnings",
    "pose_prompt_text",
    "render_prompt",
    "replace_tokens",
    "resolve_models",
    "resolve_seed",
    "resolve_templates",
    "safe_segment",
    "task_key",
    "tasks_preview",
    "template_external_prompt",
    "template_required_keys",
    "template_tokens",
    "validate_limits",
    "validate_plan",
    "validate_workflow_template",
]
