#!/usr/bin/env bash
# Run once on Jetson over SSH. Installs SDK packages, never OS/JetPack upgrades.
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/jetson_profile.sh"

system_install() (
  jetson_platform
  jetson_sdk_check >/dev/null 2>&1 && { echo 'System prerequisites already ready.'; exit 0; }
  for tool in sudo curl gpg; do
    command -v "$tool" >/dev/null || { echo "Install prerequisite $tool first" >&2; exit 1; }
  done
  local available
  available="$(df -Pk / | awk 'NR==2 {print $4}')"
  (( available >= 786432 )) || { echo 'Need at least 768 MiB free on root before SDK install; resolved package space is checked below.' >&2; exit 1; }
  mkdir -p "$VIVI_DATA/cache/system" "$VIVI_DATA/cache/apt/partial"
  local work="$VIVI_DATA/cache/system" original_cuda original_cuda11
  original_cuda="$(readlink -f /usr/local/cuda || true)"
  original_cuda11="$(readlink -f /usr/local/cuda-11 || true)"
  # Package maintainer scripts may select the new toolkit globally; restore
  # the original alternative even if installation is interrupted.
  restore_cuda() {
    if [[ -n "$original_cuda" && -d "$original_cuda" ]] && \
       [[ "$(readlink -f /usr/local/cuda || true)" != "$original_cuda" ]]; then
      sudo update-alternatives --set cuda "$original_cuda"
    fi
    if [[ -n "$original_cuda11" && -d "$original_cuda11" ]] && \
       [[ "$(readlink -f /usr/local/cuda-11 || true)" != "$original_cuda11" ]]; then
      sudo update-alternatives --set cuda-11 "$original_cuda11"
    fi
  }
  sudo -v
  trap restore_cuda EXIT
  curl --fail --location --retry 2 --max-time 60 \
    https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/arm64/3bf863cc.pub \
    -o "$work/cuda-key.pub"
  mkdir -p "$work/gnupg"
  chmod 700 "$work/gnupg"
  local fingerprint
  fingerprint="$(gpg --homedir "$work/gnupg" --show-keys --with-colons "$work/cuda-key.pub" | awk -F: '$1=="fpr" {print $10; exit}')"
  [[ "$fingerprint" == EB693B3035CD5710E231E123A4B469963BF863CC ]] \
    || { echo 'NVIDIA signing key fingerprint mismatch' >&2; exit 1; }
  gpg --homedir "$work/gnupg" --batch --yes --dearmor --output "$work/cuda-key.gpg" "$work/cuda-key.pub"
  # Use the existing official repository if one is already configured to
  # avoid duplicate APT sources with conflicting Signed-By settings.
  if ! grep -qs '^[[:space:]]*deb .*developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/arm64' \
       /etc/apt/sources.list /etc/apt/sources.list.d/*.list; then
    sudo install -m 644 "$work/cuda-key.gpg" /usr/share/keyrings/vivi-cuda.gpg
    printf '%s\n' 'deb [arch=arm64 signed-by=/usr/share/keyrings/vivi-cuda.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/arm64/ /' \
      | sudo tee /etc/apt/sources.list.d/vivi-cuda.list >/dev/null
  fi
  sudo apt-get update
  local packages=(gcc-10=10.5.0-1ubuntu1~20.04 g++-10=10.5.0-1ubuntu1~20.04 \
    build-essential git curl openssl libportaudio2 \
    cuda-nvcc-11-8=11.8.89-1 cuda-nvrtc-dev-11-8=11.8.89-1 \
    libcublas-dev-11-8=11.11.3.6-1 libcurand-dev-11-8=10.3.0.86-1 \
    libcufft-dev-11-8=10.9.0.58-1 libcusparse-dev-11-8=11.7.5.86-1 \
    cuda-compat-11-8=11.8.31339915-1)
  # Show APT's plan and reject removals before making package changes.
  sudo apt-get --simulate --no-install-recommends install "${packages[@]}" > "$work/apt-plan.txt"
  cat "$work/apt-plan.txt"
  if grep -Eq '^Remv |^Inst (nvidia-l4t-|linux-image-|linux-headers-)' "$work/apt-plan.txt"; then
    echo 'APT would remove packages or change BSP/kernel; stop and inspect apt-plan.txt' >&2; exit 1
  fi
  # Evaluate the resolved package sizes, with 768 MiB headroom for the root.
  /usr/bin/python3 - "${packages[@]}" <<'PY'
import apt, shutil, sys
cache = apt.Cache()
for spec in sys.argv[1:]:
    name, _, version = spec.partition('=')
    package = cache[name]
    if version:
        package.candidate = package.versions[version]
    package.mark_install(auto_fix=True, auto_inst=True, from_user=True)
required = max(0, cache.required_space) + 768 * 1024**2
free = shutil.disk_usage('/').free
print('APT disk requirement including headroom: %.2f GiB; free: %.2f GiB' % (required / 1024**3, free / 1024**3))
if required > free:
    sys.exit('Not enough root space for resolved packages; do not continue')
PY
  sudo apt-get -y --no-install-recommends \
    -o "Dir::Cache::archives=$VIVI_DATA/cache/apt" install "${packages[@]}"
  restore_cuda
  trap - EXIT
  jetson_sdk_check
  echo 'System prerequisites ready. OS/JetPack and default CUDA selection preserved.'
  echo 'Next: bash scripts/jetson_ai.sh setup --profile agx'
)

case "${1:-check}" in
  check) jetson_platform; jetson_sdk_check; echo 'System prerequisites ready.' ;;
  install) system_install ;;
  *) echo 'Usage: bash scripts/jetson_system.sh {check|install}' >&2; exit 2 ;;
esac
