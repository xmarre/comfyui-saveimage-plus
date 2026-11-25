"""Regression tests for SaveImagePlus encoding.

JPEG properties are verified by parsing the encoded files (SOF, DQT, DHT and
APP1 segments) and decoding them, not by inspecting arguments passed to Pillow.
"""
import inspect
import io
import json
import os
import struct

import numpy
import pytest
from PIL import ExifTags, Image, ImageFile
from PIL.PngImagePlugin import PngInfo

import save_image
from save_image import SaveImagePlus

JPEG_DEFAULTS = {
    "jpeg_quality": 90,
    "jpeg_subsampling": "4:4:4",
    "jpeg_optimize": True,
    "jpeg_progressive": False,
}
LEGACY_JPEG = {
    "jpeg_quality": 90,
    "jpeg_subsampling": "4:2:0",
    "jpeg_optimize": False,
    "jpeg_progressive": False,
}
PROMPT = {"3": {"class_type": "KSampler", "inputs": {"seed": 1, "text": "café"}}}
EXTRA_PNGINFO = {"workflow": {"version": 0.4, "nodes": [{"id": 3, "type": "KSampler"}], "links": []}}

# ITU-T T.81 Annex K tables, natural (row-major) order.
STD_LUMINANCE_QT = [
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
]
STD_CHROMINANCE_QT = [
    17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
]
ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
]
# Code-length counts of the Annex K luminance DC table, used by libjpeg when
# Huffman optimization is disabled.
STD_DC_LUMINANCE_BITS = bytes([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0])


class FakeTensor:
    """Minimal stand-in for a ComfyUI IMAGE tensor slice ([H, W, C] floats)."""

    def __init__(self, array):
        self.array = array
        self.shape = array.shape

    def cpu(self):
        return self

    def numpy(self):
        return self.array


