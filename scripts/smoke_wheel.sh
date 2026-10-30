#!/usr/bin/env bash
# 装好的 wheel 在干净环境里能不能用：CI 的 package job 和 release.yml 跑的都是这一份。
# 用法：scripts/smoke_wheel.sh dist/bunnybyte_agent-*.whl
set -euo pipefail

wheel="$1"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

python -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --quiet "$wheel"
bunny="$work/venv/bin/bunny"

# 空 HOME、没有任何 key：模拟第一次安装的用户
export HOME="$work/home"
mkdir -p "$HOME" "$work/repo"
# key、base URL 和 BUNNYBYTE_* 都清掉，否则本机的网关配置会让检查变成警告并真的发请求
for name in $(env | grep -oE '^[A-Za-z_][A-Za-z0-9_]*' | grep -E '(API_KEY|AUTH_TOKEN|_BASE_URL|_API_BASE)|^BUNNYBYTE_' || true); do unset "$name"; done
git -C "$work/repo" init -q

expected="bunny $("$work/venv/bin/python" -c 'from bunnybyte.version import __version__; print(__version__)')"
actual="$("$bunny" --version)"
[[ "$actual" == "$expected" ]] || { echo "::error::--version printed '$actual', expected '$expected'"; exit 1; }
"$work/venv/bin/bunnybyte-tui" --version >/dev/null

(cd "$work/repo" && "$bunny" doctor --offline --json > "$work/doctor.json") || true
grep -q '"版本"' "$work/doctor.json" || { echo "::error::doctor has no version row"; cat "$work/doctor.json"; exit 1; }

set +e
(cd "$work/repo" && "$bunny" -p "hi" </dev/null 2> "$work/nokey.err")
code=$?
set -e
[[ $code -eq 2 ]] || { echo "::error::bunny -p without a key exited $code, expected 2"; cat "$work/nokey.err"; exit 1; }
grep -q "bunny setup" "$work/nokey.err" || { echo "::error::no setup hint"; cat "$work/nokey.err"; exit 1; }

echo "smoke ok: $actual"
