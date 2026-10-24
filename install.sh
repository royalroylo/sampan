#!/bin/sh
# sampan installer for macOS and Linux.
#   curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --yes --version v0.7.2
# Read it first if you like: download it, open it, then run `sh install.sh`.
# Options: --yes (assume yes to every question, and run `sampan init --yes` so init takes its defaults)  --no-init (do not run `sampan init`)  --version vX.Y.Z  --help
# Testing overrides (env): SAMPAN_INSTALL_SPEC (exact spec for `uv tool install`, e.g. a local path),
#   SAMPAN_INSTALL_TTY (file to read answers from instead of /dev/tty).
# Everything lives in main(), called on the last line, so a truncated download runs nothing.
set -eu

REPO_URL="https://github.com/royalroylo/sampan"
FALLBACK_VERSION="v0.7.2"

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
die() { warn "error: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'USAGE'
Usage: install.sh [--yes] [--no-init] [--version vX.Y.Z]

Installs sampan (package sampan) with uv, after offering to install what is missing
(uv, tmux; on Linux also ps, lsof, git). Then runs `sampan init` if Claude Code is installed.

  --yes             assume yes to every question; `sampan init` also takes its defaults (`sampan init --yes`)
  --no-init         do not run `sampan init`
  --version vX.Y.Z  install this tag (default: the latest v* tag, or v0.7.2 if it cannot be found)
USAGE
}

# ask "question": 0 = yes, 1 = no. Answers come from the terminal (stdin is the pipe under `curl | sh`);
# with no terminal the answer is no.
ask() {
    if [ "$ASSUME_YES" = 1 ]; then
        say "$1 [y/N] y (--yes)"
        return 0
    fi
    if [ "$HAVE_TTY" != 1 ]; then
        say "$1 [y/N] n (no terminal to ask on)"
        return 1
    fi
    printf '%s [y/N] ' "$1"
    ans=""
    read -r ans <"$TTY" || ans=""
    case "$ans" in y | Y | yes | YES | Yes) return 0 ;; *) return 1 ;; esac
}

detect_tty() {
    TTY="${SAMPAN_INSTALL_TTY:-/dev/tty}"
    HAVE_TTY=0
    if ( : <"$TTY" ) 2>/dev/null; then HAVE_TTY=1; fi
}

# macOS: brew is often installed but not on PATH (a non-login shell, or `curl | sh` over ssh): probe the usual places.
find_brew() {
    have brew && return 0
    for d in /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$d/brew" ]; then PATH="$d:$PATH"; export PATH; return 0; fi
    done
    return 0
}

# Package manager for Linux: sets PM and SUDO.
detect_pm() {
    PM=""
    if have apt-get; then PM=apt
    elif have dnf; then PM=dnf
    elif have pacman; then PM=pacman
    elif have zypper; then PM=zypper
    fi
    SUDO=""
    if [ "$(id -u)" != 0 ]; then
        if have sudo; then SUDO="sudo"; else SUDO="__nosudo__"; fi
    fi
}

# pkg_install "tmux" "procps" ...: install the given package names (already mapped for $PM).
pkg_install() {
    if [ "$SUDO" = "__nosudo__" ]; then
        warn "Not root and sudo is missing. Install these as root, then run this again: $*"
        return 1
    fi
    case "$PM" in
        apt) $SUDO apt-get update && $SUDO apt-get install -y "$@" ;;
        dnf) $SUDO dnf install -y "$@" ;;
        pacman) $SUDO pacman -S --needed --noconfirm "$@" ;;
        zypper) $SUDO zypper --non-interactive install "$@" ;;
        *) return 1 ;;
    esac
}

# Package name of a tool for $PM.
pkg_name() {
    case "$1" in
        ps) case "$PM" in dnf | pacman) echo procps-ng ;; *) echo procps ;; esac ;;
        *) echo "$1" ;;
    esac
}

ensure_system_tools() {
    if [ "$OS" = Darwin ]; then
        # git comes with the Xcode command line tools; without them /usr/bin/git is a stub that opens a dialog.
        if [ -z "${SAMPAN_INSTALL_SPEC:-}" ] && ! xcode-select -p >/dev/null 2>&1; then
            die "the Xcode command line tools (git) are missing. Run 'xcode-select --install', wait for it to finish, then run this again."
        fi
    fi
    missing=""
    have tmux || missing="$missing tmux"
    if [ "$OS" = Linux ]; then
        have ps || missing="$missing ps"
        have lsof || [ -x /usr/sbin/lsof ] || [ -x /usr/bin/lsof ] || missing="$missing lsof"
        have git || missing="$missing git"
    fi
    [ -n "$missing" ] || return 0
    say "Missing:$missing"
    if [ "$OS" = Darwin ]; then
        # Only tmux can be missing here (ps and lsof come with macOS).
        find_brew
        if have brew; then
            ask "Run 'brew install tmux'?" || die "tmux is required. Install it ('brew install tmux') and run this again."
            brew install tmux
        else
            warn "tmux is needed, and Homebrew is not installed. Install Homebrew first (it asks for your password):"
            # shellcheck disable=SC2016  # the $(...) is for the user's shell, not ours
            warn '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            warn "then run this installer again."
            exit 1
        fi
        return 0
    fi
    detect_pm
    pkgs=""
    for t in $missing; do pkgs="$pkgs $(pkg_name "$t")"; done
    if [ -z "$PM" ]; then
        die "no supported package manager (apt, dnf, pacman, zypper) found. Install these yourself:$pkgs, then run this again."
    fi
    sudo_note=""
    [ -n "$SUDO" ] && sudo_note=" with sudo"
    # shellcheck disable=SC2086  # $pkgs is a deliberate word list
    ask "Install$pkgs with $PM$sudo_note?" || die "Needed:$pkgs. Install them ($PM) and run this again."
    # shellcheck disable=SC2086
    pkg_install $pkgs || die "package install failed"
    have tmux || die "tmux is still missing after the install"
}

