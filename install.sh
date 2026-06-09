#!/usr/bin/env bash
set -euo pipefail

REPO="${BUNNYBYTE_REPO:-https://github.com/LISAooo1234/Bunny-Byte.git}"
INSTALL_DIR="${BUNNYBYTE_INSTALL_DIR:-$HOME/.bunnybyte-agent}"
BRANCH="${BUNNYBYTE_BRANCH:-main}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; RESET='\033[0m'
info()    { printf "${CYAN}[bunnybyte]${RESET} %s\n" "$*"; }
success() { printf "${GREEN}[bunnybyte]${RESET} ${BOLD}%s${RESET}\n" "$*"; }
warn()    { printf "${YELLOW}[bunnybyte]${RESET} %s\n" "$*" >&2; }
die()     { printf "${RED}[bunnybyte] ERROR:${RESET} %s\n" "$*" >&2; exit 1; }

find_python() {
    for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

main() {
    printf "\n${BOLD}╔══════════════════════════════════════════╗${RESET}\n"
    printf   "${BOLD}║        bunnybyte  one-line install      ║${RESET}\n"
    printf   "${BOLD}╚══════════════════════════════════════════╝${RESET}\n\n"

    command -v git &>/dev/null || die "找不到 git，请先安装。"

    PYTHON=$(find_python) || die "需要 Python 3.10 或以上版本。
  安装方法：sudo apt install python3.11   (Debian/Ubuntu)
            brew install python@3.11       (macOS)"

    PY_VER=$("$PYTHON" -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}.{v.micro}")')
    info "使用 Python ${PY_VER} (${PYTHON})"

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        info "更新已有安装 ${INSTALL_DIR} ..."
        git -C "$INSTALL_DIR" fetch --quiet origin
        git -C "$INSTALL_DIR" reset --hard "origin/${BRANCH}" --quiet
    else
        info "克隆 bunnybyte 到 ${INSTALL_DIR} ..."
        rm -rf "$INSTALL_DIR"
        git clone --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR" --quiet
    fi

    VENV_DIR="$INSTALL_DIR/.venv"
    if [[ ! -d "$VENV_DIR" ]]; then
        info "创建虚拟环境 ..."
        "$PYTHON" -m venv "$VENV_DIR"
    fi

    info "安装依赖 ..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -e "$INSTALL_DIR"

    BIN_DIR="${BUNNYBYTE_BIN_DIR:-$HOME/.local/bin}"
    mkdir -p "$BIN_DIR"
    BUNNY_LAUNCHER="$BIN_DIR/bunny"
    BUNNYBYTE_LAUNCHER="$BIN_DIR/bunnybyte"

    cat > "$BUNNY_LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/bunny" "\$@"
EOF
    chmod +x "$BUNNY_LAUNCHER"

    cat > "$BUNNYBYTE_LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/bunnybyte" "\$@"
EOF
    chmod +x "$BUNNYBYTE_LAUNCHER"

    printf "\n"
    success "bunnybyte 安装完成！"
    printf "\n"

    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
        warn "${BIN_DIR} 不在 PATH 里。"
        printf "  添加方法（任选一条）：\n\n"
        printf "    ${BOLD}echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc${RESET}\n"
        printf "    ${BOLD}echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc  && source ~/.zshrc${RESET}\n\n"
    else
        printf "  运行：${BOLD}bunny${RESET}\n\n"
    fi

    printf "  首次使用先做一次全局配置：\n"
    printf "    ${BOLD}bunny setup${RESET}\n\n"
    printf "  配置后在任意项目目录启动：\n"
    printf "    ${BOLD}bunny${RESET}\n\n"
    printf "  安装位置：${CYAN}${INSTALL_DIR}${RESET}\n"
    printf "  启动器：  ${CYAN}${BUNNY_LAUNCHER}${RESET}\n"
    printf "            ${CYAN}${BUNNYBYTE_LAUNCHER}${RESET}\n\n"
}

main "$@"
