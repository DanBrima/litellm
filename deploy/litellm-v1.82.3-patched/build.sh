#!/usr/bin/env bash
#
# Build the patched LiteLLM image and write it out as a tar.
#
# BASE_IMAGE is required. Point it at the LiteLLM image you already run, so the
# result keeps everything that image carries (your CA tooling, your certs, your
# entrypoint) and differs from it only by the three patches in patches/.
#
#   BASE_IMAGE=registry.example/litellm-non-root:v1.82.3 \
#   IMAGE_TAG=registry.example/litellm-non-root:v1.82.3-team-rate-limits \
#     ./build.sh
#
# Other knobs, all environment variables:
#
#   RUNTIME_USER   user the finished image runs as (default nobody)
#   PLATFORM       architecture to build for (default linux/amd64)
#   CONTAINER_CLI  docker or podman (auto-detected)
#   OUT_DIR        where the tar goes (default ./dist)
#   TAR_NAME       name of the tar
#   SKIP_TESTS=1   do not run the test suite inside the image
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_USER="${RUNTIME_USER:-nobody}"
IMAGE_TAG="${IMAGE_TAG:-litellm-non-root:v1.82.3-team-rate-limits}"
OUT_DIR="${OUT_DIR:-${HERE}/dist}"
TAR_NAME="${TAR_NAME:-litellm-v1.82.3-team-rate-limits.tar}"
SKIP_TESTS="${SKIP_TESTS:-0}"
# The public base is a multi-arch manifest, so a builder picks its own
# architecture unless told otherwise. Building on an arm64 machine for an amd64
# cluster produces an image whose every binary fails with "exec format error",
# which reads like missing files rather than a wrong architecture. Pin it.
PLATFORM="${PLATFORM:-linux/amd64}"

if [ -z "${BASE_IMAGE:-}" ]; then
  cat >&2 <<'USAGE'
ERROR: BASE_IMAGE is not set.

Set it to the LiteLLM 1.82.3 image you actually run, from your own registry, so
the patched image inherits everything that one already has:

  BASE_IMAGE=registry.example/litellm-non-root:v1.82.3 \
  IMAGE_TAG=registry.example/litellm-non-root:v1.82.3-team-rate-limits \
    ./build.sh

Building on the public ghcr.io image instead would drop anything your own image
adds on top of it, which is how you end up missing update-ca-certificates.
USAGE
  exit 2
fi

if [ -z "${CONTAINER_CLI:-}" ]; then
  if command -v docker >/dev/null 2>&1; then
    CONTAINER_CLI=docker
  elif command -v podman >/dev/null 2>&1; then
    CONTAINER_CLI=podman
  else
    echo "ERROR: neither docker nor podman found. Set CONTAINER_CLI." >&2
    exit 2
  fi
fi

echo "==> building ${IMAGE_TAG}"
echo "    base:         ${BASE_IMAGE}"
echo "    runtime user: ${RUNTIME_USER}"
echo "    platform:     ${PLATFORM}"
echo "    builder:      ${CONTAINER_CLI}"
"${CONTAINER_CLI}" build \
  --platform "${PLATFORM}" \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "RUNTIME_USER=${RUNTIME_USER}" \
  --tag "${IMAGE_TAG}" \
  "${HERE}"

echo "==> checking the image was built for ${PLATFORM}"
expected_arch="${PLATFORM##*/}"
actual_arch="$("${CONTAINER_CLI}" image inspect "${IMAGE_TAG}" --format '{{.Architecture}}')"
if [ "${actual_arch}" != "${expected_arch}" ]; then
  echo "ERROR: image architecture is '${actual_arch}', expected '${expected_arch}'." >&2
  echo "       Running it on a ${expected_arch} host would fail with 'exec format error'." >&2
  exit 1
fi

echo "==> checking the image runs as ${RUNTIME_USER}"
actual_user="$("${CONTAINER_CLI}" run --rm --entrypoint python3 "${IMAGE_TAG}" -c 'import getpass; print(getpass.getuser())')"
if [ "${actual_user}" != "${RUNTIME_USER}" ]; then
  echo "ERROR: image runs as '${actual_user}', expected '${RUNTIME_USER}'" >&2
  exit 1
fi

echo "==> checking the base image was not swapped underneath the patches"
"${CONTAINER_CLI}" run --rm --entrypoint python3 "${IMAGE_TAG}" \
  /opt/litellm-patches/apply_patches.py --dry-run

echo "==> checking what the base image carried through"
"${CONTAINER_CLI}" run --rm --entrypoint sh "${IMAGE_TAG}" -c '
  for path in /usr/bin/update-ca-certificates /usr/sbin/update-ca-certificates; do
    [ -e "$path" ] && echo "    present: $path"
  done
  echo "    arch:    $(uname -m)"
' || true

if [ "${SKIP_TESTS}" != "1" ]; then
  echo "==> running the patch test suite inside the image"
  # As root, because the runtime user cannot install into site-packages. On an
  # air-gapped builder pytest cannot be fetched, so this reports a loud skip
  # rather than failing a build whose own SHA-256 and import checks passed.
  if ! "${CONTAINER_CLI}" run --rm --user root --entrypoint sh "${IMAGE_TAG}" -c '
        set -e
        python3 -c "import pytest" 2>/dev/null \
          || python3 -m pip install --quiet --disable-pip-version-check pytest pytest-asyncio
        cd /opt/litellm-patches
        python3 -m pytest tests -q -p no:cacheprovider -o asyncio_mode=auto
      '; then
    echo
    echo "!!  TESTS DID NOT RUN OR DID NOT PASS." >&2
    echo "!!  If this builder has no network, pytest could not be installed and the" >&2
    echo "!!  suite was never executed. Re-run with SKIP_TESTS=1 to make that" >&2
    echo "!!  explicit, or run the suite on a machine that can install pytest." >&2
    echo "!!  A genuine test failure means do not ship this image." >&2
    echo
    exit 1
  fi
else
  echo "==> skipping the in-container test suite (SKIP_TESTS=1)"
fi

echo "==> saving ${OUT_DIR}/${TAR_NAME}"
mkdir -p "${OUT_DIR}"
"${CONTAINER_CLI}" save "${IMAGE_TAG}" -o "${OUT_DIR}/${TAR_NAME}"
( cd "${OUT_DIR}" && sha256sum "${TAR_NAME}" > "${TAR_NAME}.sha256" )

echo
echo "image:  ${IMAGE_TAG}"
echo "tar:    ${OUT_DIR}/${TAR_NAME}"
echo "sha256: $(cut -d' ' -f1 "${OUT_DIR}/${TAR_NAME}.sha256")"
echo
echo "push it straight to your registry with:"
echo "  ${CONTAINER_CLI} push ${IMAGE_TAG}"
echo "or move the tar and load it with:"
echo "  ${CONTAINER_CLI} load -i ${TAR_NAME}"
