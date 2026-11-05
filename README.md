# KSK Nodes

ComfyUI custom nodes by **AI-KSK**.

This repository contains two matrix batch nodes:

| Node | Registration key | Purpose |
| --- | --- | --- |
| 矩阵配对批量 (图 × 视频) | `KSK_MatrixPairBatch` | Pair multiple reference images with multiple videos |
| 姿势 × 服装矩阵批量 | `KSK_PoseCostumeMatrixBatch` | Batch-generate pose × costume combinations from gallery plugins |

Both nodes can be used on the same canvas. They do not share node state, DOM panels or HTTP routes.

---

## 1. KSK_MatrixPairBatch / 矩阵配对批量 (图 × 视频)

The original node is preserved:

- Matrix view: rows = images, columns = videos.
- Tree view: each image expands into its selected videos.
- One click queues each selected image-video pair as an independent ComfyUI task.
- Video output is compatible with `VHS_LoadVideo` from **ComfyUI-VideoHelperSuite**.
- Size mode: manual width/height, reference image size, or original video size.
- Uploads use ComfyUI's input directory and the existing `/ksk/matrix_pair/*` routes.

## 2. KSK_PoseCostumeMatrixBatch / 姿势 × 服装矩阵批量

A canvas-native matrix node for **pose × costume** combinations.

Highlights:

- Select poses and costumes independently in in-page gallery modals.
- Cartesian product or manual pair mode.
- Multi-select and task count preview.
- Shared prompt template for a whole batch, plus overrides.
- Optional person reference image (single image or same-character three-view board).
- Qwen-Image 2.1 and FLUX.3 Klein API workflow templates.
- Seed policy, width/height/resize controls.
- Dry-run preview before submitting.
- One ComfyUI queue task per generated image (no hidden workflow batch).
- Pause, resume, cancel and retry failed tasks.
- Gallery dependency detection with actionable errors.

The node does **not** force a specific cloud platform, model path or authentication token. It reads the community gallery plugins through their public files/ComfyUI APIs.

---

## Install

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/AI-KSK/KSK-Nodes.git
```

Restart ComfyUI. Both nodes appear under the `KSKNODES` category.

No Python package installation is required beyond ComfyUI itself.

### Optional companion plugins

For `KSK_PoseCostumeMatrixBatch`:

- `ComfyUI_Pose-Gallery_ZH`
- `ComfyUI_Costume-Gallery_ZH`

These are **not bundled**. Install separately and make sure gallery data is available. Missing galleries produce a clear error without affecting the old image-video node.

For `KSK_MatrixPairBatch`:

- `ComfyUI-VideoHelperSuite` for video loading.

### Models and API templates

Model weights (Qwen-Image 2.1, FLUX.3 Klein, VAEs, text encoders, PUP, etc.) are **not included**. Prepare them in your own ComfyUI models directory.

Matrix generation uses API workflow templates imported or captured in the node UI. Use templates matching your installed models and ComfyUI version.

---

## Quick start: pose × costume

1. Add **KSKNODES 姿势 × 服装矩阵批量** to the canvas.
2. Select pose items and costume items in the two gallery modals.
3. Choose cartesian/manual pair mode and samples per pair.
4. (Optional) connect a person reference image.
5. Click **预演** to inspect task count and seeds.
6. Bind Qwen/FLUX API templates for the models you use.
7. Click **确认批量执行**. This submits one independent ComfyUI queue task per image.
8. Use the job panel to pause, resume, cancel or retry failed tasks.

The canvas top **运行** button runs the current single graph prompt; use the node's **确认批量执行** for batch submission.

## HTTP API

The new node uses its own `/aiksk_pc/*` routes. The existing image-video node keeps `/ksk/matrix_pair/*`.

```text
GET  /aiksk_pc/status
GET  /aiksk_pc/catalogue?kind=pose|costume
POST /aiksk_pc/plan
POST /aiksk_pc/submit
GET  /aiksk_pc/jobs/{job_set_id}
POST /aiksk_pc/jobs/{job_set_id}/pause|resume|cancel|retry_failed
```

These routes are for the local ComfyUI instance; they do not replace ComfyUI's own access control. If exposing ComfyUI publicly, configure authentication at the instance/reverse-proxy level.

---

## Compatibility

- ComfyUI 0.42.x / 0.43.x (integration-tested in a real ComfyUI instance)
- Windows and Linux (runtime paths use `folder_paths`; no personal absolute paths)
- The old node remains available when the new node is installed
- No private server address, API key or token is required

## Tests

Verification status (2026-11-05):

- Static contract/privacy tests: **7/7 passed**
- Final source backend regression (R6 suite, local): **223 passed, 1 skipped**
- Real ComfyUI 0.43.0 integration: both `KSK_MatrixPairBatch` and `KSK_PoseCostumeMatrixBatch` registered; old and new HTTP routes responded; 2×2 plan returned 4 tasks; new node UI mounted in the browser
- GPU image generation: **NOT_TESTED in this integration pass** (the matrix node itself only schedules user-provided model workflows; model/GPU acceptance is separate)

The repository tests do not claim GPU image-generation acceptance.

## License

MIT. See `LICENSE`.

Third-party projects are referenced as optional dependencies only; their source and assets are not redistributed here. See `LICENSE_NOTICES.md`.

---

# 中文说明

本仓库包含两个节点：

- **KSKNODES 矩阵配对批量 (图 × 视频)**：原有节点，保留。
- **KSKNODES 姿势 × 服装矩阵批量**：新增节点。

两个节点可同时存在于同一画布，互不覆盖注册名、前端状态或 HTTP 路由。

## 安装

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/AI-KSK/KSK-Nodes.git
```

重启 ComfyUI。两个节点都在 `KSKNODES` 分类下。

## 姿势 × 服装矩阵

1. 在画布添加新节点；
2. 分别打开姿势库/服装库选择素材；
3. 选择组合模式、每组张数、Seed 和尺寸；
4. 可选连接人物参考图；
5. 先点「预演」确认任务数；
6. 绑定 Qwen-Image 2.1 / FLUX.3 Klein 的 API 模板；
7. 点节点内「确认批量执行」。每张图是一条独立 ComfyUI 队列任务；
8. 在任务面板暂停、恢复、取消或重试失败任务。

画布顶部「运行」只执行当前单张图；批量请使用节点内按钮。

## 依赖

- ComfyUI 0.42.x / 0.43.x
- 姿势库、服装库插件需另行安装，本仓库不附带其源码和图片素材
- `KSK_MatrixPairBatch` 的视频加载需要 ComfyUI-VideoHelperSuite
- 模型权重不在仓库内

## 许可

MIT。第三方项目仅作为可选依赖引用，不在此仓库再分发。
