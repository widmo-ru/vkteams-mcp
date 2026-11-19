#!/usr/bin/env bash
# A new sealing key for vkteams-mcp, written by CI into the Secret the chart stages.
#
#   rev=$(scripts/new-sealing-key.sh <namespace> <secret name> [secret key, default "key"])
#   helm upgrade --install … --set key.existingSecret=<secret name> --set key.revision="$rev"
#
# The key never appears in this script's output, its arguments or the arguments of the commands it runs: it goes
# from openssl to a 0600 file in a private directory, and kubectl reads that file. Other keys of the same Secret
# are kept. Every new key signs everyone out once, when it is rolled out. The script prints a revision id, which is
# not secret, for `key.revision`: it changes the pod annotation, so every pod restarts and stages the new key.
set -euo pipefail
ns=${1:?usage: new-sealing-key.sh <namespace> <secret name> [secret key]}
name=${2:?usage: new-sealing-key.sh <namespace> <secret name> [secret key]}
field=${3:-key}

umask 077
work=$(mktemp -d)
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

rev="k$(date -u +%Y%m%d%H%M%S)$(openssl rand -hex 2)"
key=$(openssl rand -base64 48 | tr -d '\n')
test "${#key}" -eq 64 || { echo "the generated key is not 48 random bytes" >&2; exit 2; }
value=$(printf '%s' "$key" | openssl base64 -A)
printf '{"data":{"%s":"%s"}}' "$field" "$value" >"$work/patch.json"
unset key value

k() { kubectl --request-timeout=30s "$@"; }
if k -n "$ns" get secret "$name" >/dev/null 2>&1; then
  k -n "$ns" patch secret "$name" --type merge --patch-file "$work/patch.json" >/dev/null
else
  printf '{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"%s","namespace":"%s"},' "$name" "$ns" >"$work/secret.json"
  tail -c +2 "$work/patch.json" >>"$work/secret.json"
  k create -f "$work/secret.json" >/dev/null
fi
echo "$rev"
