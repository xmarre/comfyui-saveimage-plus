from comfy.cli_args import args
import folder_paths
import json
import numpy
import os
import threading
from PIL import Image, ExifTags, ImageFile
from PIL.PngImagePlugin import PngInfo

# With optimize or progressive enabled, libjpeg emits the entire compressed
# stream at the end of encoding and cannot flush Pillow's output buffer midway.
# Pillow sizes that buffer at 1 byte per pixel (2 for quality >= 95), which
# high-entropy images (heavy grain or noise) can exceed, failing with
# "broken data stream when writing image file" ("encoder error -2" in older
# Pillow). Such a save is repeated with the same settings while
# ImageFile.MAXBLOCK, the lower bound Pillow applies to that buffer, is raised.
# MAXBLOCK is process-wide, so it is only raised when needed. The bound is
# about 2.5x the largest optimized or progressive output observed with
# libjpeg-turbo (binary noise, quality 100, 4:4:4); if the retry still fails,
# its error is raised.
_JPEG_MAX_BYTES_PER_PIXEL = 8
_JPEG_MARKER_HEADROOM = 65536
_jpeg_buffer_lock = threading.Lock()


def _save_jpeg(img, path, **kwargs):
    try:
        img.save(path, **kwargs)
        return
    except OSError:
        if not (kwargs.get("optimize") or kwargs.get("progressive")):
            raise
    # Pillow removed the partially written file. Dimensions are padded to
    # whole 16x16 MCUs, since libjpeg encodes the padding.
    width = -(-img.size[0] // 16) * 16
    height = -(-img.size[1] // 16) * 16
    bufsize = width * height * _JPEG_MAX_BYTES_PER_PIXEL + _JPEG_MARKER_HEADROOM
    with _jpeg_buffer_lock:
        previous = ImageFile.MAXBLOCK
        ImageFile.MAXBLOCK = max(previous, bufsize)
        try:
            img.save(path, **kwargs)
        finally:
            ImageFile.MAXBLOCK = previous


class SaveImagePlus:
    def __init__(self):
        pass

    FILE_TYPE_PNG = "PNG"
    FILE_TYPE_JPEG = "JPEG"
    FILE_TYPE_WEBP_LOSSLESS = "WEBP (lossless)"
    FILE_TYPE_WEBP_LOSSY = "WEBP (lossy)"
    # Chroma subsampling choices mapped to Pillow's JPEG "subsampling" values.
    JPEG_SUBSAMPLING = {
        "4:4:4": 0,
        "4:2:2": 1,
        "4:2:0": 2,
    }
    RETURN_TYPES = ()
    FUNCTION = "save_images"
    OUTPUT_NODE = True
    CATEGORY = "image"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE", ),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
                "file_type": ([s.FILE_TYPE_PNG, s.FILE_TYPE_JPEG, s.FILE_TYPE_WEBP_LOSSLESS, s.FILE_TYPE_WEBP_LOSSY], ),
                "remove_metadata": ("BOOLEAN", {"default": False}),
            },
            # Optional so that workflows and API prompts created before these
            # settings existed remain valid; omitted values use the defaults.
            "optional": {
                "jpeg_quality": ("INT", {"default": 90, "min": 1, "max": 100, "step": 1,
                    "tooltip": "JPEG quality. Higher values preserve more detail and produce larger files. 100 is not lossless."}),
                "jpeg_subsampling": (list(s.JPEG_SUBSAMPLING), {"default": "4:4:4",
                    "tooltip": "JPEG chroma subsampling. 4:4:4 keeps full color resolution; 4:2:2 halves it horizontally; 4:2:0 halves it horizontally and vertically."}),
                "jpeg_optimize": ("BOOLEAN", {"default": True,
                    "tooltip": "Compute optimal Huffman tables. Smaller files with identical pixels, at some extra encoding time and memory."}),
                "jpeg_progressive": ("BOOLEAN", {"default": False,
                    "tooltip": "Write a progressive JPEG, which renders in successive refinement passes while loading."}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    def save_images(self, images, filename_prefix="ComfyUI", file_type=FILE_TYPE_PNG, remove_metadata=False,
                    prompt=None, extra_pnginfo=None,
                    jpeg_quality=90, jpeg_subsampling="4:4:4", jpeg_optimize=True, jpeg_progressive=False):
        output_dir = folder_paths.get_output_directory()
        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, output_dir, images[0].shape[1], images[0].shape[0])
        extension = {
            self.FILE_TYPE_PNG: "png",
            self.FILE_TYPE_JPEG: "jpg",
            self.FILE_TYPE_WEBP_LOSSLESS: "webp",
            self.FILE_TYPE_WEBP_LOSSY: "webp",
        }.get(file_type, "png")
        if file_type == self.FILE_TYPE_JPEG:
            # Also checked here because linked inputs bypass ComfyUI's range checks.
            if jpeg_subsampling not in self.JPEG_SUBSAMPLING:
                raise ValueError(f"Unsupported JPEG chroma subsampling: {jpeg_subsampling!r}")
            if not 1 <= jpeg_quality <= 100:
                raise ValueError(f"JPEG quality must be between 1 and 100, got {jpeg_quality!r}")

        results = []
        for image in images:
            array = 255. * image.cpu().numpy()
            img = Image.fromarray(numpy.clip(array, 0, 255).astype(numpy.uint8))

            kwargs = dict()
            if extension == "png":
                kwargs["compress_level"] = 4
                if not remove_metadata and not args.disable_metadata:
                    metadata = PngInfo()
                    if prompt is not None:
                        metadata.add_text("prompt", json.dumps(prompt))
                    if extra_pnginfo is not None:
                        for x in extra_pnginfo:
                            metadata.add_text(x, json.dumps(extra_pnginfo[x]))
                    kwargs["pnginfo"] = metadata
            else:
                if file_type == self.FILE_TYPE_WEBP_LOSSLESS:
                    kwargs["lossless"] = True
                elif file_type == self.FILE_TYPE_JPEG:
                    kwargs["quality"] = jpeg_quality
                    kwargs["subsampling"] = self.JPEG_SUBSAMPLING[jpeg_subsampling]
                    kwargs["optimize"] = jpeg_optimize
                    kwargs["progressive"] = jpeg_progressive
                else:
                    kwargs["quality"] = 90
                if not remove_metadata and not args.disable_metadata:
                    metadata = {}
                    if prompt is not None:
                        metadata["prompt"] = prompt
                    if extra_pnginfo is not None:
                        metadata.update(extra_pnginfo)
                    exif = img.getexif()
                    exif[ExifTags.Base.UserComment] = json.dumps(metadata)
                    kwargs["exif"] = exif.tobytes()

            file = f"{filename}_{counter:05}_.{extension}"
            if file_type == self.FILE_TYPE_JPEG:
                _save_jpeg(img, os.path.join(full_output_folder, file), **kwargs)
            else:
                img.save(os.path.join(full_output_folder, file), **kwargs)
            results.append({
                "filename": file,
                "subfolder": subfolder,
                "type": "output",
            })
            counter += 1

        return { "ui": { "images": results } }

NODE_CLASS_MAPPINGS = {
    "SaveImagePlus": SaveImagePlus
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveImagePlus": "Save Image Plus"
}

WEB_DIRECTORY = "web"
