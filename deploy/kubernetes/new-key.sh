#!/bin/sh
# A new sealing key for vkteams-mcp, written straight into its Secret, then a restart of its pods so their
# init containers copy it. Run it as the deploy step of any pipeline, with kubectl pointed at the cluster:
#
#   NAMESPACE=vkteams deploy/kubernetes/new-key.sh
#
# The key goes from openssl to the API server through this shell's memory and a pipe: it is in no file,
# no process's arguments and no log line, and nothing here prints it. Every deploy signs everyone out once; a pod restarted without a deploy
# reads the same Secret and keeps everyone signed in.
set -eu
set +x  # a traced run would print the key
ns="${NAMESPACE:?set NAMESPACE}"
timeout="${KUBECTL_TIMEOUT:-60s}"
secret="${SECRET_NAME:-vkteams-mcp-key}"
deployment="${DEPLOYMENT:-vkteams-mcp}"

# The key stays in this shell's memory: printf is a builtin, so it is in no process's arguments either.
key="$(openssl rand -base64 48 | tr -d '\n')"
if [ "${#key}" -ne 64 ]; then
  echo "openssl gave no key; the Secret is unchanged" >&2
  exit 1
fi
# Server-side apply: a client-side apply would keep a second copy of the key in the Secret's
# last-applied-configuration annotation. kubectl's own errors can quote the object, so they are not shown.
if ! printf '%s' "$key" |
  kubectl -n "$ns" create secret generic "$secret" --from-file=key=/dev/stdin --dry-run=client -o yaml |
  kubectl -n "$ns" apply --server-side --force-conflicts --field-manager=vkteams-mcp-new-key \
    --request-timeout="$timeout" -f - >/dev/null 2>&1; then
  unset key
  echo "could not write secret/$secret (kubectl's message is not shown, it can carry the key); the Secret is unchanged" >&2
  exit 1
fi
unset key
# an older client-side apply left that copy behind; remove it if it is there
kubectl -n "$ns" annotate --request-timeout="$timeout" secret "$secret" \
  kubectl.kubernetes.io/last-applied-configuration- >/dev/null 2>&1 || true
echo "new key in secret/$secret"
kubectl -n "$ns" rollout restart --request-timeout="$timeout" "deployment/$deployment"
kubectl -n "$ns" rollout status "deployment/$deployment" --timeout=5m
