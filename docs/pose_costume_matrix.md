# KSK Pose × Costume Matrix

This document describes the new `KSK_PoseCostumeMatrixBatch` node in more detail.

## Node contract

- Registration key: `KSK_PoseCostumeMatrixBatch`
- Display name: `KSKNODES 姿势 × 服装矩阵批量`
- Category: `KSKNODES / Pose Costume`
- Outputs:
  1. pose IMAGE
  2. costume IMAGE
  3. person IMAGE (1×1 black placeholder when no person input)
  4. pose info STRING/JSON
  5. costume info
  6. rendered prompt
  7. task JSON
  8. filename prefix

## Batch model

Each generated image is one independent ComfyUI queue task. The node does not
hide a `batch_size=N` inside a single workflow prompt. In-flight depth is
limited to 4; the backend fills the queue as tasks finish.

## Templates

The node can bind API workflow templates for Qwen-Image 2.1 and FLUX.3 Klein.
Templates must include this node and a final SaveImage/SaveImageAdvanced whose
upstream passes through a real generation branch. Templates that only save
reference images are rejected for production batch submission.

## Galleries

The node reads the public data of the community pose/costume gallery plugins if
present. Missing galleries are reported as a clear dependency error and do not
break the old image-video node.

## Privacy

The plugin contains no personal paths, credentials, user photos, model weights
or private server addresses. Runtime paths are obtained from ComfyUI
`folder_paths` and environment variables.
