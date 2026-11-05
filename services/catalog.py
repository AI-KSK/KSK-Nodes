"""只读图库访问层：发现图库、加载/校验 JSON 索引、安全解析素材路径、读取参考图。

契约 §4：

* 发现顺序：环境变量 ``AIKSK_PC_POSE_ROOT`` / ``AIKSK_PC_COSTUME_ROOT``
  → ComfyUI ``custom_nodes`` 下目录名匹配 → ``GALLERY_MISSING``；
* 索引校验：ID 唯一、路径必须是图库内相对路径（无 ``\\``、非绝对、resolve 后仍在根内）、
  扩展名白名单、40MP 上限、真实文件存在；
* 只读：绝不复制/修改图库素材。

性能说明：索引构建只做 ``stat`` 级校验（一次性，按索引 mtime+size 缓存）；
真实像素尺寸的 40MP 校验在真正读取图片时执行（避免启动时批量打开数千张图，
Windows Defender 逐文件扫描会让批量 open 慢到不可接受）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

from . import (
    GALLERY_KINDS,
    MAX_IMAGE_PIXELS,
    PLUGIN_VERSION,
    RESIZE_MODES,
)
from . import config as config_module

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
POSE_INDEX_FILE = "pose-zh.json"
COSTUME_INDEX_FILE = "costume-zh.json"
POSE_DIR_HINTS = (
    "ComfyUI_Pose-Gallery_ZH",
    "ComfyUI-Pose-Gallery_ZH",
    "ComfyUI_Pose_Gallery_ZH",
    "ComfyUI-Pose-Gallery",
    "pose-gallery",
    "pose_gallery",
    "Pose-Gallery",
)
COSTUME_DIR_HINTS = (
    "ComfyUI_Costume-Gallery_ZH",
    "ComfyUI-Costume-Gallery_ZH",
    "ComfyUI_Costume_Gallery_ZH",
    "ComfyUI-Costume-Gallery",
    "costume-gallery",
    "costume_gallery",
    "Costume-Gallery",
)
ANGLE_ORDER = ("正面", "斜侧", "侧面", "俯视", "仰视")
MAX_LOAD_SIDE = 8192
MIN_LOAD_SIDE = 1
MAX_INDEX_CACHE = 8

_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CatalogError(ValueError):
    """带错误码的图库错误；``str(exc)`` 只返回中文消息。"""

    def __init__(self, code: str, message: str, detail=None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover
        return self.message


def _index_file_name(kind: str) -> str:
    return POSE_INDEX_FILE if kind == "pose" else COSTUME_INDEX_FILE


def _check_kind(kind: str) -> str:
    if kind not in GALLERY_KINDS:
        raise CatalogError("KIND_INVALID", f"未知图库类型：{kind!r}（仅支持 pose / costume）")
    return kind


def _has_index(root: Path, kind: str) -> bool:
    try:
        return (root / "data" / _index_file_name(kind)).is_file()
    except OSError:  # pragma: no cover
        return False


def _env_root(kind: str) -> Path | None:
    name = config_module.ENV_POSE_ROOT if kind == "pose" else config_module.ENV_COSTUME_ROOT
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return None
    return Path(os.path.expandvars(raw.strip())).expanduser()


def _descend_candidates(root: Path, kind: str) -> Path | None:
    """允许传入“包含单个图库子目录”的父目录（例如 custom_nodes 本身）。"""
    try:
        if not root.is_dir():
            return None
        children = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return None
    matches = [path for path in children if _has_index(path, kind)]
    if len(matches) == 1:
        return matches[0]
    return None


def _match_in_custom_nodes(kind: str) -> Path | None:
    hints = POSE_DIR_HINTS if kind == "pose" else COSTUME_DIR_HINTS
    roots = config_module.custom_nodes_dirs()
    for base in roots:
        try:
            if not base.is_dir():
                continue
        except OSError:  # pragma: no cover
            continue
        for name in hints:
            candidate = base / name
            if _has_index(candidate, kind):
                return candidate
        try:
            children = sorted(path for path in base.iterdir() if path.is_dir())
        except OSError:  # pragma: no cover
            continue
        lowered_hints = [name.lower() for name in hints]
        for child in children:
            lowered = child.name.lower()
            if lowered in lowered_hints and _has_index(child, kind):
                return child
        for child in children:
            lowered = child.name.lower()
            if "gallery" not in lowered:
                continue
            if kind == "pose" and "pose" not in lowered:
                continue
            if kind == "costume" and "costume" not in lowered:
                continue
            if _has_index(child, kind):
                return child
    return None


def discover_root(kind: str, config: config_module.Config | None = None) -> Path:
    """按契约 §4 顺序发现图库根目录；找不到时报 ``GALLERY_MISSING``。"""
    _check_kind(kind)
    config = config or config_module.get_config()
    env_name = config_module.ENV_POSE_ROOT if kind == "pose" else config_module.ENV_COSTUME_ROOT
    env_root = _env_root(kind)
    if env_root is not None:
        path = env_root if _has_index(env_root, kind) else _descend_candidates(env_root, kind)
        if path is None:
            raise CatalogError(
                "GALLERY_MISSING",
                f"环境变量 {env_name} 指向的目录不是有效的{_kind_label(kind)}图库：{env_root}",
                {"root": str(env_root), "source": f"env:{env_name}"},
            )
        return path.resolve()
    explicit = config.resolve_pose_root() if kind == "pose" else config.resolve_costume_root()
    if explicit is not None:
        path = explicit if _has_index(explicit, kind) else _descend_candidates(explicit, kind)
        if path is None:
            raise CatalogError(
                "GALLERY_MISSING",
                f"配置指定的{_kind_label(kind)}图库无效：{explicit}",
                {"root": str(explicit), "source": "config"},
            )
        return path.resolve()
    matched = _match_in_custom_nodes(kind)
    if matched is not None:
        return matched.resolve()
    searched = ", ".join(str(path) for path in config_module.custom_nodes_dirs()) or "(无 custom_nodes)"
    raise CatalogError(
        "GALLERY_MISSING",
        f"未找到{_kind_label(kind)}图库（ComfyUI custom_nodes 目录：{searched}）",
        {"searched": [str(path) for path in config_module.custom_nodes_dirs()]},
    )


def _kind_label(kind: str) -> str:
    return "姿势" if kind == "pose" else "服装"


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------


def safe_relative_path(root: Path, relative: str) -> Path:
    """把索引里的“图库内相对路径”解析为绝对路径，并做完整安全校验。"""
    if not isinstance(relative, str) or not relative.strip():
        raise CatalogError("ASSET_INVALID", "素材路径为空")
    value = relative.strip()
    if "\\" in value:
        raise CatalogError("ASSET_INVALID", f"素材路径不得包含反斜杠：{value}")
    if value.startswith("/") or _ABS_DRIVE_RE.match(value) or Path(value).is_absolute():
        raise CatalogError("ASSET_INVALID", f"素材必须使用图库内相对路径：{value}")
    parts = [part for part in value.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise CatalogError("ASSET_INVALID", f"素材路径不得包含 . 或 .. 路径段：{value}")
    root_resolved = root.resolve()
    candidate = (root_resolved / Path(*parts)).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise CatalogError("ASSET_INVALID", f"素材路径越出图库根目录：{value}")
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        raise CatalogError("ASSET_INVALID", f"素材扩展名不在白名单（{', '.join(sorted(IMAGE_EXTENSIONS))}）：{value}")
    return candidate


def _validate_declared_pixels(row: dict, root: Path, relative: str) -> None:
    """校验索引中声明的图片尺寸（服装图库带「宽/高」字段）。"""
    width = row.get("宽")
    height = row.get("高")
    if isinstance(width, int) and not isinstance(width, bool) and isinstance(height, int) and not isinstance(height, bool):
        if width > 0 and height > 0 and width * height > MAX_IMAGE_PIXELS:
            raise CatalogError(
                "IMAGE_TOO_LARGE",
                f"素材像素数 {width}×{height} 超过 {MAX_IMAGE_PIXELS} 上限：{relative}",
            )


def _require_existing(path: Path, relative: str) -> None:
    try:
        if not path.is_file():
            raise CatalogError("ASSET_MISSING", f"素材文件缺失：{relative}")
    except OSError as exc:  # pragma: no cover
        raise CatalogError("ASSET_MISSING", f"素材文件无法访问：{relative}（{exc}）") from exc


# ---------------------------------------------------------------------------
# 索引加载与缓存
# ---------------------------------------------------------------------------


class CatalogIndex:
    """一个图库的只读索引（items + 快速查表 + 路径解析）。"""

    def __init__(self, kind: str, root: Path, signature: tuple, items: list[dict], angles, categories, aliases, systems):
        self.kind = kind
        self.root = root
        self.signature = signature
        self.items = items
        self.by_id = {item["id"]: item for item in items}
        self.angles = angles
        self.categories = categories
        self.aliases = aliases
        self.systems = systems
        self.groups_count = len({item.get("system") for item in items if item.get("system")})

    @property
    def count(self) -> int:
        return len(self.items)

    def item(self, item_id) -> dict:
        if not isinstance(item_id, str) or not item_id.strip():
            raise CatalogError("ASSET_NOT_FOUND", "素材 ID 为空")
        found = self.by_id.get(item_id.strip())
        if found is None:
            raise CatalogError(
                "ASSET_NOT_FOUND",
                f"{_kind_label(self.kind)}图库中找不到素材 ID：{item_id}",
            )
        return found

    def relative_path(self, item_id, variant: str = "original") -> str:
        item = self.item(item_id)
        if variant not in ("thumb", "original", "preview"):
            raise CatalogError("VARIANT_INVALID", f"未知图片规格：{variant!r}（仅支持 thumb / original）")
        relative = item.get(variant) or item.get("original")
        return relative

    def path(self, item_id, variant: str = "original") -> Path:
        item = self.item(item_id)
        relative = self.relative_path(item_id, variant)
        path = safe_relative_path(self.root, relative)
        if not path.is_file():
            fallback = item.get("original")
            if fallback and fallback != relative:
                path = safe_relative_path(self.root, fallback)
            if not path.is_file():
                raise CatalogError("ASSET_MISSING", f"素材文件缺失：{relative}")
        return path

    def fingerprint(self) -> str:
        """轻量指纹：根目录 + 索引文件签名 + 数量（用于缓存/日志，不遍历素材）。"""
        material = f"{self.root}|{self.signature!r}|{self.kind}|{self.count}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def payload(self) -> dict:
        """契约 §6 ``/catalogue`` 降级清单结构。"""
        items = [item_payload(self.kind, item) for item in self.items]
        result = {
            "schema": "aiksk.pc.catalogue.v1",
            "kind": self.kind,
            "source": "builtin",
            "count": self.count,
            "items": items,
            "categories": list(self.categories),
            "aliases": dict(self.aliases),
            "generator": f"KSK_PoseCostumeMatrixBatch/{PLUGIN_VERSION}",
        }
        if self.kind == "pose":
            result["angles"] = list(self.angles)
            result["systems"] = list(self.systems)
            result["groups"] = self.groups_count
        return result


def item_payload(kind: str, item: dict) -> dict:
    """单条素材的降级 API 结构（前端弹窗直接用这个）。"""
    item_id = item["id"]
    quoted = quote(item_id, safe="")
    return {
        "id": item_id,
        "label": item.get("label") or item_id,
        "category": item.get("category") or "",
        "angle": item.get("angle") or "",
        "tags": list(item.get("tags") or []),
        "keywords_en": item.get("keywords_en") or "",
        "keywords_zh": item.get("keywords_zh") or "",
        "width": item.get("width"),
        "height": item.get("height"),
        "thumb_url": f"/aiksk_pc/image?kind={kind}&id={quoted}&variant=thumb",
        "original_url": f"/aiksk_pc/image?kind={kind}&id={quoted}&variant=original",
    }


_CACHE_LOCK = threading.RLock()
_INDEX_CACHE: dict[tuple, CatalogIndex] = {}
_CACHE_HITS = 0


def _index_signature(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError as exc:
        raise CatalogError("GALLERY_MISSING", f"无法读取图库索引文件：{path}") from exc
    return (stat.st_mtime_ns, stat.st_size)


def _row_text(row: dict, *keys) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_pose_row(row: dict, root: Path, index: int) -> dict:
    if not isinstance(row, dict):
        raise CatalogError("INDEX_INVALID", f"第 {index + 1} 条姿势素材必须是对象")
    item_id = row.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise CatalogError("INDEX_INVALID", f"第 {index + 1} 条姿势素材缺少文本 ID")
    item_id = item_id.strip()
    original = _row_text(row, "原图", "大图", "缩略图")
    if not original:
        raise CatalogError("INDEX_INVALID", f"姿势素材缺少图片路径：{item_id}")
    thumb = _row_text(row, "缩略图") or original
    safe_relative_path(root, original)
    safe_relative_path(root, thumb)
    _require_existing(safe_relative_path(root, original), original)
    # 缩略图缺失时降级用原图（不阻塞索引，也不复制素材）。
    if thumb != original:
        try:
            thumb_path = safe_relative_path(root, thumb)
            if not thumb_path.is_file():
                thumb = original
        except CatalogError:
            raise
    label_name = _row_text(row, "姿势名称", "分类") or item_id
    angle = _row_text(row, "角度")
    system = _row_text(row, "体系")
    category = _row_text(row, "分类", "体系")
    number = _row_text(row, "姿势编号")
    search = _row_text(row, "检索")
    if not search:
        search = " ".join(part for part in (item_id, number, system, category, label_name, angle) if part)
    return {
        "id": item_id,
        "kind": "pose",
        "source": "builtin",
        "label": f"{label_name} · {angle}" if angle else label_name,
        "name": label_name,
        "category": category,
        "angle": angle,
        "system": system,
        "pose_number": number,
        "tags": [system] if system else [],
        "keywords_en": " ".join(part for part in (number, item_id) if part),
        "keywords_zh": " ".join(part for part in (label_name, angle, system) if part),
        "thumb": thumb,
        "original": original,
        "preview": None,
        "width": None,
        "height": None,
        "search": search.lower(),
    }


def _normalize_costume_row(row: dict, root: Path, index: int) -> dict:
    if not isinstance(row, dict):
        raise CatalogError("INDEX_INVALID", f"第 {index + 1} 条服装素材必须是对象")
    item_id = row.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise CatalogError("INDEX_INVALID", f"第 {index + 1} 条服装素材缺少文本 ID")
    item_id = item_id.strip()
    original = _row_text(row, "原图", "大图", "缩略图")
    if not original:
        raise CatalogError("INDEX_INVALID", f"服装素材缺少图片路径：{item_id}")
    thumb = _row_text(row, "缩略图") or original
    preview = _row_text(row, "大图")
    safe_relative_path(root, original)
    safe_relative_path(root, thumb)
    _require_existing(safe_relative_path(root, original), original)
    if preview:
        safe_relative_path(root, preview)
        try:
            if not safe_relative_path(root, preview).is_file():
                preview = ""
        except CatalogError:
            raise
    if thumb != original:
        try:
            if not safe_relative_path(root, thumb).is_file():
                thumb = original
        except CatalogError:
            raise
    _validate_declared_pixels(row, root, original)
    tags_value = row.get("标签")
    if isinstance(tags_value, list) and all(isinstance(tag, str) for tag in tags_value):
        tags = [tag for tag in tags_value if tag.strip()]
    else:
        keywords_zh_raw = _row_text(row, "关键词中文")
        tags = [part.strip() for part in re.split(r"[·,，;；/|]+", keywords_zh_raw) if part.strip()]
    category = _row_text(row, "分类")
    keywords_en = _row_text(row, "关键词")
    keywords_zh = _row_text(row, "关键词中文")
    search = _row_text(row, "检索")
    if not search:
        search = " ".join([item_id, category, keywords_en, keywords_zh, *tags])
    width = row.get("宽")
    height = row.get("高")
    return {
        "id": item_id,
        "kind": "costume",
        "source": "builtin",
        "label": keywords_zh or keywords_en or category or item_id,
        "name": keywords_zh or keywords_en or item_id,
        "category": category,
        "angle": "",
        "system": "",
        "tags": tags,
        "keywords_en": keywords_en,
        "keywords_zh": keywords_zh,
        "thumb": thumb,
        "original": original,
        "preview": preview or None,
        "width": int(width) if isinstance(width, int) and not isinstance(width, bool) else None,
        "height": int(height) if isinstance(height, int) and not isinstance(height, bool) else None,
        "search": search.lower(),
    }


def _load_meta(root: Path, kind: str) -> tuple[dict, list]:
    """读取 data/meta-zh.json（服装同义词、姿势体系顺序）；缺失/坏格式不阻塞。"""
    path = root / "data" / "meta-zh.json"
    if not path.is_file():
        return {}, []
    try:
        meta = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}, []
    if not isinstance(meta, dict):
        return {}, []
    aliases: dict = {}
    raw_aliases = meta.get("同义词")
    if isinstance(raw_aliases, dict):
        for key, values in raw_aliases.items():
            if isinstance(key, str) and isinstance(values, list) and all(isinstance(value, str) for value in values):
                aliases[key] = list(values)
    systems = meta.get("体系分类")
    systems = [value for value in systems if isinstance(value, str)] if isinstance(systems, list) else []
    return aliases, systems


def _build_index(root: Path, kind: str) -> CatalogIndex:
    index_path = root / "data" / _index_file_name(kind)
    signature = _index_signature(index_path)
    try:
        rows = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise CatalogError("INDEX_INVALID", f"无法读取图库索引：{index_path}") from exc
    except ValueError as exc:
        raise CatalogError("INDEX_INVALID", f"图库索引不是合法 JSON：{index_path}（{exc}）") from exc
    if not isinstance(rows, list) or not rows:
        raise CatalogError("INDEX_INVALID", f"图库索引必须是非空数组：{index_path}")
    items: list[dict] = []
    by_id: dict[str, dict] = {}
    for index, row in enumerate(rows):
        item = _normalize_pose_row(row, root, index) if kind == "pose" else _normalize_costume_row(row, root, index)
        if item["id"] in by_id:
            raise CatalogError("INDEX_INVALID", f"图库 ID 重复：{item['id']}")
        items.append(item)
        by_id[item["id"]] = item
    if kind == "pose":
        present = {item["angle"] for item in items if item["angle"]}
        angles = [angle for angle in ANGLE_ORDER if angle in present]
        angles.extend(sorted(present - set(angles)))
        _, systems = _load_meta(root, kind)
        if not systems:
            systems = list(dict.fromkeys(item["system"] for item in items if item["system"]))
        categories = list(dict.fromkeys(item["category"] for item in items if item["category"]))
        return CatalogIndex(kind, root, signature, items, angles, categories, {}, systems)
    aliases, _systems = _load_meta(root, kind)
    categories = list(dict.fromkeys(item["category"] for item in items if item["category"]))
    return CatalogIndex(kind, root, signature, items, [], categories, aliases, [])


def catalogue(kind: str, config: config_module.Config | None = None) -> CatalogIndex:
    """获取（必要时构建并缓存）图库索引。"""
    global _CACHE_HITS
    _check_kind(kind)
    root = discover_root(kind, config)
    signature = _index_signature(root / "data" / _index_file_name(kind))
    key = (kind, str(root), signature[0], signature[1])
    with _CACHE_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            _CACHE_HITS += 1
            return cached
    index = _build_index(root, kind)
    with _CACHE_LOCK:
        if len(_INDEX_CACHE) >= MAX_INDEX_CACHE:
            for stale_key in list(_INDEX_CACHE)[: len(_INDEX_CACHE) - MAX_INDEX_CACHE + 1]:
                _INDEX_CACHE.pop(stale_key, None)
        _INDEX_CACHE[key] = index
    return index


def clear_caches() -> None:
    """清空索引缓存（测试/替换图库后调用）。"""
    global _CACHE_HITS
    with _CACHE_LOCK:
        _INDEX_CACHE.clear()
        _CACHE_HITS = 0


def cache_stats() -> dict:
    with _CACHE_LOCK:
        return {"entries": len(_INDEX_CACHE), "hits": _CACHE_HITS}


def catalogue_payload(kind: str, config: config_module.Config | None = None) -> dict:
    """契约 §6 ``GET /catalogue`` 的降级清单。"""
    return catalogue(kind, config).payload()


def catalogue_item(kind: str, item_id: str, config: config_module.Config | None = None) -> dict:
    return catalogue(kind, config).item(item_id)


def item_info(kind: str, item_id: str, config: config_module.Config | None = None) -> dict:
    """节点输出用的素材信息 JSON（契约 §2.3：id/source/label/category/angle|tags/keywords）。"""
    item = catalogue_item(kind, item_id, config)
    info = {
        "id": item["id"],
        "source": item["source"],
        "label": item.get("label") or item["id"],
        "category": item.get("category") or "",
        "keywords_en": item.get("keywords_en") or "",
        "keywords_zh": item.get("keywords_zh") or "",
    }
    if kind == "pose":
        info["name"] = item.get("name") or ""
        info["angle"] = item.get("angle") or ""
        if item.get("system"):
            info["system"] = item["system"]
    else:
        info["tags"] = list(item.get("tags") or [])
        if item.get("width") and item.get("height"):
            info["width"] = int(item["width"])
            info["height"] = int(item["height"])
    return info


def image_path(kind: str, item_id: str, variant: str = "original", config: config_module.Config | None = None) -> Path:
    """路由 ``/image`` 使用的安全路径解析。"""
    if variant not in ("thumb", "original"):
        raise CatalogError("VARIANT_INVALID", f"未知图片规格：{variant!r}（仅支持 thumb / original）")
    return catalogue(kind, config).path(item_id, variant)


def gallery_status(config: config_module.Config | None = None) -> dict:
    """``/status`` 用的两库状态（缺失不抛异常）。"""
    result: dict = {}
    for kind in GALLERY_KINDS:
        try:
            index = catalogue(kind, config)
            result[kind] = {
                "found": True,
                "count": index.count,
                "root": str(index.root),
                "cache": {"mtime_ns": index.signature[0], "size": index.signature[1]},
                "error": None,
            }
        except CatalogError as exc:
            result[kind] = {"found": False, "count": 0, "root": None, "cache": None,
                            "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # pragma: no cover - 防御
            result[kind] = {"found": False, "count": 0, "root": None, "cache": None,
                            "error": {"code": "GALLERY_MISSING", "message": str(exc)}}
    return result


# ---------------------------------------------------------------------------
# 图片读取与缩放（四模式）
# ---------------------------------------------------------------------------


def _import_pil():
    try:
        from PIL import Image, ImageOps  # type: ignore

        return Image, ImageOps
    except Exception as exc:  # pragma: no cover - 环境缺 PIL
        raise CatalogError("PIL_MISSING", "当前 Python 环境缺少 Pillow，无法读取参考图") from exc


def _import_torch():
    try:
        import torch  # type: ignore

        return torch
    except Exception as exc:  # pragma: no cover - 无 torch 环境
        raise CatalogError("TORCH_MISSING", "当前 Python 环境缺少 torch，无法转换为 IMAGE 张量") from exc


def validate_size(width, height) -> tuple[int, int]:
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise CatalogError("BAD_SIZE", f"{name} 必须是整数，收到：{value!r}")
        if not MIN_LOAD_SIDE <= value <= MAX_LOAD_SIDE:
            raise CatalogError("BAD_SIZE", f"{name} 必须在 {MIN_LOAD_SIDE}~{MAX_LOAD_SIDE} 像素之间")
    return int(width), int(height)


def validate_resize_mode(mode: str) -> str:
    if mode not in RESIZE_MODES:
        raise CatalogError("BAD_RESIZE_MODE", f"未知缩放模式：{mode!r}（仅支持 {' / '.join(RESIZE_MODES)}）")
    return mode


def open_image_rgb(path: Path) -> "object":
    """以 RGB 读取图片；先做 40MP 上限校验，避免解压炸弹。"""
    Image, ImageOps = _import_pil()
    opened = None
    try:
        opened = Image.open(path)
        width, height = opened.size
        if width <= 0 or height <= 0:
            raise CatalogError("IMAGE_INVALID", f"参考图尺寸非法：{Path(path).name} {width}×{height}")
        if width * height > MAX_IMAGE_PIXELS:
            raise CatalogError(
                "IMAGE_TOO_LARGE",
                f"参考图 {Path(path).name} 像素数 {width}×{height} 超过 {MAX_IMAGE_PIXELS} 上限",
            )
        if opened.mode in ("RGBA", "LA", "P"):
            opened = opened.convert("RGBA")
        transposed = ImageOps.exif_transpose(opened)
        return transposed.convert("RGB")
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError("IMAGE_INVALID", f"参考图解码失败：{Path(path).name}（{exc}）") from exc
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:  # pragma: no cover
                pass


def _resampling():
    Image, _ = _import_pil()
    return getattr(Image, "Resampling", Image).LANCZOS


def resize_image(image, width: int, height: int, mode: str):
    """四种缩放模式（契约 §2.1）。

    * ``原始尺寸``：原样返回，忽略 width/height；
    * ``等比留边``：等比缩放到框内，四周补黑边（letterbox）；
    * ``居中裁剪``：先等比放大到覆盖目标框，再居中裁剪；
    * ``拉伸``：直接拉伸到目标尺寸。
    """
    Image, ImageOps = _import_pil()
    validate_resize_mode(mode)
    width, height = validate_size(width, height)
    if mode == "原始尺寸":
        return image
    src_w, src_h = image.size
    resample = _resampling()
    if mode == "拉伸":
        return image.resize((width, height), resample)
    if mode == "等比留边":
        scale = min(width / src_w, height / src_h)
        target_w = max(1, min(width, round(src_w * scale)))
        target_h = max(1, min(height, round(src_h * scale)))
        scaled = image.resize((target_w, target_h), resample)
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        canvas.paste(scaled, ((width - target_w) // 2, (height - target_h) // 2))
        return canvas
    # 居中裁剪
    scale = max(width / src_w, height / src_h)
    target_w = max(width, round(src_w * scale))
    target_h = max(height, round(src_h * scale))
    scaled = image.resize((target_w, target_h), resample)
    left = max(0, (target_w - width) // 2)
    top = max(0, (target_h - height) // 2)
    return scaled.crop((left, top, left + width, top + height))


def _resolve_pair_ids(pair) -> tuple[dict, dict]:
    if not isinstance(pair, dict):
        raise CatalogError("PAIR_INVALID", "image pair 必须是 {pose,costume} 对象")
    pose = pair.get("pose")
    costume = pair.get("costume")
    if not isinstance(pose, dict) or not isinstance(costume, dict):
        raise CatalogError("PAIR_INVALID", "image pair 必须包含 pose / costume 两个对象")
    for side in (pose, costume):
        source = side.get("source", "builtin")
        if source not in ("builtin", None, ""):
            raise CatalogError(
                "SOURCE_UNSUPPORTED",
                f"本节点只读取内置图库素材，暂不支持来源：{source}",
            )
    if not isinstance(pose.get("id"), str) or not pose["id"].strip():
        raise CatalogError("PAIR_INVALID", "姿势 ID 为空")
    if not isinstance(costume.get("id"), str) or not costume["id"].strip():
        raise CatalogError("PAIR_INVALID", "服装 ID 为空")
    return pose, costume


def load_pair_pil(pair, width: int, height: int, resize_mode: str, config: config_module.Config | None = None) -> dict:
    """读取一对参考图并缩放到目标尺寸；返回 PIL 图像（不依赖 torch）。"""
    width, height = validate_size(width, height)
    validate_resize_mode(resize_mode)
    pose, costume = _resolve_pair_ids(pair)
    pose_index = catalogue("pose", config)
    costume_index = catalogue("costume", config)
    result: dict = {"width": width, "height": height, "resize_mode": resize_mode}
    for side_name, index, selection in (
        ("pose", pose_index, pose),
        ("costume", costume_index, costume),
    ):
        item = index.item(selection["id"])
        original_relative = index.relative_path(item["id"], "original")
        path = index.path(item["id"], "original")
        image = open_image_rgb(path)
        resized = resize_image(image, width, height, resize_mode)
        result[side_name] = {
            "item": item,
            "image": resized,
            "path": str(path),
            "relative": original_relative,
            "original_size": [image.width, image.height],
            "size": [resized.width, resized.height],
        }
    return result


def pil_to_tensor(image):
    """PIL RGB → IMAGE 张量 ``float32[1,H,W,3]``（0..1）。"""
    np = _import_numpy()
    torch = _import_torch()
    array = np.array(image.convert("RGB"), dtype=np.float32) / 255.0
    tensor = torch.from_numpy(array).unsqueeze(0).contiguous()
    if tensor.dim() != 4 or tensor.shape[-1] != 3:
        raise CatalogError("IMAGE_INVALID", f"参考图张量形状异常：{tuple(tensor.shape)}")
    return tensor


def _import_numpy():
    try:
        import numpy as np  # type: ignore

        return np
    except Exception as exc:  # pragma: no cover
        raise CatalogError("NUMPY_MISSING", "当前 Python 环境缺少 numpy，无法转换为 IMAGE 张量") from exc


def load_pair_images(
    pair,
    width: int,
    height: int,
    resize_mode: str,
    config: config_module.Config | None = None,
) -> dict:
    """契约 §4：节点 ``run()`` 用同一 catalog 读取当前任务的两张参考图。

    返回 ``{"pose": {...}, "costume": {...}, "width", "height", "resize_mode"}``，
    其中每侧含 ``tensor``（float32[1,H,W,3]，0..1）、``image``、``item``、
    ``original_size``、``size``。
    """
    data = load_pair_pil(pair, width, height, resize_mode, config)
    for side in ("pose", "costume"):
        data[side]["tensor"] = pil_to_tensor(data[side]["image"])
    return data


__all__ = [
    "ANGLE_ORDER",
    "CatalogError",
    "CatalogIndex",
    "IMAGE_EXTENSIONS",
    "cache_stats",
    "catalogue",
    "catalogue_item",
    "catalogue_payload",
    "clear_caches",
    "discover_root",
    "gallery_status",
    "image_path",
    "item_info",
    "item_payload",
    "load_pair_images",
    "load_pair_pil",
    "open_image_rgb",
    "pil_to_tensor",
    "resize_image",
    "safe_relative_path",
    "validate_resize_mode",
    "validate_size",
]
