"""AIKSK｜姿势 × 服装矩阵批量 —— ComfyUI 画布原生节点。

契约：``docs/CONTRACT_node_v1.md`` §2。
* 类名 ``KSK_PoseCostumeMatrixBatch``，显示名 / 分类 / 输入输出顺序严格按契约；
* ``run()`` 按 ``active_index`` 从 ``selection_json``（或 pose_ids/costume_ids+mode 推导）
  取当前任务，用 ``services/catalog.py`` 读取两张真实参考图并转成 IMAGE 张量；
* 所有错误统一为 ``ValueError("[AIKSK-PC:{CODE}] 中文信息")``；
* ``IS_CHANGED`` 返回稳定指纹，输入不变则不重复执行。
"""

from __future__ import annotations

import json
from typing import Any

try:  # ComfyUI 以包方式加载本节点
    from .services import catalog, planner
    from .services import config as config_module
    from .services import (
        DEFAULT_PROMPT,
        MAX_SAMPLES_PER_PAIR,
        MODEL_KEYS,
        PAIR_MODES,
        RESIZE_MODES,
        SCHEMA_TASK,
    )
except ImportError:  # pragma: no cover - 直接按文件导入时的兜底
    import os
    import sys

    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from services import catalog, planner  # type: ignore
    from services import config as config_module  # type: ignore
    from services import (  # type: ignore
        DEFAULT_PROMPT,
        MAX_SAMPLES_PER_PAIR,
        MODEL_KEYS,
        PAIR_MODES,
        RESIZE_MODES,
        SCHEMA_TASK,
    )

NODE_CLASS_NAME = "KSK_PoseCostumeMatrixBatch"
NODE_DISPLAY_NAME = "KSKNODES 姿势 × 服装矩阵批量"
NODE_CATEGORY = "KSKNODES / Pose Costume"
NODE_DESCRIPTION = (
    "在一个节点内完成「姿势 × 服装」矩阵批量：从内置图库按 ID 读取参考图，"
    "按计划展开任务，用 active_index 输出当前任务的姿势图 / 服装图 / 提示词 / 任务信息。"
)

MAX_SEED = 2**32 - 1
MIN_SIDE = 64
MAX_SIDE = 4096
PLACEHOLDER_DIRECTIVE = "未接人物参考"


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------

ERROR_CODES = {
    "BAD_JSON": "JSON 文本框不是合法 JSON",
    "PLAN_INVALID": "计划 JSON 结构不符合契约",
    "NO_PAIRS": "没有可执行的姿势×服装组合",
    "NO_MODEL": "两个模型都未勾选",
    "INDEX_INVALID": "active_index 非法",
    "INDEX_OUT_OF_RANGE": "active_index 超出任务数量",
    "PAIR_MODE_INVALID": "未知组合方式",
    "SAMPLES_INVALID": "每组样本数非法",
    "SEED_INVALID": "seed 非法",
    "SEED_POLICY_INVALID": "seed 策略非法",
    "PROMPT_INVALID": "提示词非法",
    "TOO_MANY_PAIRS": "组合数超过上限",
    "TOO_MANY_TASKS": "任务数超过上限",
    "GALLERY_MISSING": "图库缺失",
    "ASSET_NOT_FOUND": "图库中找不到素材 ID",
    "ASSET_INVALID": "素材路径非法或越界",
    "ASSET_MISSING": "素材文件缺失",
    "IMAGE_INVALID": "参考图无法读取",
    "IMAGE_TOO_LARGE": "参考图像素超过 40MP 上限",
    "BAD_SIZE": "输出宽高非法",
    "BAD_RESIZE_MODE": "未知缩放模式",
    "PERSON_INVALID": "人物参考图格式非法",
    "TORCH_MISSING": "缺少 torch 环境",
    "PIL_MISSING": "缺少 Pillow 环境",
    "INTERNAL": "节点内部错误",
}


def _fail(code: str, message: str) -> None:
    raise ValueError(f"[AIKSK-PC:{code}] {message}")


