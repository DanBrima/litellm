#!/usr/bin/env bash
#
# Build the patched LiteLLM image and write it out as a tar.
#
#   ./build.sh                        # build, verify, save dist/<image>.tar
#   IMAGE_TAG=my-registry/litellm:1.82.3-p1 ./build.sh
#   BASE_IMAGE=ghcr.io/berriai/litellm@sha256:... ./build.sh
#   SKIP_TESTS=1 ./build.sh           # skip the in-container test run
#
# The in-container test run installs pytest, so it needs network. Set
# SKIP_TESTS=1 for an air-gapped build; the Dockerfile's own SHA-256 and import
# checks still run either way.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_IMAGE="${BASE_IMAGE:-ghcr.io/berriai/litellm:v1.82.3}"
IMAGE_TAG="${IMAGE_TAG:-litellm:v1.82.3-team-rate-limits}"
OUT_DIR="${OUT_DIR:-${HERE}/dist}"
TAR_NAME="${TAR_NAME:-litellm-v1.82.3-team-rate-limits.tar}"
SKIP_TESTS="${SKIP_TESTS:-0}"

echo "==> building ${IMAGE_TAG} from ${BASE_IMAGE}"
docker build \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  ${RUNTIME_USER:+--build-arg "RUNTIME_USER=${RUNTIME_USER}"} \
  --tag "${IMAGE_TAG}" \
  "${HERE}"

echo "==> checking the base image was not swapped underneath the patches"
docker run --rm --entrypoint python3 "${IMAGE_TAG}" \
  /opt/litellm-patches/apply_patches.py --dry-run

if [ "${SKIP_TESTS}" != "1" ]; then
  echo "==> running the patch test suite inside the image"
  docker run --rm --entrypoint sh "${IMAGE_TAG}" -c '
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
