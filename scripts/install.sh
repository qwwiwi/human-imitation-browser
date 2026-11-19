#!/usr/bin/env bash
# Install everything human-imitation-browser needs on Ubuntu 22.04 / 24.04.
#
# Usage (from the repository folder):
#   bash scripts/install.sh            # full install; asks for the sudo password for system packages
#   bash scripts/install.sh --no-system  # only the Python part, without apt / sudo
#
# What it does:
#   1. installs python3-venv and Xvfb with apt (needs sudo rights);
#   2. creates the .venv folder and installs camoufox[geoip] into it;
#   3. downloads the Camoufox browser files;
#   4. installs the browser's system libraries (GTK 3 and others) with apt, from an explicit package list;
#   5. creates .env from .env.example if there is none.
# Without sudo rights it prints the exact commands to run instead.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_DIR}/.venv"
VENV_PY="${VENV_DIR}/bin/python"
REQ_FILE="${REPO_DIR}/requirements.txt"
ENV_FILE="${REPO_DIR}/.env"
ENV_EXAMPLE="${REPO_DIR}/.env.example"
SYSTEM_STEPS=1
MANUAL_STEPS=()

log() { echo "[install] $*"; }
fail() { echo "[install] ERROR: $*" >&2; exit 1; }

for arg in "$@"; do
  case "${arg}" in
    --no-system) SYSTEM_STEPS=0 ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) fail "unknown option: ${arg} (see --help)" ;;
  esac
done

# Decide how to run commands as root: already root, sudo, or not possible.
SUDO=""
CAN_ROOT=0
if [[ "${SYSTEM_STEPS}" -eq 1 ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    CAN_ROOT=1
  elif command -v sudo >/dev/null 2>&1; then
    log "system packages need administrator rights; sudo may ask for your password"
    if sudo -v; then
      SUDO="sudo"
      CAN_ROOT=1
    fi
  fi
fi

log "step 1/5: python3 and system packages"
command -v python3 >/dev/null 2>&1 || fail "python3 not found; on Ubuntu: sudo apt-get install -y python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10 or newer is needed, found $(python3 --version 2>&1)"
[[ -f "${REQ_FILE}" ]] || fail "requirements.txt not found in ${REPO_DIR}"

APT_PACKAGES=(python3-venv xvfb)
# System libraries of Firefox / Camoufox, following the list Playwright documents for Firefox
# on Ubuntu. UNVERIFIED: written from memory of that list, not tested on a clean machine.
# On Ubuntu 24.04 some of these have a "t64" twin (libgtk-3-0t64); browser_package picks it.
BROWSER_LIBS=(
  libasound2 libatk1.0-0 libcairo-gobject2 libcairo2 libdbus-glib-1-2 libfontconfig1 libfreetype6
  libgdk-pixbuf-2.0-0 libglib2.0-0 libgtk-3-0 libpango-1.0-0 libpangocairo-1.0-0 libx11-6 libx11-xcb1
  libxcb-shm0 libxcb1 libxcomposite1 libxcursor1 libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2
  libxrender1 libxtst6 fonts-liberation
)

# Print the t64 name of a package when this system has it, otherwise the plain name.
browser_package() {
  if apt-cache show "$1t64" >/dev/null 2>&1; then echo "$1t64"; else echo "$1"; fi
}
if [[ "${CAN_ROOT}" -eq 1 ]]; then
  if command -v apt-get >/dev/null 2>&1; then
    log "installing with apt: ${APT_PACKAGES[*]}"
    ${SUDO} apt-get update
    ${SUDO} apt-get install -y "${APT_PACKAGES[@]}"
  else
    log "apt-get not found: this is not Ubuntu/Debian, install python3-venv and Xvfb yourself"
  fi
else
  log "skipping apt (no administrator rights or --no-system)"
  MANUAL_STEPS+=("sudo apt-get update && sudo apt-get install -y ${APT_PACKAGES[*]}")
fi

log "step 2/5: Python environment in ${VENV_DIR}"
if [[ -x "${VENV_PY}" ]] && "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
  log ".venv already exists, reusing it"
else
  if [[ -d "${VENV_DIR}" ]]; then
    log "found a broken .venv (no pip inside), creating it again"
    python3 -m venv --clear "${VENV_DIR}" \
      || fail "could not create .venv; on Ubuntu install python3-venv: sudo apt-get install -y python3-venv"
  else
    python3 -m venv "${VENV_DIR}" \
      || fail "could not create .venv; on Ubuntu install python3-venv: sudo apt-get install -y python3-venv"
  fi
fi
"${VENV_PY}" -m pip install --upgrade pip
"${VENV_PY}" -m pip install -r "${REQ_FILE}"

log "step 3/5: downloading the Camoufox browser (a large download, takes a while)"
"${VENV_PY}" -m camoufox fetch

log "step 4/5: system libraries for the browser"
# Administrator rights are used only for apt-get with a fixed package list; the Python
# from the project's own .venv (writable by the user) never runs as root.
BROWSER_PACKAGES=()
if command -v apt-cache >/dev/null 2>&1; then
  for lib in "${BROWSER_LIBS[@]}"; do
    BROWSER_PACKAGES+=("$(browser_package "${lib}")")
  done
fi
if [[ "${CAN_ROOT}" -eq 1 ]] && [[ "${#BROWSER_PACKAGES[@]}" -gt 0 ]]; then
  log "installing with apt: ${BROWSER_PACKAGES[*]}"
  ${SUDO} apt-get install -y "${BROWSER_PACKAGES[@]}"
elif [[ "${CAN_ROOT}" -eq 1 ]]; then
  log "apt-get not found: install the Firefox system libraries (GTK 3 and others) for your system yourself"
else
  log "skipping system libraries (no administrator rights, --no-system or no apt)"
  if [[ "${#BROWSER_PACKAGES[@]}" -gt 0 ]]; then
    MANUAL_STEPS+=("sudo apt-get install -y ${BROWSER_PACKAGES[*]}")
  else
    MANUAL_STEPS+=("install the Firefox system libraries (GTK 3 and others) with your system's package manager")
  fi
fi
if ! command -v Xvfb >/dev/null 2>&1; then
  log "warning: Xvfb (virtual screen) is not installed yet; the browser will not start without it"
fi

log "step 5/5: settings file"
if [[ -f "${ENV_FILE}" ]]; then
  log ".env already exists, leaving it as is"
elif [[ -f "${ENV_EXAMPLE}" ]]; then
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  log "created .env from .env.example (only you can read it); put your token there"
else
  log "warning: .env.example not found, create .env yourself"
fi

if [[ "${#MANUAL_STEPS[@]}" -gt 0 ]]; then
  log "almost done. Run these commands yourself (they need administrator rights):"
  for cmd in "${MANUAL_STEPS[@]}"; do
    echo "    ${cmd}"
  done
  log "then run this script again or go on with the check."
fi

log "done. Next:"
echo "    nano ${ENV_FILE}        # put MOBILEPROXY_API_TOKEN=..."
echo "    ${VENV_PY} ${REPO_DIR}/scripts/check_proxy.py"
echo "    ${VENV_PY} ${REPO_DIR}/scripts/preflight.py     # check the address and the profile before the first real site"
echo "    ${VENV_PY} ${REPO_DIR}/scripts/open_page.py https://www.ozon.ru/"
