"""Shared constants for the AIKSK Pose x Costume canvas node."""

from __future__ import annotations

PLUGIN_VERSION = "1.0.0"
SCHEMA_PLAN = "aiksk.pose_costume.plan.v1"
SCHEMA_TASK = "aiksk.pc.node_task.v1"

MODEL_KEYS = ("qwen21", "flux3klein")
MODEL_LABELS = {"qwen21": "Qwen-Image 2.1", "flux3klein": "FLUX.3 Klein"}
PAIR_MODES = ("cartesian", "zip", "manual")
SEED_POLICIES = ("fixed", "increment", "random_recorded")
RESIZE_MODES = ("原始尺寸", "等比留边", "居中裁剪", "拉伸")
GALLERY_KINDS = ("pose", "costume")

DEFAULT_PROMPT = (
    "Full-body photorealistic fashion photograph of one complete person, head to toe "
    "(head, face, hair, hands and feet visible), clean neutral studio background, natural lighting, sharp details. "
    "Strictly keep the body pose, limb placement and camera framing from the pose reference image "
    "(the skeleton pose guide). Dress the subject in the complete outfit from the costume reference image; "
    "preserve garment structure, fabric texture, colors and accessories. "
    "Pose hint: {pose}. Outfit keywords: {costume}."
)

MAX_PAIRS = 10_000
MAX_TASKS = 10_000
WARN_OVER_TASKS = 100
MAX_SAMPLES_PER_PAIR = 8
MAX_IMAGE_PIXELS = 40_000_000
DEFAULT_MAX_IN_FLIGHT = 1
HARD_MAX_IN_FLIGHT = 4

__all__ = [name for name in dir() if name.isupper()]
