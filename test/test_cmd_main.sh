#!/usr/bin/env bash
# 校验 cmd/main 的架构选择与 trim-cli 预检逻辑，不需要飞牛环境。
# 覆盖曾出事的两类问题：函数被误删（bash -n 查不出）、架构选错导致用错二进制。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
MAIN="$HERE/../cmd/main"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAIL=0

pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }

# --- 抽取被测函数（源码直接 source 会执行 start_process）---
: > "$TMP/fn.sh"
for fn in host_arch elf_arch copy_trim_cli resolve_trim_cli trim_cli_pwd_login_ok; do
    awk "/^$fn\(\)/,/^}/" "$MAIN" >> "$TMP/fn.sh"
    grep -q "^$fn()" "$TMP/fn.sh" || fail "函数 $fn 不存在于 cmd/main"
done

mkelif() { printf "\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00$1" > "$2"; chmod +x "$2"; }
setup() { rm -rf "$TMP/app" "$TMP/var"; mkdir -p "$TMP/app" "$TMP/var"; }

run_resolve() { # $1 = 额外的 host_arch 覆盖
    bash -c "
        source '$TMP/fn.sh'
        log_msg() { :; }
        APPDEST='$TMP/app'; PKGVAR='$TMP/var'; PKGETC='$TMP/etc'
        $1
        resolve_trim_cli
    "
}

echo "== elf_arch 架构判定 =="
mkelif '\xb7\x00' "$TMP/arm.bin"; mkelif '\x3e\x00' "$TMP/x64.bin"; echo hi > "$TMP/txt.bin"
bash -c "source '$TMP/fn.sh'; [ \"\$(elf_arch '$TMP/arm.bin')\" = arm64 ]" && pass "aarch64 识别为 arm64" || fail "aarch64 识别错误"
bash -c "source '$TMP/fn.sh'; [ \"\$(elf_arch '$TMP/x64.bin')\" = x64 ]"   && pass "x86-64 识别为 x64"    || fail "x86-64 识别错误"
bash -c "source '$TMP/fn.sh'; [ \"\$(elf_arch '$TMP/txt.bin')\" = other ]" && pass "非 ELF 识别为 other"   || fail "非 ELF 识别错误"

echo "== resolve_trim_cli（当前宿主: $(uname -m)，下面逐例固定 host_arch）=="
setup; out=$(run_resolve "host_arch() { echo arm; }")
[ -z "$out" ] && pass "ARM 无任何候选时失败" || fail "无候选却返回: $out"

setup; mkelif '\xb7\x00' "$TMP/app/trim-cli-linux-arm64"
out=$(run_resolve "host_arch() { echo arm; }")
case "$out" in */trim-cli-linux-arm64) pass "arm64 随包被选中";; *) fail "arm64 随包未选中: $out";; esac

setup; mkelif '\x3e\x00' "$TMP/app/trim-cli-linux-x64"
out=$(run_resolve "host_arch() { echo arm; }")
[ -z "$out" ] && pass "ARM 上拒绝仅有 x86 随包（不再 qemu 硬跑）" || fail "ARM 上误用 x86: $out"

out=$(run_resolve "host_arch() { echo x86; }")
case "$out" in */trim-cli-linux-x64) pass "x86 宿主选中 x64 随包";; *) fail "x86 宿主未选中: $out";; esac

setup; mkelif '\xb7\x00' "$TMP/app/trim-cli-linux-arm64"
out=$(run_resolve "host_arch() { echo x86; }")
[ -z "$out" ] && pass "x86 宿主不误用 arm64 随包" || fail "x86 上误用 arm64: $out"

echo "== trim_cli_pwd_login_ok 版本预检 =="
cat > "$TMP/good" <<'EOS'
#!/bin/sh
printf 'Usage: trim [OPTIONS]\n\nOptions:\n  -u, --username <USERNAME>\n  -p, --password <PASSWORD>\n'
EOS
cat > "$TMP/oauth" <<'EOS'
#!/bin/sh
printf 'Usage: trim [OPTIONS]\n\nOptions:\n      --profile <name>\n      --refresh\n'
EOS
printf '#!/bin/sh\nnot an elf\n' > "$TMP/junk"
chmod +x "$TMP/good" "$TMP/oauth" "$TMP/junk"

bash -c "source '$TMP/fn.sh'; trim_cli_pwd_login_ok '$TMP/good'";  [ $? -eq 0 ] && pass "同代 CLI 判为可用" || fail "同代 CLI 误判"
bash -c "source '$TMP/fn.sh'; trim_cli_pwd_login_ok '$TMP/oauth'"; [ $? -eq 1 ] && pass "OAuth-only 判为版本不兼容" || fail "OAuth-only 未判出"
bash -c "source '$TMP/fn.sh'; trim_cli_pwd_login_ok '$TMP/junk'";  [ $? -eq 2 ] && pass "不可执行判为跑不起来" || fail "坏文件未判出"

echo
if [ "$FAIL" -eq 0 ]; then echo "全部通过"; else echo "失败 $FAIL 项"; exit 1; fi
