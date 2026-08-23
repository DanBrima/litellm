#!/usr/bin/env bash
#
# Build the patched LiteLLM image and write it out as a tar.
#
#   ./build.sh                        # build, verify, save dist/<image>.tar
#   IMAGE_TAG=my-registry/litellm:1.82.3-p1 ./build.sh
#   BASE_IMAGE=ghcr.io/berriai/litellm-non_root@sha256:... ./build.sh
#   PLATFORM=linux/arm64 ./build.sh   # build for a different architecture
#   SKIP_TESTS=1 ./build.sh           # skip the in-container test run
#
# The in-container test run installs pytest, so it needs network. Set
# SKIP_TESTS=1 for an air-gapped build; the Dockerfile's own SHA-256 and import
# checks still run either way.
#
# The default base is the non-root image, which runs as `nobody`. Override
# BASE_IMAGE and RUNTIME_USER together to build on a different variant
# (the root image is `ghcr.io/berriai/litellm:v1.82.3` with RUNTIME_USER=root).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_IMAGE="${BASE_IMAGE:-ghcr.io/berriai/litellm-non_root:main-v1.82.3}"
RUNTIME_USER="${RUNTIME_USER:-nobody}"
IMAGE_TAG="${IMAGE_TAG:-litellm-non_root:v1.82.3-team-rate-limits}"
OUT_DIR="${OUT_DIR:-${HERE}/dist}"
TAR_NAME="${TAR_NAME:-litellm-v1.82.3-team-rate-limits.tar}"
SKIP_TESTS="${SKIP_TESTS:-0}"
# The base image is a multi-arch manifest, so a builder picks its own
# architecture unless told otherwise. Building on an arm64 laptop for an amd64
# cluster produces an image whose every binary fails with "exec format error",
# which reads like missing files rather than a wrong architecture. Pin it.
PLATFORM="${PLATFORM:-linux/amd64}"

echo "==> building ${IMAGE_TAG}"
echo "    base:         ${BASE_IMAGE}"
echo "    runtime user: ${RUNTIME_USER}"
echo "    platform:     ${PLATFORM}"
docker build \
  --platform "${PLATFORM}" \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "RUNTIME_USER=${RUNTIME_USER}" \
  --tag "${IMAGE_TAG}" \
  "${HERE}"

echo "==> checking the image was built for ${PLATFORM}"
expected_arch="${PLATFORM##*/}"
actual_arch="$(docker image inspect "${IMAGE_TAG}" --format '{{.Architecture}}')"
if [ "${actual_arch}" != "${expected_arch}" ]; then
  echo "ERROR: image architecture is '${actual_arch}', expected '${expected_arch}'." >&2
  echo "       Running it on a ${expected_arch} host would fail with 'exec format error'." >&2
  exit 1
fi

echo "==> checking the image runs as ${RUNTIME_USER}"
actual_user="$(docker run --rm --entrypoint python3 "${IMAGE_TAG}" -c 'import getpass; print(getpass.getuser())')"
if [ "${actual_user}" != "${RUNTIME_USER}" ]; then
  echo "ERROR: image runs as '${actual_user}', expected '${RUNTIME_USER}'" >&2
  exit 1
fi

echo "==> checking the base image was not swapped underneath the patches"
docker run --rm --entrypoint python3 "${IMAGE_TAG}" \
  /opt/litellm-patches/apply_patches.py --dry-run

if [ "${SKIP_TESTS}" != "1" ]; then
  echo "==> running the patch test suite inside the image"
  # As root, because the runtime user cannot install into site-packages.
  docker run --rm --user root --entrypoint sh "${IMAGE_TAG}" -c '
    set -e
    python3 -m pip install --quiet --disable-pip-version-check pytest pytest-asyncio
    cd /opt/litellm-patches
    python3 -m pytest tests -q -p no:cacheprovider -o asyncio_mode=auto
  '
fi

echo "==> saving ${OUT_DIR}/${TAR_NAME}"
mkdir -p "${OUT_DIR}"
docker save "${IMAGE_TAG}" -o "${OUT_DIR}/${TAR_NAME}"
( cd "${OUT_DIR}" && sha256sum "${TAR_NAME}" > "${TAR_NAME}.sha256" )

echo
echo "image:  ${IMAGE_TAG}"
echo "tar:    ${OUT_DIR}/${TAR_NAME}"
echo "sha256: $(cut -d' ' -f1 "${OUT_DIR}/${TAR_NAME}.sha256")"
echo
echo "load it on the target host with:"
echo "  docker load -i ${TAR_NAME}"
