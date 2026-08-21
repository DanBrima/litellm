"""
Guards the artifact itself rather than the behaviour the patches add.

Run inside the built image, this is what proves the image really carries the
patched files, on every ``litellm`` package the image can import, and that the
shipped patches are still the ones the manifest describes.
"""

import hashlib
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((DEPLOY_DIR / "manifest.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(DEPLOY_DIR))
from apply_patches import litellm_package_roots, parse_patch  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("relative_path", sorted(MANIFEST["files"]))
def test_installed_file_is_the_patched_one(relative_path):
    roots = litellm_package_roots(())
    assert roots, "no litellm package found"

    for root in roots:
        target = root / relative_path
        assert target.is_file(), f"{target} is missing"
        assert _sha256(target) == MANIFEST["files"][relative_path]["after"], (
            f"{target} is not the patched file; the image was built from an unexpected base "
            f"or a later layer overwrote it"
        )


def test_the_installed_litellm_is_the_version_the_patches_target():
    version = importlib.metadata.version("litellm")
    assert version == MANIFEST["base_version"]


def test_patches_and_manifest_describe_the_same_files():
    patched_paths = {
        file_patch.path
        for patch_file in sorted((DEPLOY_DIR / "patches").glob("*.patch"))
        for file_patch in parse_patch(patch_file.read_text(encoding="utf-8"))
    }

    assert patched_paths == set(MANIFEST["files"])


def test_every_patch_target_is_reachable_from_the_import_path():
    """
    A patch aimed at a file no import can reach is a patch that does nothing.
    """
    for relative_path in MANIFEST["files"]:
        module_name = relative_path[: -len(".py")].replace("/", ".")
        assert importlib.util.find_spec(module_name) is not None, f"{module_name} is not importable"
