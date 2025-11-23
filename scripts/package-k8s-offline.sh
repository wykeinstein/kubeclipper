#!/usr/bin/env bash
# Build a Kubernetes offline package that matches KubeClipper expectations
# and optionally push it to the control plane via kcctl.
set -euo pipefail

NAME="k8s"
RESOURCE_TYPE="k8s"
ARCH=""
VERSION=""
IMAGES=""
CHARTS=""
CONFIGS_DIR=""
OUTPUT_DIR=""
WORKDIR=""
CACHE_PREFIX="/tmp/kc-downloader"
PUSH=false
KCCTL_BIN="kcctl"
KCCTL_CONFIG=""
PULL_TOOL="docker"
KUBEADM_BIN="kubeadm"
IMAGE_REPO="registry.k8s.io"
EXTRA_IMAGES=()

usage() {
  cat <<'USAGE'
Usage: package-k8s-offline.sh --version <vX.Y.Z> --arch <arch> [--images <images.tar.gz>] [options]

Required flags:
  --version <string>        Kubernetes version, e.g. v1.27.3
  --arch <string>           Target CPU architecture, e.g. amd64, arm64

Image source (choose one):
  --images <path>           Path to images.tar.gz that contains Kubernetes and dependency images
  # If --images is omitted, the script will pull required images from the internet,
  # save them as images.tar.gz, and include them in the package.

Optional flags:
  --name <string>           Resource name (default: k8s)
  --type <string>           Resource type for kcctl push (default: k8s)
  --charts <path>           Path to a charts archive to include (will be copied as charts.tgz)
  --configs-dir <path>      Directory whose contents are packed as configs.tar.gz and described in config/manifest.json
  --workdir <path>          Working directory for assembling the package (default: /tmp/kc-offline-build)
  --output-dir <path>       Directory to place the final tarball (default: current directory)
  --cache-prefix <path>     Prefix for manifest path entries (default: /tmp/kc-downloader)
  --kcctl-bin <path>        kcctl binary to use when --push is set (default: kcctl)
  --kcctl-config <path>     kcctl config file to use when --push is set
  --pull-tool <docker|ctr|nerdctl>  Tool for pulling/saving images when --images is omitted (default: docker)
  --kubeadm-bin <path>      kubeadm binary to list required images (default: kubeadm)
  --image-repo <string>     Image repository passed to kubeadm --image-repository (default: registry.k8s.io)
  --extra-image <ref>       Additional image reference to pull into images.tar.gz (can be repeated)
  --push                    Push the generated tarball with kcctl resource push
  -h, --help                Show this help text
USAGE
}

log() {
  printf '[package-k8s-offline] %s\n' "$*"
}

fail() {
  >&2 log "error: $*"
  exit 1
}

add_manifest_entry() {
  local name="$1" digest="$2" path="$3"
  manifest_entries+=("{\"name\":\"${name}\",\"digest\":\"${digest}\",\"path\":\"${path}\"}")
}

add_config_manifest_entry() {
  local name="$1" digest="$2" path="$3"
  config_entries+=("{\"name\":\"${name}\",\"digest\":\"${digest}\",\"path\":\"${path}\"}")
}

print_json_array() {
  local -n arr_ref=$1
  printf '[\n  %s\n]\n' "$(IFS=$'\n  '; echo "${arr_ref[*]}")"
}

ensure_pull_tool() {
  case "$PULL_TOOL" in
    docker|ctr|nerdctl) ;;
    *) fail "--pull-tool must be docker, ctr, or nerdctl" ;;
  esac
}

ensure_kubeadm() {
  if command -v "$KUBEADM_BIN" >/dev/null 2>&1; then
    return
  fi

  local os arch_dl kubeadm_url
  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  case "$ARCH" in
    amd64|arm64) arch_dl="$ARCH" ;;
    *) fail "unsupported arch for kubeadm download: $ARCH" ;;
  esac

  kubeadm_url="https://dl.k8s.io/release/${VERSION}/bin/${os}/${arch_dl}/kubeadm"
  mkdir -p "${WORKDIR}/.kubeadm"
  KUBEADM_BIN="${WORKDIR}/.kubeadm/kubeadm-${VERSION}-${arch_dl}"

  if [[ ! -x "$KUBEADM_BIN" ]]; then
    log "downloading kubeadm from ${kubeadm_url}"
    curl -fsSL -o "$KUBEADM_BIN" "$kubeadm_url" || fail "failed to download kubeadm"
    chmod +x "$KUBEADM_BIN"
  fi
}