ensure_uv() {
    if have uv; then UV=uv; return 0; fi
    if [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; return 0; fi
    say "uv (the Python tool installer) is missing."
    ask "Install uv with its official installer (curl -LsSf https://astral.sh/uv/install.sh | sh)?" ||
        die "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
    have curl || die "curl is required to install uv"
    # INSTALLER_NO_MODIFY_PATH: uv will not edit your shell startup files; the PATH line is printed at the end.
    curl -LsSf https://astral.sh/uv/install.sh | INSTALLER_NO_MODIFY_PATH=1 sh
    [ -x "$HOME/.local/bin/uv" ] || die "uv was not found at $HOME/.local/bin/uv after its installer ran"
    UV="$HOME/.local/bin/uv"
}

latest_version() {
    if have git; then
        # GIT_TERMINAL_PROMPT=0: never stop to ask for credentials (the repo may not be reachable).
        tag=$(GIT_TERMINAL_PROMPT=0 git ls-remote --tags --refs "$REPO_URL" 2>/dev/null |
            sed 's|.*refs/tags/v||' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1) || tag=""
        [ -z "$tag" ] || tag="v$tag"
        if [ -n "$tag" ]; then echo "$tag"; return 0; fi
    fi
    return 0  # prints nothing when the lookup failed; the caller then uses FALLBACK_VERSION
}

path_hint() {
    # ORIG_PATH: the PATH this installer was started with (what the user's own shell has), not our adjusted one.
    case ":$ORIG_PATH:" in *":$BIN_DIR:"*) return 0 ;; esac
    say ""
    say "$BIN_DIR is not on your PATH. Add it (then open a new terminal):"
    case "${SHELL:-}" in
        */fish) say "  fish_add_path $BIN_DIR" ;;
        */zsh) say "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc" ;;
        */bash) say "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc" ;;
        *) say "  export PATH=\"$BIN_DIR:\$PATH\"   # put this in your shell's startup file" ;;
    esac
}

main() {
    ORIG_PATH="$PATH"
    ASSUME_YES=0
    DO_INIT=1
    VERSION=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes | -y) ASSUME_YES=1 ;;
            --no-init) DO_INIT=0 ;;
            --version)
                [ $# -ge 2 ] || die "--version needs a value, e.g. --version v0.7.2"
                VERSION="$2"
                shift
                ;;
            --version=*) VERSION="${1#--version=}" ;;
            -h | --help) usage; exit 0 ;;
            *) usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done

    case "$(uname -s)" in
        Darwin) OS=Darwin ;;
        Linux) OS=Linux ;;
        *) die "unsupported system $(uname -s): sampan runs on macOS and Linux" ;;
    esac
    detect_tty

    ensure_system_tools
    ensure_uv

    case "$VERSION" in "" | v*) ;; *) VERSION="v$VERSION" ;; esac  # --version 0.7.2 means v0.7.2
    if [ -z "$VERSION" ]; then
        VERSION=$(latest_version)
        if [ -z "$VERSION" ]; then
            VERSION="$FALLBACK_VERSION"
        fi
    fi
    SPEC="${SAMPAN_INSTALL_SPEC:-git+$REPO_URL@$VERSION}"

    say "Installing sampan ($SPEC)..."
    # --reinstall (not --force): replaces sampan over sampan, but never overwrites some other program called sampan.
    "$UV" tool install --reinstall "$SPEC" || die "installing $SPEC failed"

    BIN_DIR=$("$UV" tool dir --bin 2>/dev/null) || BIN_DIR="$HOME/.local/bin"
    [ -x "$BIN_DIR/sampan" ] || die "sampan was installed but is not in $BIN_DIR"

    skip_init=""
    if [ "$DO_INIT" != 1 ]; then
        skip_init="--no-init was given"
    elif ! (PATH="$ORIG_PATH"; have claude); then
        skip_init="Claude Code (claude) is not installed"
        say ""
        say "Claude Code is not installed. Install it, run 'claude' once and log in:"
        say "  curl -fsSL https://claude.ai/install.sh | bash"
    elif [ "$HAVE_TTY" != 1 ]; then
        skip_init="there is no terminal to ask questions on"
    fi

    if [ -z "$skip_init" ]; then
        say ""
        init_args="init"
        if [ "$ASSUME_YES" = 1 ]; then
            init_args="init --yes"
            say "Running 'sampan init --yes' (every question takes its default)..."
        else
            say "Running 'sampan init' (it asks a few questions)..."
        fi
        # The original PATH plus the absolute binary: init's own PATH hint then fires when BIN_DIR is not on the user's PATH.
        # $init_args is unquoted on purpose (two words at most). A failing init must not abort the installer under set -e.
        init_rc=0
        # shellcheck disable=SC2086
        PATH="$ORIG_PATH" "$BIN_DIR/sampan" $init_args <"$TTY" || init_rc=$?
        if [ "$init_rc" != 0 ]; then
            say "sampan init did not finish (exit $init_rc). Run 'sampan init' yourself in a terminal."
        fi
    else
        say ""
        say "Skipped 'sampan init': $skip_init. Run 'sampan init' yourself when ready."
    fi

    path_hint
    say ""
    "$BIN_DIR/sampan" --version
}

main "$@"
