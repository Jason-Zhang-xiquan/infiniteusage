#!/usr/bin/env bash
# 把 FreeLLMAPI 的统一 key 存到 ~/.infiniteusage/keys.env（只有自己能读）。输入时不显示，key 不打印、不进日志。
# 用法：bash scripts/save_freellmapi_key.sh [ssh主机]
#   给了 ssh 主机（FreeLLMAPI 跑在那台上）：本机也记下 FREELLMAPI_SSH_HOST，iu 会自动开 SSH 隧道；那台机器上也存一份 key
# keys.env 里别的行原样保留，只替换 FREELLMAPI_KEY / FREELLMAPI_SSH_HOST 这两行。
set -euo pipefail
host="${1:-}"
read -rsp "粘贴 FreeLLMAPI 统一 key（输入不显示），然后回车: " k
echo
case "$k" in
  freellmapi-*) ;;
  *) echo "这串不是以 freellmapi- 开头，没保存。请复制 Keys 页面最上方的统一 key。"; exit 1 ;;
esac
umask 077
mkdir -p ~/.infiniteusage
f=~/.infiniteusage/keys.env
touch "$f"
{ grep -v -E '^(FREELLMAPI_KEY|FREELLMAPI_SSH_HOST)=' "$f" || true
  printf 'FREELLMAPI_KEY=%s\n' "$k"
  [ -n "$host" ] && printf 'FREELLMAPI_SSH_HOST=%s\n' "$host"; } > "$f.tmp"
mv "$f.tmp" "$f"
if [ -n "$host" ]; then
  printf '%s\n' "$k" | ssh "$host" 'umask 077; mkdir -p ~/.infiniteusage; f=~/.infiniteusage/keys.env; touch "$f";
    read -r k; { grep -v "^FREELLMAPI_KEY=" "$f" || true; printf "FREELLMAPI_KEY=%s\n" "$k"; } > "$f.tmp"; mv "$f.tmp" "$f"'
fi
unset k
echo "已保存${host:+（本机和 $host 都存了）}，可以回到对话里说一声了。"
