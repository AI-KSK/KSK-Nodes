import os
import shutil

from .nodes import KSK_IMAGE_EXTS, KSK_VIDEO_EXTS, KSK_MatrixPairBatch


NODE_CLASS_MAPPINGS = {
    "KSK_MatrixPairBatch": KSK_MatrixPairBatch,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "KSK_MatrixPairBatch": "KSKNODES 矩阵配对批量 (图×视频)",
}

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


try:
    from aiohttp import web
    from server import PromptServer
    import folder_paths

    @PromptServer.instance.routes.post("/ksk/matrix_pair/upload")
    async def ksk_matrix_pair_upload(request):
        """Upload an image or video into ComfyUI's input directory."""
        try:
            post = await request.post()
            upload = post.get("file")
            if upload is None:
                return web.json_response({"error": "缺少文件"}, status=400)

            filename = os.path.basename(upload.filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext in KSK_IMAGE_EXTS:
                kind = "image"
            elif ext in KSK_VIDEO_EXTS:
                kind = "video"
            else:
                return web.json_response({"error": f"不支持的文件类型: {ext}"}, status=400)

            input_dir = folder_paths.get_input_directory()
            os.makedirs(input_dir, exist_ok=True)

            dest = os.path.join(input_dir, filename)
            base, suffix = os.path.splitext(filename)
            number = 1
            while os.path.exists(dest):
                filename = f"{base}_{number}{suffix}"
                dest = os.path.join(input_dir, filename)
                number += 1

            with open(dest, "wb") as file:
                shutil.copyfileobj(upload.file, file)

            return web.json_response({"name": filename, "kind": kind})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    @PromptServer.instance.routes.get("/ksk/matrix_pair/list")
    async def ksk_matrix_pair_list(request):
        """List image and video files in ComfyUI's input directory."""
        input_dir = folder_paths.get_input_directory()
        images, videos = [], []
        try:
            for name in sorted(os.listdir(input_dir)):
                full = os.path.join(input_dir, name)
                if not os.path.isfile(full):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in KSK_IMAGE_EXTS:
                    images.append(name)
                elif ext in KSK_VIDEO_EXTS:
                    videos.append(name)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response({"images": images, "videos": videos})

    print("[KSKNODES] Matrix Pair Batch HTTP routes registered")
except Exception as exc:
    print(f"[KSKNODES] Warning: failed to register Matrix Pair Batch HTTP routes: {exc}")
