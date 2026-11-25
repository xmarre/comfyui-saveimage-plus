"""Stub the ComfyUI modules imported by save_image so it can be tested standalone."""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if "comfy.cli_args" not in sys.modules:
    cli_args = types.ModuleType("comfy.cli_args")
    cli_args.args = types.SimpleNamespace(disable_metadata=False)
    comfy = sys.modules.setdefault("comfy", types.ModuleType("comfy"))
    comfy.cli_args = cli_args
    sys.modules["comfy.cli_args"] = cli_args

if "folder_paths" not in sys.modules:
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.output_directory = None

    def get_output_directory():
        return folder_paths.output_directory

    def get_save_image_path(filename_prefix, output_dir, image_width=0, image_height=0):
        # Simplified version of ComfyUI's helper: next free counter for the prefix.
        subfolder = os.path.dirname(os.path.normpath(filename_prefix))
        filename = os.path.basename(os.path.normpath(filename_prefix))
        full_output_folder = os.path.join(output_dir, subfolder)
        os.makedirs(full_output_folder, exist_ok=True)
        counters = [
            int(f[len(filename) + 1:].split("_")[0])
            for f in os.listdir(full_output_folder)
            if f.startswith(filename + "_") and f[len(filename) + 1:].split("_")[0].isdigit()
        ]
        counter = max(counters, default=0) + 1
        return full_output_folder, filename, counter, subfolder, filename_prefix

    folder_paths.get_output_directory = get_output_directory
    folder_paths.get_save_image_path = get_save_image_path
    sys.modules["folder_paths"] = folder_paths


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    import folder_paths
    monkeypatch.setattr(folder_paths, "output_directory", str(tmp_path))
    return tmp_path


@pytest.fixture
def cli_args(monkeypatch):
    from comfy.cli_args import args
    monkeypatch.setattr(args, "disable_metadata", False)
    return args
