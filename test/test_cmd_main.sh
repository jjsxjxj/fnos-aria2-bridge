#!/usr/bin/env bash
# 校验 cmd/main 的 trim-cli 定位与版本预检逻辑，不需要飞牛环境。
# 覆盖曾出事的两类问题：函数被误删（bash -n 查不出）、选错二进制。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
MAIN="$HERE/../cmd/main"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAIL=0

pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }

: > "$TMP/fn.sh"
for fn in resolve_trim_cli trim_cli_pwd_login_ok; do
    awk "/^$fn\(\)/,/^}/" "$MAIN" >> "$TMP/fn.sh"
    grep -q "^$fn()" "$TMP/fn.sh" || fail "函数 $fn 不存在于 cmd/main"
done

setup() { rm -rf "$TMP/app" "$TMP/var"; mkdir -p "$TMP/app" "$TMP/var"; }

run_resolve() {
    bash -c "source '$TMP/fn.sh'; APPDEST='$TMP/app'; PKGVAR='$TMP/var'; PKGETC='$TMP/etc'; resolve_trim_cli"
}

echo "== resolve_trim_cli 定位顺序 =="
setup; out=$(run_resolve)
[ -z "$out" ] && pass "没有任何候选时失败" || fail "无候选却返回: $out"

setup; mkdir -p "$TMP/app/app"; : > "$TMP/app/app/trim-cli-linux-x64"   # APPDEST/app/ 子目录
out=$(run_resolve)
case "$out" in */app/trim-cli-linux-x64) pass "app/ 子目录命中";; *) fail "app/ 子目录未选中: $out";; esac

setup; : > "$TMP/app/trim-cli-linux-x64"        # APPDEST 根目录（fnOS 解包到根的情况）
out=$(run_resolve)
case "$out" in */trim-cli-linux-x64) pass "APPDEST 根目录也能找到";; *) fail "根目录未选中: $out";; esac

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

echo "== 不应再出现 ARM 相关逻辑 =="
if grep -qE "aarch64|arm64|host_arch|qemu_wrap" "$MAIN"; then
    fail "cmd/main 仍含 ARM 相关代码"
else
    pass "cmd/main 已无 ARM 逻辑"
fi

echo
if [ "$FAIL" -eq 0 ]; then echo "全部通过"; else echo "失败 $FAIL 项"; exit 1; fi