build_image_list() {
  local kubeadm_args=(config images list --kubernetes-version "$VERSION")
  if [[ -n "$IMAGE_REPO" ]]; then
    kubeadm_args+=(--image-repository "$IMAGE_REPO")
  fi

  ensure_kubeadm
  mapfile -t kube_images < <("$KUBEADM_BIN" "${kubeadm_args[@]}")

  images_to_pull=("${kube_images[@]}" "${EXTRA_IMAGES[@]}")
  if [[ ${#images_to_pull[@]} -eq 0 ]]; then
    fail "no images returned by kubeadm, please provide --images manually"
  fi
}

pull_and_save_images() {
  ensure_pull_tool
  build_image_list

  log "pulling ${#images_to_pull[@]} images with ${PULL_TOOL}"

  case "$PULL_TOOL" in
    docker)
      for img in "${images_to_pull[@]}"; do
        docker pull --platform "linux/${ARCH}" "$img"
      done
      docker save "${images_to_pull[@]}" -o "${TARGET_DIR}/images.tar"
      ;;
    ctr)
      for img in "${images_to_pull[@]}"; do
        ctr -n k8s.io images pull --platform "linux/${ARCH}" "$img"
      done
      ctr -n k8s.io images export "${TARGET_DIR}/images.tar" "${images_to_pull[@]}"
      ;;
    nerdctl)
      for img in "${images_to_pull[@]}"; do
        nerdctl --namespace k8s.io pull --platform "linux/${ARCH}" "$img"
      done
      nerdctl --namespace k8s.io save -o "${TARGET_DIR}/images.tar" "${images_to_pull[@]}"
      ;;
  esac

  gzip -f "${TARGET_DIR}/images.tar"
  IMAGES="${TARGET_DIR}/images.tar.gz"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="$2"; shift 2 ;;
    --arch)
      ARCH="$2"; shift 2 ;;
    --images)
      IMAGES="$2"; shift 2 ;;
    --charts)
      CHARTS="$2"; shift 2 ;;
    --configs-dir)
      CONFIGS_DIR="$2"; shift 2 ;;
    --name)
      NAME="$2"; shift 2 ;;
    --type)
      RESOURCE_TYPE="$2"; shift 2 ;;
    --workdir)
      WORKDIR="$2"; shift 2 ;;
    --output-dir)
      OUTPUT_DIR="$2"; shift 2 ;;
    --cache-prefix)
      CACHE_PREFIX="$2"; shift 2 ;;
    --kcctl-bin)
      KCCTL_BIN="$2"; shift 2 ;;
    --kcctl-config)
      KCCTL_CONFIG="$2"; shift 2 ;;
    --pull-tool)
      PULL_TOOL="$2"; shift 2 ;;
    --kubeadm-bin)
      KUBEADM_BIN="$2"; shift 2 ;;
    --image-repo)
      IMAGE_REPO="$2"; shift 2 ;;
    --extra-image)
      EXTRA_IMAGES+=("$2"); shift 2 ;;
    --push)
      PUSH=true; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      fail "unknown flag: $1" ;;
  esac
done

[[ -z "$VERSION" ]] && fail "--version is required"
[[ -z "$ARCH" ]] && fail "--arch is required"

WORKDIR="${WORKDIR:-/tmp/kc-offline-build}"
OUTPUT_DIR="${OUTPUT_DIR:-$(pwd)}"

TARGET_DIR="${WORKDIR}/${NAME}/${VERSION}/${ARCH}"
CONFIG_DIR_IN_TARBALL="${TARGET_DIR}/config"
REMOTE_PATH="${CACHE_PREFIX}/.${NAME}/${VERSION}/${ARCH}"
PACKAGE_NAME="${NAME}-${VERSION}-${ARCH}.tar.gz"
PACKAGE_PATH="${OUTPUT_DIR%/}/${PACKAGE_NAME}"

log "assembling package in ${TARGET_DIR}"
mkdir -p "$TARGET_DIR"

if [[ -n "$IMAGES" ]]; then
  [[ -f "$IMAGES" ]] || fail "images file not found: $IMAGES"
  log "including existing images archive $IMAGES"
  cp "$IMAGES" "${TARGET_DIR}/images.tar.gz"
else
  pull_and_save_images
fi

if [[ -n "$CHARTS" ]]; then
  [[ -f "$CHARTS" ]] || fail "charts file not found: $CHARTS"
  log "including charts from $CHARTS"
  cp "$CHARTS" "${TARGET_DIR}/charts.tgz"
fi

if [[ -n "$CONFIGS_DIR" ]]; then
  [[ -d "$CONFIGS_DIR" ]] || fail "configs-dir is not a directory: $CONFIGS_DIR"
fi

manifest_entries=()
add_manifest_entry "images.tar.gz" "$(md5sum "${TARGET_DIR}/images.tar.gz" | awk '{print $1}')" "$REMOTE_PATH"

if [[ -n "$CHARTS" ]]; then
  add_manifest_entry "charts.tgz" "$(md5sum "${TARGET_DIR}/charts.tgz" | awk '{print $1}')" "$REMOTE_PATH"
fi

if [[ -n "$CONFIGS_DIR" ]]; then
  log "packing configs from $CONFIGS_DIR"
  mkdir -p "$CONFIG_DIR_IN_TARBALL"
  tar -C "$CONFIGS_DIR" -czf "${TARGET_DIR}/configs.tar.gz" .
  add_manifest_entry "configs.tar.gz" "$(md5sum "${TARGET_DIR}/configs.tar.gz" | awk '{print $1}')" "$REMOTE_PATH"

  config_entries=()
  while IFS= read -r -d '' file; do
    rel_path=${file#${CONFIGS_DIR%/}/}
    rel_path=${rel_path#./}
    digest=$(md5sum "$file" | awk '{print $1}')
    add_config_manifest_entry "$rel_path" "$digest" "/"
  done < <(find "$CONFIGS_DIR" -type f -print0 | sort -z)

  print_json_array config_entries > "${CONFIG_DIR_IN_TARBALL}/manifest.json"
fi

print_json_array manifest_entries > "${TARGET_DIR}/manifest.json"

log "creating tarball ${PACKAGE_PATH}"
mkdir -p "$OUTPUT_DIR"
tar -C "$WORKDIR" -czf "$PACKAGE_PATH" "$NAME"

if $PUSH; then
  log "pushing package with ${KCCTL_BIN}"
  push_cmd=("${KCCTL_BIN}" resource push --type "$RESOURCE_TYPE" --pkg "$PACKAGE_PATH")
  if [[ -n "$KCCTL_CONFIG" ]]; then
    push_cmd+=(--config "$KCCTL_CONFIG")
  fi
  "${push_cmd[@]}"
fi

log "package ready: ${PACKAGE_PATH}"
