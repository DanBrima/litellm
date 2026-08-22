#!/usr/bin/env bash
#
# Pack the patch set into a single tar you can carry to a machine that can
# reach a container registry, where ./build.sh turns it into the image.
#
#   ./make_bundle.sh                  # writes dist/<name>.tar.gz and a .sha256
#   OUT_DIR=/tmp ./make_bundle.sh
#
# The archive is byte-for-byte reproducible: same inputs, same checksum, so the
# .sha256 is worth comparing after any transfer.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-${HERE}/dist}"
BUNDLE_NAME="${BUNDLE_NAME:-litellm-v1.82.3-team-rate-limits}"
TARBALL="${OUT_DIR}/${BUNDLE_NAME}.tar.gz"

CONTENTS=(
  README.md
  PROOF.md
  Dockerfile
  .dockerignore
  build.sh
  make_bundle.sh
  apply_patches.py
  manifest.json
  patches
  tests
  upstream-test-patch
)

echo "==> checking the patches still describe exactly what the manifest lists"
python3 - "${HERE}" <<'PY'
import json
import sys
from pathlib import Path

here = Path(sys.argv[1])
sys.path.insert(0, str(here))
from apply_patches import parse_patch  # noqa: E402

manifest = json.loads((here / "manifest.json").read_text(encoding="utf-8"))
patched = {
    file_patch.path
    for patch_file in sorted((here / "patches").glob("*.patch"))
    for file_patch in parse_patch(patch_file.read_text(encoding="utf-8"))
}
if patched != set(manifest["files"]):
    raise SystemExit(f"patches touch {sorted(patched)} but the manifest lists {sorted(manifest['files'])}")
print(f"    {len(patched)} files, base version {manifest['base_version']}")
PY

echo "==> packing ${TARBALL}"
mkdir -p "${OUT_DIR}"
rm -f "${TARBALL}"
# --sort, a fixed mtime and numeric root ownership keep the archive, and so its
# checksum, identical across machines and runs.
tar --create \
    --file - \
    --directory "${HERE}" \
    --transform "s,^,${BUNDLE_NAME}/," \
    --sort=name \
    --mtime='UTC 2020-01-01' \
    --owner=0 --group=0 --numeric-owner \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "${CONTENTS[@]}" \
  | gzip --no-name > "${TARBALL}"

( cd "${OUT_DIR}" && sha256sum "$(basename "${TARBALL}")" > "$(basename "${TARBALL}").sha256" )

echo "==> verifying the archive unpacks to what went in"
verify_dir="$(mktemp -d)"
trap 'rm -rf "${verify_dir}"' EXIT
tar --extract --file "${TARBALL}" --directory "${verify_dir}"
missing=0
for entry in "${CONTENTS[@]}"; do
  if [ ! -e "${verify_dir}/${BUNDLE_NAME}/${entry}" ]; then
    echo "ERROR: ${entry} missing from the archive" >&2
    missing=1
  fi
done
[ "${missing}" -eq 0 ] || exit 1
diff -r -x '__pycache__' -x '*.pyc' -x 'dist' "${verify_dir}/${BUNDLE_NAME}/patches" "${HERE}/patches"
diff -r -x '__pycache__' -x '*.pyc' "${verify_dir}/${BUNDLE_NAME}/tests" "${HERE}/tests"

echo
echo "bundle: ${TARBALL}"
echo "sha256: $(cut -d' ' -f1 "${TARBALL}.sha256")"
echo "size:   $(du -h "${TARBALL}" | cut -f1)"
echo
echo "on a host that can pull ghcr.io:"
echo "  tar xzf $(basename "${TARBALL}")"
echo "  cd ${BUNDLE_NAME}"
echo "  ./build.sh"
