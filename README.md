# KSK Nodes

This repository currently contains only one ComfyUI custom node:

- `KSK_MatrixPairBatch` / `KSKNODES 矩阵配对批量 (图×视频)`

More KSK nodes will be added later after they are polished.

## Matrix Pair Batch

`KSK_MatrixPairBatch` pairs multiple reference images with multiple videos through a visual matrix UI.

Highlights:

- Matrix view: rows are images, columns are videos.
- Tree view: each image expands into its selected videos.
- One click queues each selected image-video pair as an independent ComfyUI task.
- Video output is compatible with `VHS_LoadVideo` from ComfyUI-VideoHelperSuite.
- Size mode can use manual width/height, reference image size, or original video size.
- The UI resizes with the ComfyUI node and scales the matrix cells to use available space.

## Requirements

- ComfyUI
- ComfyUI-VideoHelperSuite

## Install

Clone this repository into `ComfyUI/custom_nodes`:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/AI-KSK/KSK-Nodes.git
```

Restart ComfyUI after cloning.

## Notes

The upload and refresh buttons work with files in ComfyUI's `input` directory. Uploaded images and videos are also saved there.
