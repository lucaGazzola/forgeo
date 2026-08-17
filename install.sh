#!/bin/sh
#
# Install the `forgeo` CLI from the public GitHub remote.
#
#   curl -fsSL https://forgeo.org/install.sh | bash
#
# Prefers a prebuilt standalone binary downloaded from the matching GitHub
# Release for this OS/arch — no Python required. Falls back to pipx, then
# `pip install --user`, only when no prebuilt binary matches this platform
# and a Python >= 3.11 happens to be available. Never requires root.
# Re-running the script upgrades the existing install (re-downloads /
# pipx --force / pip --upgrade).
set -eu

REPO_OWNER="lucaGazzola"
REPO_NAME="forgeo"
DEFAULT_VERSION="0.7.0"
MIN_PYTHON="3.11"
PYPI_PACKAGE="forgeo-cli"
PREFIX="${FORGEO_PREFIX:-${HOME:-}/.local}"
BIN_DIR="$PREFIX/bin"

log() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

die() {
    warn "error: $*"
    exit 1
}

# Print the version (without the leading "v") of the latest GitHub release,
# or $DEFAULT_VERSION when the API is unreachable or rate-limited.
# Uses only shell parameter expansion (no sed/grep/head) so the script keeps
# working in minimal environments and in the hermetic test harness.
latest_version() {
    url="https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/latest"
    body=""
    if command -v curl >/dev/null 2>&1; then
        body="$(curl -fsSL "$url" 2>/dev/null || true)"
    elif command -v wget >/dev/null 2>&1; then
        body="$(wget -qO- "$url" 2>/dev/null || true)"
    fi
    tag="${body#*\"tag_name\"}"   # drop everything through the first "tag_name"
    tag="${tag#*\"}"              # drop through the opening quote of the value
    tag="${tag%%\"*}"             # keep only up to the closing quote
    case "$tag" in
        v[0-9]*.[0-9]*.[0-9]*)
            printf '%s\n' "${tag#v}"
            ;;
        *)
            warn "warning: could not determine the latest release from GitHub; falling back to v$DEFAULT_VERSION."
            printf '%s\n' "$DEFAULT_VERSION"
            ;;
    esac
}

# Print "<os>-<arch>" when a prebuilt binary is published for this platform,
# or nothing when it is not (the caller then falls back to Python).
detect_os_arch() {
    os="$(uname -s 2>/dev/null || true)"
    arch="$(uname -m 2>/dev/null || true)"
    case "$os" in
        Linux) os="linux" ;;
        Darwin) os="darwin" ;;
        MINGW*|MSYS*|CYGWIN*) os="windows" ;;
        *) return 1 ;;
    esac
    case "$arch" in
        x86_64|amd64) arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        i386|i686) arch="386" ;;
        *) return 1 ;;
    esac
    printf '%s-%s\n' "$os" "$arch"
}

# Print the name of the first interpreter that is Python >= 3.11.
find_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

# download <url> <dest>: fetch <url> into <dest> with curl or wget.
download() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        return 1
    fi
}

# Warn when $1 is not on PATH so the installed command would not be found.
warn_path_not_on_path() {
    case ":$PATH:" in
        *":$1:"*)
            ;;
        *)
            warn "warning: $1 is not on your PATH, so the 'forgeo' command will not be found."
            warn "Add it to your PATH (e.g. 'export PATH=\"\$PATH:$1\"' in ~/.profile) and open a new shell."
            ;;
    esac
}

install_binary() {
    os_arch="$1"
    suffix=""
    case "$os_arch" in
        windows-*) suffix=".exe" ;;
    esac
    url="$BASE_URL/forgeo-$os_arch$suffix"
    mkdir -p "$BIN_DIR"
    tmp="$BIN_DIR/.forgeo-$os_arch-$$"
    trap 'rm -f "$tmp"' EXIT HUP INT TERM
    log "Downloading forgeo v$VERSION ($os_arch)..."
    if ! download "$url" "$tmp"; then
        die "could not download $url. Check your network connection, or that release v$VERSION is published with prebuilt binaries."
    fi
    chmod 755 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$BIN_DIR/forgeo"
    trap - EXIT HUP INT TERM
    log "Installed 'forgeo' to $BIN_DIR/forgeo."
    warn_path_not_on_path "$BIN_DIR"
}

install_from_source() {
    if [ -n "$OS_ARCH" ]; then
        die "a prebuilt binary is available for $OS_ARCH, but neither curl nor wget was found on PATH. Install curl or wget and re-run."
    fi
    PYTHON="$(find_python)" || die "Python $MIN_PYTHON or newer is required but none was found on PATH, and no prebuilt binary is available for this platform. Install Python $MIN_PYTHON+ (https://www.python.org/downloads/ or your system package manager) and re-run."
    log "Using $PYTHON."

    if command -v pipx >/dev/null 2>&1; then
        log "Installing forgeo with pipx..."
        pipx install --force "$PYPI_PACKAGE"
    else
        warn "pipx not found; falling back to 'pip install --user'."
        user_base="$("$PYTHON" -m site --user-base 2>/dev/null || printf '%s/.local' "${HOME:-}")"
        warn_path_not_on_path "$user_base/bin"
        "$PYTHON" -m pip install --user --upgrade "$PYPI_PACKAGE"
    fi
}

OS_ARCH="$(detect_os_arch 2>/dev/null || true)"
VERSION="$(latest_version)"
BASE_URL="https://github.com/$REPO_OWNER/$REPO_NAME/releases/download/v$VERSION"

if [ -n "$OS_ARCH" ] && { command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; }; then
    install_binary "$OS_ARCH"
else
    install_from_source
fi

log ""
log "Done. The 'forgeo' CLI is installed. Next steps:"
log "  forgeo init"
log "  forgeo start"
