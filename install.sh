#!/usr/bin/env bash
# VX — AI Video Editor installer
# Usage: curl -fsSL https://raw.githubusercontent.com/MaxCSHan/ai-video-editor/main/install.sh | bash
#
# What this does:
#   1. Checks for Python 3.11+ (installs via system package manager if missing)
#   2. Checks for ffmpeg (installs via system package manager if missing)
#   3. Installs uv (fast Python package manager) if not present
#   4. Clones the repository (or updates if already cloned)
#   5. Creates a virtual environment and installs VX
#   6. Runs the first-time setup wizard (API keys, language)

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors and helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

info()  { echo -e "  ${GREEN}✓${NC} $1"; }
warn()  { echo -e "  ${YELLOW}!${NC} $1"; }
fail()  { echo -e "  ${RED}✗${NC} $1"; }
step()  { echo -e "\n  ${BOLD}$1${NC}"; }

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
detect_os() {
    case "$(uname -s)" in
        Darwin*)  echo "macos" ;;
        Linux*)   echo "linux" ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        *)        echo "unknown" ;;
    esac
}

detect_linux_pkg_manager() {
    if command -v apt-get &>/dev/null; then echo "apt"
    elif command -v dnf &>/dev/null; then echo "dnf"
    elif command -v pacman &>/dev/null; then echo "pacman"
    elif command -v zypper &>/dev/null; then echo "zypper"
    else echo "unknown"
    fi
}

OS=$(detect_os)

echo -e "\n${BOLD}  VX — AI Video Editor Installer${NC}"
echo -e "${DIM}  Setting up your environment...${NC}\n"

# ---------------------------------------------------------------------------
# Step 1: Python 3.11+
# ---------------------------------------------------------------------------
step "Checking Python..."

PYTHON_CMD=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_CMD="$cmd"
            info "Python $version ($cmd)"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    warn "Python 3.11+ not found. Installing..."
    case "$OS" in
        macos)
            if command -v brew &>/dev/null; then
                brew install python@3.12
                PYTHON_CMD="python3.12"
            else
                fail "Homebrew not found. Install Python 3.11+ from https://www.python.org/downloads/"
                exit 1
            fi
            ;;
        linux)
            PKG=$(detect_linux_pkg_manager)
            case "$PKG" in
                apt)
                    sudo apt-get update -qq
                    sudo apt-get install -y -qq python3.12 python3.12-venv python3-pip
                    PYTHON_CMD="python3.12"
                    ;;
                dnf)
                    sudo dnf install -y python3.12
                    PYTHON_CMD="python3.12"
                    ;;
                pacman)
                    sudo pacman -S --noconfirm python
                    PYTHON_CMD="python3"
                    ;;
                *)
                    fail "Could not detect package manager. Install Python 3.11+ manually."
                    exit 1
                    ;;
            esac
            ;;
        *)
            fail "Unsupported OS. Install Python 3.11+ from https://www.python.org/downloads/"
            exit 1
            ;;
    esac
    info "Python installed: $($PYTHON_CMD --version)"
fi

# ---------------------------------------------------------------------------
# Step 2: ffmpeg
# ---------------------------------------------------------------------------
step "Checking ffmpeg..."

if command -v ffmpeg &>/dev/null; then
    ffmpeg_version=$(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}' | cut -d- -f1)
    info "ffmpeg $ffmpeg_version"
else
    warn "ffmpeg not found. Installing..."
    case "$OS" in
        macos)
            if command -v brew &>/dev/null; then
                brew install ffmpeg
            else
                fail "Install ffmpeg: https://ffmpeg.org/download.html"
                exit 1
            fi
            ;;
        linux)
            PKG=$(detect_linux_pkg_manager)
            case "$PKG" in
                apt)    sudo apt-get install -y -qq ffmpeg ;;
                dnf)    sudo dnf install -y ffmpeg ;;
                pacman) sudo pacman -S --noconfirm ffmpeg ;;
                *)
                    fail "Install ffmpeg manually: https://ffmpeg.org/download.html"
                    exit 1
                    ;;
            esac
            ;;
        *)
            fail "Install ffmpeg manually: https://ffmpeg.org/download.html"
            exit 1
            ;;
    esac
    info "ffmpeg installed: $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
fi

# ---------------------------------------------------------------------------
# Step 3: uv (fast Python package manager)
# ---------------------------------------------------------------------------
step "Checking uv..."

if command -v uv &>/dev/null; then
    uv_version=$(uv --version 2>/dev/null | awk '{print $2}')
    info "uv $uv_version"
else
    warn "uv not found. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Add to PATH for this session
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if command -v uv &>/dev/null; then
        info "uv installed: $(uv --version | awk '{print $2}')"
    else
        fail "uv installation failed. Install manually: https://docs.astral.sh/uv/"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Step 4: Clone or update repository
# ---------------------------------------------------------------------------
step "Setting up VX..."

VX_DIR="${VX_INSTALL_DIR:-$HOME/ai-video-editor}"

if [ -d "$VX_DIR/.git" ]; then
    info "Repository found at $VX_DIR"
    cd "$VX_DIR"
    git pull --ff-only origin main 2>/dev/null || warn "Could not update (offline or diverged)"
else
    if [ -d "$VX_DIR" ]; then
        warn "$VX_DIR exists but is not a git repo. Using existing directory."
        cd "$VX_DIR"
    else
        info "Cloning to $VX_DIR..."
        git clone https://github.com/MaxCSHan/ai-video-editor.git "$VX_DIR"
        cd "$VX_DIR"
    fi
fi

# ---------------------------------------------------------------------------
# Step 5: Virtual environment + install
# ---------------------------------------------------------------------------
step "Installing dependencies..."

if [ ! -d ".venv" ]; then
    uv venv --python "$PYTHON_CMD"
    info "Virtual environment created"
else
    info "Virtual environment exists"
fi

uv pip install -e . --quiet
info "VX installed"

# Check if vx is on PATH
VX_BIN=".venv/bin/vx"
if [ -f "$VX_BIN" ]; then
    info "VX binary: $VX_DIR/$VX_BIN"
else
    warn "VX binary not found at expected path"
fi

# ---------------------------------------------------------------------------
# Step 6: Optional extras
# ---------------------------------------------------------------------------
echo ""
echo -e "  ${DIM}Optional: local transcription (no API cost, macOS Apple Silicon only)${NC}"
read -p "  Install mlx-whisper? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    uv pip install -e ".[whisper]" --quiet
    info "mlx-whisper installed"
fi

# ---------------------------------------------------------------------------
# Step 7: Run setup wizard
# ---------------------------------------------------------------------------
step "Running first-time setup..."
echo ""

# Activate venv and run setup
source .venv/bin/activate
vx setup

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "  ${GREEN}${BOLD}Installation complete!${NC}"
echo ""
echo -e "  ${BOLD}Quick start:${NC}"
echo -e "    cd $VX_DIR"
echo -e "    source .venv/bin/activate"
echo -e "    vx                            ${DIM}# Interactive mode${NC}"
echo -e "    vx new my-trip ~/footage/     ${DIM}# Create project from clips${NC}"
echo ""
echo -e "  ${DIM}Add to your shell profile for convenience:${NC}"
echo -e "    echo 'alias vx=\"$VX_DIR/.venv/bin/vx\"' >> ~/.zshrc"
echo ""