def _raise_known(exc: BaseException) -> None:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        _fail(code, str(exc))
    _fail("INTERNAL", f"{type(exc).__name__}: {exc}")


def _import_torch():
    try:
        import torch  # type: ignore

        return torch
    except Exception:  # pragma: no cover - ComfyUI 环境必有 torch
        _fail("TORCH_MISSING", "当前环境缺少 torch，无法生成 IMAGE 张量")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _stable_json_text(value: Any) -> str:
    """把 STRING widget 里的 JSON 规范成稳定文本（用于 IS_CHANGED 指纹）。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            return planner.canonical_json(json.loads(text))
        except ValueError:
            return value
    return planner.canonical_json(value)


def _index_signature(kind: str) -> list:
    """图库索引文件的 (mtime_ns, size)，图库更新后让 IS_CHANGED 变化。"""
    try:
        index = catalog.catalogue(kind)
        return [int(index.signature[0]), int(index.signature[1])]
    except Exception:  # 图库缺失不应让 IS_CHANGED 抛错
        return []


def _person_placeholder():
    torch = _import_torch()
    return torch.zeros((1, 1, 1, 3), dtype=torch.float32)


def _validate_active_index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("INDEX_INVALID", f"active_index 必须是整数，收到：{value!r}")
    if value < 0:
        _fail("INDEX_INVALID", f"active_index 不能为负数：{value}")
    return int(value)


def _validate_dims(width: Any, height: Any) -> tuple[int, int]:
    result = []
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int):
            _fail("BAD_SIZE", f"{name} 必须是整数，收到：{value!r}")
        if not MIN_SIDE <= value <= MAX_SIDE:
            _fail("BAD_SIZE", f"{name} 必须在 {MIN_SIDE}~{MAX_SIDE} 之间，收到：{value}")
        result.append(int(value))
    return result[0], result[1]


def _validate_samples(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SAMPLES_PER_PAIR:
        _fail("SAMPLES_INVALID", f"samples_per_pair 必须是 1~{MAX_SAMPLES_PER_PAIR} 的整数，收到：{value!r}")
    return int(value)


def _validate_seed(value: Any, field: str = "seed") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SEED:
        _fail("SEED_INVALID", f"{field} 必须是 0~{MAX_SEED} 的整数，收到：{value!r}")
    return int(value)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------


class KSK_PoseCostumeMatrixBatch:
    """姿势 × 服装矩阵批量（画布原生节点）。"""

    CATEGORY = NODE_CATEGORY
    FUNCTION = "run"
    DESCRIPTION = NODE_DESCRIPTION
    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = (
        "姿势参考图",
        "服装参考图",
        "人物参考图",
        "姿势信息",
        "服装信息",
        "编辑提示词",
        "组合任务信息JSON",
        "文件名前缀",
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "pose_ids_json": ("STRING", {"multiline": True, "default": "[]", "dynamicPrompts": False}),
                "costume_ids_json": ("STRING", {"multiline": True, "default": "[]", "dynamicPrompts": False}),
                "selection_json": ("STRING", {"multiline": True, "default": "{}", "dynamicPrompts": False}),
                "active_index": ("INT", {"default": 0, "min": 0, "max": 1_000_000, "step": 1}),
                "pair_mode": (list(PAIR_MODES), {"default": "cartesian"}),
                "model_qwen": ("BOOLEAN", {"default": True}),
                "model_flux": ("BOOLEAN", {"default": True}),
                "samples_per_pair": ("INT", {"default": 1, "min": 1, "max": MAX_SAMPLES_PER_PAIR, "step": 1}),
                "seed": ("INT", {"default": 9527, "min": 0, "max": MAX_SEED, "step": 1}),
                "prompt_template": ("STRING", {"multiline": True, "default": DEFAULT_PROMPT, "dynamicPrompts": False}),
                "width": ("INT", {"default": 768, "min": MIN_SIDE, "max": MAX_SIDE, "step": 8}),
                "height": ("INT", {"default": 1152, "min": MIN_SIDE, "max": MAX_SIDE, "step": 8}),
                "resize_mode": (list(RESIZE_MODES), {"default": "等比留边"}),
            },
            "optional": {
                "person_image": ("IMAGE",),
                "external_prompt": ("STRING", {"multiline": True, "default": "", "dynamicPrompts": False}),
                "seed_override": ("INT", {"default": -1, "min": -1, "max": MAX_SEED, "step": 1}),
            },
        }

    @classmethod
    def IS_CHANGED(
        cls,
        pose_ids_json: str = "",
        costume_ids_json: str = "",
        selection_json: str = "",
        active_index: int = 0,
        pair_mode: str = "cartesian",
        model_qwen: bool = True,
        model_flux: bool = True,
        samples_per_pair: int = 1,
        seed: int = 9527,
        prompt_template: str = "",
        width: int = 768,
        height: int = 1152,
        resize_mode: str = "等比留边",
        person_image=None,
        external_prompt: str = "",
        seed_override: int = -1,
        **kwargs,
    ):
        """稳定指纹：相同输入（含图库索引版本）→ 相同字符串，节点不会重复执行。"""
        payload = {
            "v": 1,
            "pose_ids": _stable_json_text(pose_ids_json),
            "costume_ids": _stable_json_text(costume_ids_json),
            "selection": _stable_json_text(selection_json),
            "active_index": active_index,
            "pair_mode": pair_mode,
            "model_qwen": bool(model_qwen),
            "model_flux": bool(model_flux),
            "samples": samples_per_pair,
            "seed": seed,
            "prompt": prompt_template,
            "external_prompt": external_prompt,
            "seed_override": seed_override,
            "width": width,
            "height": height,
            "resize_mode": resize_mode,
            "person": None if person_image is None else "connected",
            "pose_index": _index_signature("pose"),
            "costume_index": _index_signature("costume"),
        }
        return "aiksk-pc:" + planner.sha256_hex(planner.canonical_json(payload))[:32]

    def run(
        self,
        pose_ids_json: str = "[]",
        costume_ids_json: str = "[]",
        selection_json: str = "{}",
        active_index: int = 0,
        pair_mode: str = "cartesian",
        model_qwen: bool = True,
        model_flux: bool = True,
        samples_per_pair: int = 1,
        seed: int = 9527,
        prompt_template: str = DEFAULT_PROMPT,
        width: int = 768,
        height: int = 1152,
        resize_mode: str = "等比留边",
        person_image=None,
        external_prompt: str | None = None,
        seed_override: int = -1,
        **_ignored,
    ):
        """执行当前 active_index 对应的一个任务。"""
        try:
            return self._run(
                pose_ids_json=pose_ids_json,
                costume_ids_json=costume_ids_json,
                selection_json=selection_json,
                active_index=active_index,
                pair_mode=pair_mode,
                model_qwen=model_qwen,
                model_flux=model_flux,
                samples_per_pair=samples_per_pair,
                seed=seed,
                prompt_template=prompt_template,
                width=width,
                height=height,
                resize_mode=resize_mode,
                person_image=person_image,
                external_prompt=external_prompt,
                seed_override=seed_override,
            )
        except ValueError as exc:
            if str(exc).startswith("[AIKSK-PC:"):
                raise
            _raise_known(exc)

    # -- 内部实现 ----------------------------------------------------------

    def _run(
        self,
        *,
        pose_ids_json,
        costume_ids_json,
        selection_json,
        active_index,
        pair_mode,
        model_qwen,
        model_flux,
        samples_per_pair,
        seed,
        prompt_template,
        width,
        height,
        resize_mode,
        person_image,
        external_prompt,
        seed_override,
    ):
        width, height = _validate_dims(width, height)
        samples = _validate_samples(samples_per_pair)
        base_seed = _validate_seed(seed)
        override = None
        if seed_override is not None and not isinstance(seed_override, bool) and isinstance(seed_override, int) and seed_override >= 0:
            override = _validate_seed(seed_override, "seed_override")
        index = _validate_active_index(active_index)
        try:
            catalog.validate_resize_mode(resize_mode)
        except ValueError as exc:
            _raise_known(exc)

        try:
            plan = planner.build_plan(
                selection_json,
                pose_ids=pose_ids_json,
                costume_ids=costume_ids_json,
                pair_mode=pair_mode,
                model_qwen=bool(model_qwen),
                model_flux=bool(model_flux),
                samples_per_pair=samples,
                seed=base_seed,
                prompt_template=prompt_template,
            )
            tasks = planner.expand_tasks(plan, job_id="node")
        except ValueError as exc:
            _raise_known(exc)

        if not tasks:
            _fail("NO_PAIRS", "计划中没有任何可执行任务")
        if index >= len(tasks):
            _fail(
                "INDEX_OUT_OF_RANGE",
                f"active_index={index} 超出任务范围 0..{len(tasks) - 1}（共 {len(tasks)} 个任务）",
            )
        task = tasks[index]
        final_seed = override if override is not None else int(task["seed"])

        config = config_module.get_config()
        pair = {"pose": dict(task["pose"]), "costume": dict(task["costume"])}
        try:
            images = catalog.load_pair_images(pair, width, height, resize_mode, config)
        except ValueError as exc:
            _raise_known(exc)

        pose_tensor = images["pose"]["tensor"]
        costume_tensor = images["costume"]["tensor"]

        person_available = person_image is not None
        if person_available:
            person_tensor = person_image
            shape = getattr(person_tensor, "shape", None)
            if shape is None or len(tuple(shape)) != 4 or tuple(shape)[-1] != 3:
                _fail("PERSON_INVALID", "person_image 必须是 [1,H,W,3] 的 IMAGE 张量")
            person_placeholder = False
        else:
            # 契约 §2.2：不伪造人像，第三路输出 1×1 全黑占位并显式标注。
            person_tensor = _person_placeholder()
            person_placeholder = True

        pose_info = catalog.item_info("pose", task["pose_id"], config)
        costume_info = catalog.item_info("costume", task["costume_id"], config)

        if isinstance(external_prompt, str) and external_prompt.strip():
            final_prompt = external_prompt.strip()
        else:
            try:
                final_prompt = planner.render_prompt(plan["prompt_template"], images["pose"]["item"], images["costume"]["item"])
            except ValueError as exc:
                _raise_known(exc)

        filename = planner.filename_prefix(task["pair_id"], final_seed)
        if not planner.is_safe_filename_prefix(filename):  # pragma: no cover - 内部一致性
            _fail("INTERNAL", f"生成的文件名前缀不合法：{filename}")

        task_info = {
            "schema": SCHEMA_TASK,
            "pair_id": task["pair_id"],
            "pair_index": int(task["pair_index"]),
            "pair_total": int(task["pair_total"]),
            "pose_id": task["pose_id"],
            "costume_id": task["costume_id"],
            "model": task["model"],
            "sample_index": int(task["sample_index"]),
            "seed": int(final_seed),
            "person_available": bool(person_available),
            "person_placeholder": bool(person_placeholder),
            "filename_prefix": filename,
        }

        return (
            pose_tensor,
            costume_tensor,
            person_tensor,
            _json_text(pose_info),
            _json_text(costume_info),
            final_prompt,
            _json_text(task_info),
            filename,
        )


__all__ = [
    "KSK_PoseCostumeMatrixBatch",
    "NODE_CATEGORY",
    "NODE_CLASS_NAME",
    "NODE_DESCRIPTION",
    "NODE_DISPLAY_NAME",
    "ERROR_CODES",
    "MODEL_KEYS",
]


# Backward-compatible import alias for existing local API templates.
AIKSK_PoseCostumeMatrix = KSK_PoseCostumeMatrixBatch
