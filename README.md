# Save Image Plus for ComfyUI

- This custom node is largely identical to the usual Save Image but allows saving images also in JPEG and WEBP formats, the latter with both lossless and lossy compression.
- Metadata is embedded in the images for loading workflows.
- An option to remove metadata is available and can be overridden from the node; otherwise, it defaults to the ComfyUI arguments.
- JPEG quality, chroma subsampling, Huffman optimization and progressive encoding are configurable.

![Screenshot 2024-05-30 at 10 06 54](https://github.com/Goktug/comfyui-saveimage-plus/assets/534426/d08bb984-911e-4a3c-a5cc-7a069cdc7005)


## Installation

To install, clone this repository into the `ComfyUI/custom_nodes` folder with:

```sh
git clone https://github.com/Goktug/comfyui-saveimage-plus
```

## JPEG settings

These settings apply only when `file_type` is `JPEG`, and the node shows them only for that format. Their values are kept when switching formats. PNG and WEBP output is not affected; lossy WEBP always uses quality 90.

| Setting | Default | Values |
|---|---|---|
| `jpeg_quality` | 90 | 1–100 |
| `jpeg_subsampling` | 4:4:4 | 4:4:4, 4:2:2, 4:2:0 |
| `jpeg_optimize` | enabled | enabled, disabled |
| `jpeg_progressive` | disabled | enabled, disabled |

The settings are optional inputs. Workflows and API prompts that do not specify them use the defaults above.

### Quality

`jpeg_quality` scales the quantization tables (libjpeg quality scale). Higher values keep more detail and produce larger files; the size grows quickly above about 90. Quality 100 is still lossy: the color conversion, chroma subsampling (unless 4:4:4) and rounding of DCT coefficients all discard information, so the decoded pixels differ from the original.

### Chroma subsampling

JPEG stores brightness (luma) and color (chroma) separately and can store chroma at reduced resolution:

- **4:4:4** keeps full chroma resolution. Fine colored detail, saturated edges, colored text and thin colored lines stay sharp.
- **4:2:2** halves chroma resolution horizontally.
- **4:2:0** halves chroma resolution horizontally and vertically. Files are smaller, and the loss is often hard to see in photographs, but it can blur or bleed saturated edges and small colored details.

At the same quality setting, 4:4:4 produces noticeably larger files than 4:2:0 (for example, roughly 25% larger at quality 90 on typical photographic images), with the extra data spent on color detail. When file size matters more than color detail, 4:2:0 at a higher quality can be a better trade-off than 4:4:4 at a lower one.

### Huffman optimization

`jpeg_optimize` computes Huffman tables for the specific image instead of using the standard tables. This only changes the lossless entropy coding: decoded pixels are identical, and files are typically a few percent smaller. The cost is a second pass over the image, roughly two to three times the encoding time of the unoptimized encoder, and buffering of the whole image's DCT coefficients during encoding (about 6 bytes per pixel with 4:4:4 and 3 bytes per pixel with 4:2:0).

### Progressive encoding

`jpeg_progressive` writes a progressive JPEG, which a viewer can display as a coarse preview that is refined as more of the file arrives. Pixels are the same as with sequential encoding. Progressive files are often slightly smaller, but take longer to encode and decode, and are always written with optimized Huffman tables regardless of `jpeg_optimize`. Sequential encoding is the default because the output is usually read from local disk.

### Recommended settings

- **Routine generation:** the defaults (quality 90, 4:4:4, optimization enabled, sequential), or quality 85–90 with 4:2:0 if smaller files are preferred.
- **High-fidelity final images:** quality 95 with 4:4:4. Consider PNG or lossless WEBP instead if the image will be edited further.

### Previous behavior

Before these settings existed, JPEG files were saved with quality 90, 4:2:0 subsampling, standard Huffman tables and sequential encoding. To reproduce that configuration exactly, set `jpeg_quality` to 90, `jpeg_subsampling` to 4:2:0, and disable `jpeg_optimize` and `jpeg_progressive`.

### When to use PNG or lossless WEBP

Use PNG or lossless WEBP when the image must be preserved exactly, for example for intermediate results that will be edited, upscaled or processed further, for masks and graphics with hard edges or flat colors, or for images that need transparency. Each JPEG save adds new compression loss, so repeatedly re-saving the same image as JPEG accumulates artifacts.

### Metadata limits

Workflow metadata in JPEG files is stored in a single EXIF segment, which the JPEG format limits to about 64 KB. Saving fails with "EXIF data is too long" if a workflow is larger than that; use PNG for such workflows or enable `remove_metadata`.