def make_image(width=256, height=192, seed=0):
    """Photo-like test content: smooth color gradients, saturated edges and grain."""
    rng = numpy.random.default_rng(seed)
    y, x = numpy.mgrid[0:height, 0:width].astype(numpy.float32)
    r = 0.5 + 0.5 * numpy.sin(x / 17.0)
    g = 0.5 + 0.5 * numpy.cos(y / 23.0)
    b = (x + y) / (width + height)
    image = numpy.stack([r, g, b], axis=-1)
    image[height // 3:height // 2, width // 4:width // 2] = [0.9, 0.05, 0.1]
    image += rng.normal(0, 0.02, image.shape).astype(numpy.float32)
    return numpy.clip(image, 0, 1)


def make_batch(count=1, width=256, height=192):
    return [FakeTensor(make_image(width, height, seed)) for seed in range(count)]


def to_pil(tensor):
    array = 255. * tensor.cpu().numpy()
    return Image.fromarray(numpy.clip(array, 0, 255).astype(numpy.uint8))


def save(output_dir, file_type, images=None, **kwargs):
    kwargs.setdefault("prompt", PROMPT)
    kwargs.setdefault("extra_pnginfo", EXTRA_PNGINFO)
    result = SaveImagePlus().save_images(images or make_batch(), file_type=file_type, **kwargs)
    paths = [os.path.join(output_dir, r["subfolder"], r["filename"]) for r in result["ui"]["images"]]
    return result, paths


def jpeg_segments(data):
    """Return (marker, payload) for every marker segment, skipping entropy-coded data."""
    assert data[:2] == b"\xff\xd8"
    segments = []
    pos = 2
    while pos < len(data):
        assert data[pos] == 0xFF, f"expected marker at {pos}"
        marker = data[pos + 1]
        if marker == 0xD9:
            segments.append((marker, b""))
            break
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        segments.append((marker, data[pos + 4:pos + 2 + length]))
        pos += 2 + length
        if marker == 0xDA:
            # Skip scan data up to the next marker that is not stuffing or RSTn.
            while not (data[pos] == 0xFF and data[pos + 1] != 0 and not 0xD0 <= data[pos + 1] <= 0xD7):
                pos += 1
    return segments


def jpeg_info(path):
    with open(path, "rb") as f:
        data = f.read()
    segments = jpeg_segments(data)
    info = {"sof": None, "components": None, "qtables": {}, "dht": [], "scans": 0, "app1": []}
    for marker, payload in segments:
        if marker in (0xC0, 0xC1, 0xC2):
            info["sof"] = marker
            count = payload[5]
            info["components"] = [
                (payload[6 + 3 * i], payload[7 + 3 * i] >> 4, payload[7 + 3 * i] & 0x0F, payload[8 + 3 * i])
                for i in range(count)
            ]
        elif marker == 0xDB:
            pos = 0
            while pos < len(payload):
                precision, table_id = payload[pos] >> 4, payload[pos] & 0x0F
                assert precision == 0
                info["qtables"][table_id] = list(payload[pos + 1:pos + 65])
                pos += 65
        elif marker == 0xC4:
            pos = 0
            while pos < len(payload):
                table_class, table_id = payload[pos] >> 4, payload[pos] & 0x0F
                bits = payload[pos + 1:pos + 17]
                count = sum(bits)
                info["dht"].append((table_class, table_id, bytes(bits), bytes(payload[pos + 17:pos + 17 + count])))
                pos += 17 + count
        elif marker == 0xDA:
            info["scans"] += 1
        elif marker == 0xE1:
            info["app1"].append(payload)
    return info


def expected_qtable(std_table, quality):
    """libjpeg's jpeg_set_quality() scaling with force_baseline, in zigzag order."""
    scale = 5000 // quality if quality < 50 else 200 - quality * 2
    natural = [min(max((value * scale + 50) // 100, 1), 255) for value in std_table]
    return [natural[i] for i in ZIGZAG]


def sampling_factors(path):
    return [(h, v) for _, h, v, _ in jpeg_info(path)["components"]]


def decoded(path):
    with Image.open(path) as im:
        return numpy.asarray(im.convert("RGB"))


def exif_user_comment_ifd0(data):
    """Read UserComment from IFD0 the way web/load_workflow.js does (ASCII type only)."""
    little = data[:2] == b"II"
    order = "<" if little else ">"
    ifd = struct.unpack(order + "I", data[4:8])[0]
    entries = struct.unpack(order + "H", data[ifd:ifd + 2])[0]
    for i in range(entries):
        entry = ifd + 2 + 12 * i
        tag, typ, count, offset = struct.unpack(order + "HHII", data[entry:entry + 12])
        if tag == 0x9286 and typ == 2:
            # Values of up to four bytes are stored inline in the entry.
            start = entry + 8 if count <= 4 else offset
            return data[start:start + count - 1].decode("latin-1")
    return None


def jpeg_workflow_metadata(path):
    with open(path, "rb") as f:
        data = f.read()
    for payload in jpeg_info(path)["app1"]:
        if payload.startswith(b"Exif\x00\x00"):
            comment = exif_user_comment_ifd0(payload[6:])
            if comment is not None:
                return json.loads(comment)
    return None


# --- Node definition and compatibility ---------------------------------------


def test_input_types_keep_existing_required_inputs():
    inputs = SaveImagePlus.INPUT_TYPES()
    assert list(inputs["required"]) == ["images", "filename_prefix", "file_type", "remove_metadata"]
    assert inputs["required"]["file_type"][0] == ["PNG", "JPEG", "WEBP (lossless)", "WEBP (lossy)"]
    assert inputs["hidden"] == {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"}


def test_jpeg_inputs_are_optional_with_documented_defaults():
    optional = SaveImagePlus.INPUT_TYPES()["optional"]
    # New widgets are appended after the existing ones so positional
    # widgets_values of older workflows still map to the same widgets.
    assert list(optional) == ["jpeg_quality", "jpeg_subsampling", "jpeg_optimize", "jpeg_progressive"]
    quality_type, quality = optional["jpeg_quality"]
    assert quality_type == "INT"
    assert (quality["default"], quality["min"], quality["max"], quality["step"]) == (90, 1, 100, 1)
    assert optional["jpeg_subsampling"][0] == ["4:4:4", "4:2:2", "4:2:0"]
    assert optional["jpeg_subsampling"][1]["default"] == "4:4:4"
    assert optional["jpeg_optimize"] == ("BOOLEAN", optional["jpeg_optimize"][1])
    assert optional["jpeg_optimize"][1]["default"] is True
    assert optional["jpeg_progressive"][0] == "BOOLEAN"
    assert optional["jpeg_progressive"][1]["default"] is False


def test_function_defaults_match_input_defaults():
    # Executions that omit the optional inputs (older workflows and API
    # prompts) receive the Python defaults, which must match the widgets.
    parameters = inspect.signature(SaveImagePlus.save_images).parameters
    optional = SaveImagePlus.INPUT_TYPES()["optional"]
    for name, (_, options) in optional.items():
        assert parameters[name].default == options["default"], name


def test_existing_positional_arguments_keep_their_meaning(output_dir, cli_args):
    result = SaveImagePlus().save_images(make_batch(), "pos", "JPEG", False, PROMPT, EXTRA_PNGINFO)
    path = os.path.join(output_dir, result["ui"]["images"][0]["filename"])
    assert jpeg_workflow_metadata(path) == {"prompt": PROMPT, **EXTRA_PNGINFO}
    assert sampling_factors(path) == [(1, 1), (1, 1), (1, 1)]


def test_subsampling_mapping_matches_pillow_values():
    assert SaveImagePlus.JPEG_SUBSAMPLING == {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}


# --- JPEG encoder settings, verified on the encoded files --------------------


def test_jpeg_defaults(output_dir, cli_args):
    # No JPEG arguments, as for a workflow or API prompt predating them.
    _, (path,) = save(output_dir, "JPEG")
    info = jpeg_info(path)
    assert path.endswith(".jpg")
    assert info["sof"] == 0xC0, "expected baseline sequential JPEG"
    assert info["scans"] == 1
    assert sampling_factors(path) == [(1, 1), (1, 1), (1, 1)]
    assert info["qtables"][0] == expected_qtable(STD_LUMINANCE_QT, 90)
    assert info["qtables"][1] == expected_qtable(STD_CHROMINANCE_QT, 90)
    assert STD_DC_LUMINANCE_BITS not in [bits for _, _, bits, _ in info["dht"]], "expected optimized Huffman tables"


@pytest.mark.parametrize("quality", [1, 2, 50, 85, 90, 95, 99, 100])
def test_jpeg_quality_is_forwarded(output_dir, cli_args, quality):
    _, (path,) = save(output_dir, "JPEG", jpeg_quality=quality)
    info = jpeg_info(path)
    assert info["qtables"][0] == expected_qtable(STD_LUMINANCE_QT, quality)
    assert info["qtables"][1] == expected_qtable(STD_CHROMINANCE_QT, quality)


def test_jpeg_quality_100_is_not_lossless(output_dir, cli_args):
    images = make_batch()
    _, (path,) = save(output_dir, "JPEG", images=images, jpeg_quality=100)
    assert jpeg_info(path)["qtables"][0] == [1] * 64
    assert not numpy.array_equal(decoded(path), numpy.asarray(to_pil(images[0])))


def test_jpeg_quality_changes_file_size(output_dir, cli_args):
    sizes = []
    for quality in (50, 90, 100):
        _, (path,) = save(output_dir, "JPEG", jpeg_quality=quality, extra_pnginfo=None, prompt=None)
        sizes.append(os.path.getsize(path))
    assert sizes[0] < sizes[1] < sizes[2]


@pytest.mark.parametrize("mode, factors", [
    ("4:4:4", [(1, 1), (1, 1), (1, 1)]),
    ("4:2:2", [(2, 1), (1, 1), (1, 1)]),
    ("4:2:0", [(2, 2), (1, 1), (1, 1)]),
])
def test_jpeg_subsampling_modes(output_dir, cli_args, mode, factors):
    _, (path,) = save(output_dir, "JPEG", jpeg_subsampling=mode)
    assert sampling_factors(path) == factors


def test_jpeg_invalid_subsampling_raises(output_dir, cli_args):
    with pytest.raises(ValueError, match="subsampling"):
        save(output_dir, "JPEG", jpeg_subsampling="4:1:1")
    assert os.listdir(output_dir) == []


@pytest.mark.parametrize("quality", [0, -1, 101])
def test_jpeg_out_of_range_quality_raises(output_dir, cli_args, quality):
    # Values arriving through links are not range-checked by ComfyUI, and
    # Pillow would silently treat -1 as its default quality.
    with pytest.raises(ValueError, match="quality"):
        save(output_dir, "JPEG", jpeg_quality=quality)
    assert os.listdir(output_dir) == []


@pytest.mark.parametrize("subsampling", ["4:4:4", "4:2:0"])
def test_jpeg_optimize_changes_huffman_tables_only(output_dir, cli_args, subsampling):
    images = make_batch()
    _, (optimized,) = save(output_dir, "JPEG", images=images, jpeg_subsampling=subsampling, jpeg_optimize=True)
    _, (standard,) = save(output_dir, "JPEG", images=images, jpeg_subsampling=subsampling, jpeg_optimize=False)
    optimized_info, standard_info = jpeg_info(optimized), jpeg_info(standard)
    assert STD_DC_LUMINANCE_BITS in [bits for _, _, bits, _ in standard_info["dht"]]
    assert STD_DC_LUMINANCE_BITS not in [bits for _, _, bits, _ in optimized_info["dht"]]
    assert optimized_info["qtables"] == standard_info["qtables"]
    assert optimized_info["components"] == standard_info["components"]
    assert os.path.getsize(optimized) < os.path.getsize(standard)
    # Entropy coding is lossless: identical decoded pixels.
    assert numpy.array_equal(decoded(optimized), decoded(standard))


@pytest.mark.parametrize("optimize", [True, False])
def test_jpeg_progressive(output_dir, cli_args, optimize):
    images = make_batch()
    _, (progressive,) = save(output_dir, "JPEG", images=images, jpeg_progressive=True, jpeg_optimize=optimize)
    _, (sequential,) = save(output_dir, "JPEG", images=images, jpeg_progressive=False, jpeg_optimize=optimize)
    progressive_info, sequential_info = jpeg_info(progressive), jpeg_info(sequential)
    assert progressive_info["sof"] == 0xC2
    assert progressive_info["scans"] > 1
    assert sequential_info["sof"] == 0xC0
    assert sequential_info["scans"] == 1
    with Image.open(progressive) as im:
        assert im.info.get("progressive") == 1
    assert progressive_info["qtables"] == sequential_info["qtables"]
    assert numpy.array_equal(decoded(progressive), decoded(sequential))


def test_legacy_jpeg_configuration_is_reproducible(output_dir, cli_args):
    """The previous encoder (Pillow defaults with quality=90) is reproduced byte for byte."""
    images = make_batch()
    _, (path,) = save(output_dir, "JPEG", images=images, **LEGACY_JPEG)
    img = to_pil(images[0])
    exif = img.getexif()
    exif[ExifTags.Base.UserComment] = json.dumps({"prompt": PROMPT, **EXTRA_PNGINFO})
    reference = io.BytesIO()
    img.save(reference, "JPEG", quality=90, exif=exif.tobytes())
    with open(path, "rb") as f:
        assert f.read() == reference.getvalue()


def noise_batch(width, height, seed=0):
    return [FakeTensor(numpy.random.default_rng(seed).random((height, width, 3), dtype=numpy.float32))]


@pytest.fixture
def recorded_maxblock(monkeypatch):
    """Record ImageFile.MAXBLOCK at every Image.save call."""
    seen = []
    original = Image.Image.save

    def recording_save(self, *args, **kwargs):
        seen.append(ImageFile.MAXBLOCK)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", recording_save)
    return seen


# Pillow reserves one byte per pixel (two at quality >= 95) for optimized or
# progressive output. Uniform noise with 4:4:4 needs about 18 bits per pixel
# at quality 94 and about 24 at quality 100 (optimized), exceeding both.
@pytest.mark.parametrize("quality", [94, 100])
@pytest.mark.parametrize("optimize, progressive", [(True, False), (False, True), (True, True)])
def test_jpeg_high_entropy_image_does_not_overflow_encoder_buffer(output_dir, cli_args, recorded_maxblock,
                                                                  quality, optimize, progressive):
    maxblock = ImageFile.MAXBLOCK
    _, (path,) = save(output_dir, "JPEG", images=noise_batch(512, 512), jpeg_quality=quality,
                      jpeg_optimize=optimize, jpeg_progressive=progressive)
    # The first attempt overflowed and was repeated with a larger buffer.
    assert len(recorded_maxblock) == 2
    assert recorded_maxblock[0] == maxblock < recorded_maxblock[1]
    assert ImageFile.MAXBLOCK == maxblock
    assert os.listdir(output_dir) == [os.path.basename(path)]
    assert os.path.getsize(path) * 8 / (512 * 512) > (16 if quality >= 95 else 8)
    with Image.open(path) as im:
        im.load()
        assert im.size == (512, 512)
        assert bool(im.info.get("progressive")) == progressive
    assert jpeg_workflow_metadata(path)["prompt"] == PROMPT


def test_jpeg_thin_high_entropy_image(output_dir, cli_args, recorded_maxblock):
    # A one-pixel-high image is encoded as a full MCU strip; its output
    # exceeds Pillow's 64 KiB minimum buffer and is saved by the retry.
    _, (path,) = save(output_dir, "JPEG", images=noise_batch(30000, 1), jpeg_quality=100)
    assert len(recorded_maxblock) == 2
    assert os.path.getsize(path) > 65536
    with Image.open(path) as im:
        im.load()
        assert im.size == (30000, 1)


def test_jpeg_typical_save_does_not_touch_buffer_setting(output_dir, cli_args, recorded_maxblock):
    maxblock = ImageFile.MAXBLOCK
    save(output_dir, "JPEG", images=make_batch(2), jpeg_progressive=True)
    assert recorded_maxblock == [maxblock, maxblock]


@pytest.mark.parametrize("settings", [{}, {"jpeg_optimize": False, "jpeg_progressive": True}])
def test_jpeg_encoder_error_raised_after_one_retry(output_dir, cli_args, monkeypatch, settings):
    maxblock = ImageFile.MAXBLOCK
    seen = []

    def failing_save(self, *args, **kwargs):
        seen.append(ImageFile.MAXBLOCK)
        raise OSError(f"simulated encoder failure {len(seen)}")

    monkeypatch.setattr(Image.Image, "save", failing_save)
    with pytest.raises(OSError, match="failure 2"):
        save(output_dir, "JPEG", **settings)
    assert len(seen) == 2 and seen[0] == maxblock < seen[1]
    assert ImageFile.MAXBLOCK == maxblock


def test_jpeg_sequential_unoptimized_error_is_not_retried(output_dir, cli_args, monkeypatch):
    seen = []

    def failing_save(self, *args, **kwargs):
        seen.append(ImageFile.MAXBLOCK)
        raise OSError("simulated encoder failure")

    monkeypatch.setattr(Image.Image, "save", failing_save)
    with pytest.raises(OSError, match="simulated"):
        save(output_dir, "JPEG", **LEGACY_JPEG)
    assert len(seen) == 1


def test_jpeg_sequential_unoptimized_save_leaves_buffer_setting_alone(output_dir, cli_args, recorded_maxblock):
    maxblock = ImageFile.MAXBLOCK
    save(output_dir, "JPEG", images=noise_batch(512, 512), **{**LEGACY_JPEG, "jpeg_quality": 94})
    assert recorded_maxblock == [maxblock]


# --- Metadata -----------------------------------------------------------------


@pytest.mark.parametrize("settings", [{}, LEGACY_JPEG, {"jpeg_progressive": True, "jpeg_subsampling": "4:2:2"}])
def test_jpeg_metadata_is_recoverable(output_dir, cli_args, settings):
    _, (path,) = save(output_dir, "JPEG", **settings)
    metadata = jpeg_workflow_metadata(path)
    assert metadata == {"prompt": PROMPT, **EXTRA_PNGINFO}
    with Image.open(path) as im:
        assert json.loads(im.getexif()[ExifTags.Base.UserComment])["workflow"] == EXTRA_PNGINFO["workflow"]


def test_jpeg_remove_metadata(output_dir, cli_args):
    _, (path,) = save(output_dir, "JPEG", remove_metadata=True)
    assert jpeg_info(path)["app1"] == []
    assert jpeg_workflow_metadata(path) is None


def test_jpeg_global_disable_metadata(output_dir, cli_args, monkeypatch):
    monkeypatch.setattr(cli_args, "disable_metadata", True)
    _, (path,) = save(output_dir, "JPEG")
    assert jpeg_info(path)["app1"] == []


def test_jpeg_without_prompt_or_workflow(output_dir, cli_args):
    _, (path,) = save(output_dir, "JPEG", prompt=None, extra_pnginfo=None)
    assert jpeg_workflow_metadata(path) == {}


# --- Other formats are unaffected --------------------------------------------


def reference_bytes(file_type, image, remove_metadata=False):
    """Encode with the settings the node used before JPEG options existed."""
    img = to_pil(image)
    out = io.BytesIO()
    if file_type == "PNG":
        kwargs = {"compress_level": 4}
        if not remove_metadata:
            metadata = PngInfo()
            metadata.add_text("prompt", json.dumps(PROMPT))
            for key in EXTRA_PNGINFO:
                metadata.add_text(key, json.dumps(EXTRA_PNGINFO[key]))
            kwargs["pnginfo"] = metadata
        img.save(out, "PNG", **kwargs)
    else:
        kwargs = {"lossless": True} if file_type == "WEBP (lossless)" else {"quality": 90}
        if not remove_metadata:
            exif = img.getexif()
            exif[ExifTags.Base.UserComment] = json.dumps({"prompt": PROMPT, **EXTRA_PNGINFO})
            kwargs["exif"] = exif.tobytes()
        img.save(out, "WEBP", **kwargs)
    return out.getvalue()


NON_DEFAULT_JPEG = {
    "jpeg_quality": 10,
    "jpeg_subsampling": "4:2:0",
    "jpeg_optimize": False,
    "jpeg_progressive": True,
}


@pytest.mark.parametrize("file_type", ["PNG", "WEBP (lossless)", "WEBP (lossy)"])
@pytest.mark.parametrize("jpeg_settings", [{}, NON_DEFAULT_JPEG])
@pytest.mark.parametrize("remove_metadata", [False, True])
def test_non_jpeg_formats_unchanged(output_dir, cli_args, file_type, jpeg_settings, remove_metadata):
    images = make_batch()
    _, (path,) = save(output_dir, file_type, images=images, remove_metadata=remove_metadata, **jpeg_settings)
    with open(path, "rb") as f:
        assert f.read() == reference_bytes(file_type, images[0], remove_metadata)


def test_non_jpeg_formats_ignore_invalid_jpeg_settings(output_dir, cli_args):
    _, (path,) = save(output_dir, "WEBP (lossy)", jpeg_subsampling="bogus")
    assert path.endswith(".webp")


def test_png_metadata(output_dir, cli_args):
    _, (path,) = save(output_dir, "PNG")
    with Image.open(path) as im:
        assert json.loads(im.text["prompt"]) == PROMPT
        assert json.loads(im.text["workflow"]) == EXTRA_PNGINFO["workflow"]


# --- Batches, filenames and UI results ----------------------------------------


@pytest.mark.parametrize("file_type, extension", [
    ("PNG", "png"), ("JPEG", "jpg"), ("WEBP (lossless)", "webp"), ("WEBP (lossy)", "webp"),
])
def test_batch_filenames_and_results(output_dir, cli_args, file_type, extension):
    images = make_batch(3, width=200, height=120)
    result, paths = save(output_dir, file_type, images=images, filename_prefix="sub/run")
    assert result == {"ui": {"images": [
        {"filename": f"run_{i:05}_.{extension}", "subfolder": "sub", "type": "output"} for i in (1, 2, 3)
    ]}}
    for path, image in zip(paths, images):
        with Image.open(path) as im:
            assert im.size == (200, 120)
    # A second run continues the counter instead of overwriting.
    result, _ = save(output_dir, file_type, images=images[:1], filename_prefix="sub/run")
    assert result["ui"]["images"][0]["filename"] == f"run_00004_.{extension}"


def test_jpeg_batch_applies_settings_to_every_image(output_dir, cli_args):
    _, paths = save(output_dir, "JPEG", images=make_batch(3), jpeg_quality=75,
                    jpeg_subsampling="4:2:2", jpeg_progressive=True)
    for path in paths:
        info = jpeg_info(path)
        assert info["sof"] == 0xC2
        assert sampling_factors(path) == [(2, 1), (1, 1), (1, 1)]
        assert info["qtables"][0] == expected_qtable(STD_LUMINANCE_QT, 75)
        assert jpeg_workflow_metadata(path)["prompt"] == PROMPT


@pytest.mark.parametrize("width, height", [(1, 1), (17, 9), (3000, 1)])
def test_jpeg_odd_dimensions(output_dir, cli_args, width, height):
    image = numpy.random.default_rng(1).random((height, width, 3), dtype=numpy.float32)
    _, (path,) = save(output_dir, "JPEG", images=[FakeTensor(image)], jpeg_quality=100)
    with Image.open(path) as im:
        im.load()
        assert im.size == (width, height)


def test_module_exports():
    assert save_image.NODE_CLASS_MAPPINGS == {"SaveImagePlus": SaveImagePlus}
    assert save_image.NODE_DISPLAY_NAME_MAPPINGS == {"SaveImagePlus": "Save Image Plus"}
    assert save_image.WEB_DIRECTORY == "web"
