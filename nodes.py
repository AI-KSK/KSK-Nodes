import json
import os
import sys

import folder_paths
import numpy as np
import torch
from PIL import Image, ImageOps


KSK_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
KSK_VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".gif", ".m4v")
KSK_VHS_FORMATS = ["None", "AnimateDiff", "Mochi", "LTXV", "Hunyuan", "Cosmos", "Wan"]
MANUAL_SIZE_SOURCE = "手动宽高"
REFERENCE_IMAGE_SIZE_SOURCE = "参考图尺寸"
VIDEO_ORIGINAL_SIZE_SOURCE = "视频原尺寸"


def ksk_resolve_input_path(filename):
    """Resolve a ComfyUI input filename, including annotated paths when available."""
    if hasattr(folder_paths, "get_annotated_filepath"):
        try:
            path = folder_paths.get_annotated_filepath(filename)
            if path and os.path.exists(path):
                return path
        except Exception:
            pass
    return os.path.join(folder_paths.get_input_directory(), filename)


def ksk_load_image_tensor(filename):
    path = ksk_resolve_input_path(filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到图像文件: {filename} ({path})")

    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]


def ksk_parse_json_list(text, label):
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError(f"{label} 不是合法的 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{label} 必须是一个列表")
    return data


def ksk_get_vhs_load_video():
    """Return VideoHelperSuite's load_video function for VHS_LoadVideo-compatible output."""
    try:
        import nodes as comfy_nodes

        cls = comfy_nodes.NODE_CLASS_MAPPINGS.get("VHS_LoadVideo")
        if cls is not None:
            mod = sys.modules.get(cls.__module__)
            if mod is not None and hasattr(mod, "load_video"):
                return getattr(mod, "load_video")
    except Exception:
        pass

    mod = sys.modules.get("videohelpersuite.load_video_nodes")
    if mod is not None and hasattr(mod, "load_video"):
        return mod.load_video

    try:
        from videohelpersuite.load_video_nodes import load_video

        return load_video
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    vhs_root = os.path.join(os.path.dirname(here), "ComfyUI-VideoHelperSuite")
    if os.path.isdir(vhs_root) and vhs_root not in sys.path:
        sys.path.append(vhs_root)

    try:
        from videohelpersuite.load_video_nodes import load_video

        return load_video
    except Exception as exc:
        raise ImportError(
            "无法导入 VideoHelperSuite 的 load_video，请确认已安装并启用 ComfyUI-VideoHelperSuite。"
            f" 原因: {exc}"
        ) from exc


class KSK_MatrixPairBatch:
    """Matrix pair batch node for image x video combinations."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_files": ("STRING", {"default": "[]", "multiline": True}),
                "video_files": ("STRING", {"default": "[]", "multiline": True}),
                "selection": ("STRING", {"default": "[]", "multiline": True}),
                "active_index": ("INT", {"default": 0, "min": 0, "max": 1000000}),
                "force_rate": ("FLOAT", {"default": 0, "min": 0, "max": 60, "step": 1}),
                "custom_width": ("INT", {"default": 0, "min": 0, "max": 8192}),
                "custom_height": ("INT", {"default": 0, "min": 0, "max": 8192}),
                "frame_load_cap": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF}),
                "skip_first_frames": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF}),
                "select_every_nth": ("INT", {"default": 1, "min": 1, "max": 0x7FFFFFFF}),
                "format": (KSK_VHS_FORMATS, {"default": "AnimateDiff"}),
                "size_source": (
                    [MANUAL_SIZE_SOURCE, REFERENCE_IMAGE_SIZE_SOURCE, VIDEO_ORIGINAL_SIZE_SOURCE],
                    {"default": MANUAL_SIZE_SOURCE},
                ),
            },
            "optional": {
                "vae": ("VAE",),
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "INT", "AUDIO", "VHS_VIDEOINFO", "STRING", "INT")
    RETURN_NAMES = ("参考图", "视频帧", "帧数", "音频", "视频信息", "组合信息", "组合总数")
    FUNCTION = "run"
    CATEGORY = "KSKNODES/批量"
    DESCRIPTION = "图像×视频 矩阵配对批量：可视化网格/树状勾选 + 一次执行排 N 条任务；视频输出与 VHS_LoadVideo 一致。"

    def run(
        self,
        image_files,
        video_files,
        selection,
        active_index,
        force_rate=0,
        custom_width=0,
        custom_height=0,
        frame_load_cap=0,
        skip_first_frames=0,
        select_every_nth=1,
        format="AnimateDiff",
        size_source=MANUAL_SIZE_SOURCE,
        vae=None,
    ):
        images = ksk_parse_json_list(image_files, "图像列表")
        videos = ksk_parse_json_list(video_files, "视频列表")
        pairs = ksk_parse_json_list(selection, "选中组合")

        total = len(pairs)
        if total == 0:
            raise ValueError("没有选中的组合，请在节点网格上勾选「图像 × 视频」的配对。")

        idx = max(0, min(int(active_index), total - 1))
        pair = pairs[idx]
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise ValueError(f"第 {idx} 个组合格式错误，应为 [图像下标, 视频下标]")

        image_index, video_index = int(pair[0]), int(pair[1])
        if not (0 <= image_index < len(images)):
            raise ValueError(f"图像下标 {image_index} 越界（共 {len(images)} 张）")
        if not (0 <= video_index < len(videos)):
            raise ValueError(f"视频下标 {video_index} 越界（共 {len(videos)} 个）")

        image_name = images[image_index]
        video_name = videos[video_index]
        video_path = ksk_resolve_input_path(video_name)
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"找不到视频文件: {video_name} ({video_path})")

        image_tensor = ksk_load_image_tensor(image_name)

        if size_source == REFERENCE_IMAGE_SIZE_SOURCE:
            custom_height = int(image_tensor.shape[1])
            custom_width = int(image_tensor.shape[2])
        elif size_source == VIDEO_ORIGINAL_SIZE_SOURCE:
            custom_width = 0
            custom_height = 0

        load_video = ksk_get_vhs_load_video()
        frames, frame_count, audio, video_info = load_video(
            video=video_path,
            force_rate=force_rate,
            custom_width=custom_width,
            custom_height=custom_height,
            frame_load_cap=frame_load_cap,
            skip_first_frames=skip_first_frames,
            select_every_nth=select_every_nth,
            format=format,
            vae=vae,
        )

        size_info = (
            f"{size_source}({custom_width}x{custom_height})"
            if custom_width and custom_height
            else size_source
        )
        info = (
            f"组合 {idx + 1}/{total} | 图像[{image_index}]={image_name} | "
            f"视频[{video_index}]={video_name} | 尺寸={size_info}"
        )
        print(f"\033[96m[KSK MatrixPairBatch] {info} | 帧数={frame_count}\033[0m")

        return (image_tensor, frames, frame_count, audio, video_info, info, total)

    @classmethod
    def IS_CHANGED(cls, image_files, video_files, selection, active_index, **kwargs):
        return f"{image_files}|{video_files}|{selection}|{active_index}|{kwargs}"
