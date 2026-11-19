#!/usr/bin/env bash
# The chart in a real cluster (kind): three replicas of the server, one Valkey behind three Sentinels, and a fake
# VK Teams installation (tests/e2e/fake_vkteams.py) to sign in against. No real VK Teams is involved.
#
#   IMAGE=vkteams-mcp:e2e scripts/chart-e2e.sh                         # the chart, with the image under test
#   MODE=baseline IMAGE=vkteams-mcp:main scripts/chart-e2e.sh          # the same checks against an older image
#
# The images must already be loaded into the cluster (`kind load docker-image`). Each check prints PASS or FAIL.
# In `chart` mode the first FAIL stops the run (CONTINUE=1 runs on anyway, to see every result; the run still fails) and the script ends by counting the checks that ran, so a check
# that silently did not run fails the run. In `baseline` mode nothing stops: the older image is deployed the way
# deploy/kubernetes/deployment.yaml deployed it (3 replicas, no shared state) and every check is run, to show
# which of them an old build fails. A check that PASSES on the old image does not tell the two apart.
#
# The three pods are called directly (kubectl port-forward to each pod) so that "pod A" and "pod B" are real.
set -euo pipefail
cd "$(dirname "$0")/.."
image=${IMAGE:?IMAGE=<repository:tag> already loaded into the cluster}
mode=${MODE:-chart}
ns=vkteams-e2e
rel=e2e
host=mcp.example.com
fake="fake-vk.$ns.svc.cluster.local"
work=$(mktemp -d)
cluster=${KIND_CLUSTER:-vk-state}
# Only this kind cluster is visible to everything below (kubectl, helm, new-sealing-key.sh): the default kubeconfig can
# hold any number of other clusters, and another kind cluster's `kind create` moves its current context.
kind get clusters | grep -Fx -- "$cluster" >/dev/null || { echo "no kind cluster named $cluster"; exit 1; }
kind export kubeconfig --name "$cluster" --kubeconfig "$work/kubeconfig"
export KUBECONFIG="$work/kubeconfig"
run=$(date +%s)  # addresses carry it: a rerun against the same Valkey must not meet the limits of the last one
# public ports of pods A, B, C and their metrics ports, on this machine
PUB=(18701 18702 18703)
MET=(18791 18792 18793)
passed=0
failed=0
want=10
failures=""

k() { kubectl --request-timeout=30s -n "$ns" "$@"; }
say() { echo "== $*"; }
check() {
  local name=$1
  shift
  say "$name"
  # In the background and waited for: `set -e` is ignored inside anything run as an `if` or `||` condition, which
  # would let a failing command in a check go by, and report PASS.
  (
    set -euo pipefail
    "$@"
  ) &
  if wait $!; then
    echo "PASS $name"
    passed=$((passed + 1))
  else
    echo "FAIL $name"
    failed=$((failed + 1))
    failures="$failures
  $name"
    if [ "$mode" = chart ] && [ -z "${CONTINUE:-}" ]; then exit 1; fi
  fi
}

pf_pids=""
cleanup() {
  for p in $pf_pids; do kill "$p" 2>/dev/null || true; done
  rm -rf "$work"
}
trap cleanup EXIT

# ---------------------------------------------------------------- setup

setup_namespace() {
  kubectl --request-timeout=60s create namespace "$ns" --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f - >/dev/null
  # the passwords of Valkey and of its Sentinels: random, in a Secret, never printed; the chart's init container
  # stages the same two values for the server
  if ! k get secret valkey-auth >/dev/null 2>&1; then
    k create secret generic valkey-auth \
      --from-literal=password="$(openssl rand -hex 16)" --from-literal=sentinelPassword="$(openssl rand -hex 16)" >/dev/null
  fi
  # a CA, and a certificate for the fake VK Teams host: the server trusts this CA and nothing else
  if [ ! -s "$work/ca.crt" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$work/ca.key" -out "$work/ca.crt" -days 2 \
      -subj "/CN=vkteams-e2e-ca" 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -keyout "$work/tls.key" -out "$work/tls.csr" -subj "/CN=$fake" 2>/dev/null
    printf 'subjectAltName=DNS:%s\n' "$fake" >"$work/ext.cnf"
    openssl x509 -req -in "$work/tls.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial \
      -out "$work/tls.crt" -days 2 -extfile "$work/ext.cnf" 2>/dev/null
  fi
  k create secret generic fake-vk-tls --from-file=tls.crt="$work/tls.crt" --from-file=tls.key="$work/tls.key" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  k create configmap vk-ca --from-file=ca.crt="$work/ca.crt" --dry-run=client -o yaml | k apply -f - >/dev/null
  k create configmap fake-vk-script --from-file=fake_vkteams.py=tests/e2e/fake_vkteams.py \
    --dry-run=client -o yaml | k apply -f - >/dev/null
}

start_state() {
  sed -e "s#IMAGE_PLACEHOLDER#$image#" -e "s#FAKE_HOST_PLACEHOLDER#$fake#" tests/e2e/state.yaml | k apply -f - >/dev/null
  # a rerun made a new CA and certificate: the fake loads its certificate at start, so it restarts to pick them up
  k rollout restart deployment/fake-vk >/dev/null
  k rollout status statefulset/valkey --timeout=240s
  k rollout status statefulset/valkey-sentinel --timeout=240s
  k rollout status deployment/fake-vk --timeout=240s
  # three Sentinels that agree on a master, and a replica attached to it
  for _ in $(seq 1 60); do
    n=$(k exec valkey-sentinel-0 -- sh -c 'valkey-cli -p 26379 -a "$(cat /auth/sentinelPassword)" --no-auth-warning \
      --raw sentinel master mymaster' 2>/dev/null | grep -A1 '^num-other-sentinels$' | tail -1 || true)
    s=$(k exec valkey-sentinel-0 -- sh -c 'valkey-cli -p 26379 -a "$(cat /auth/sentinelPassword)" --no-auth-warning \
      --raw sentinel master mymaster' 2>/dev/null | grep -A1 '^num-slaves$' | tail -1 || true)
    [ "$n" = 2 ] && [ "$s" = 1 ] && return 0
    sleep 2
  done
  echo "the Sentinels never agreed on one master with one replica"
  return 1
}

deploy_chart() {
  local sent="valkey-sentinel-0.valkey-sentinel:26379,valkey-sentinel-1.valkey-sentinel:26379,valkey-sentinel-2.valkey-sentinel:26379"
  local rev=$1
  helm upgrade --install "$rel" charts/vkteams-mcp -n "$ns" --wait --timeout 240s \
    --set image.repository="${image%:*}" --set image.tag="${image##*:}" --set image.pullPolicy=Never \
    --set publicUrl="https://$host" --set replicaCount=3 \
    --set key.existingSecret=vkteams-mcp-key --set key.revision="$rev" \
    --set "state.sentinel.hosts={$sent}" --set state.existingSecret=valkey-auth \
    --set vkteams.allowPrivateNetworks=true --set vkteams.caBundle.configMap=vk-ca \
    --set resources.requests.cpu=20m --set 'metrics.serviceMonitor.enabled=false' "${@:2}" >/dev/null
}

deploy_baseline() {
  # What deploy/kubernetes/deployment.yaml was before the chart: three replicas, memory state, the key by an init
  # container, one port. The same Secret and the same CA, so the only difference is the build and its wiring.
  k apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: $rel-vkteams-mcp, labels: {app.kubernetes.io/name: vkteams-mcp, app.kubernetes.io/instance: $rel}}
spec:
  replicas: 3
  selector: {matchLabels: {app.kubernetes.io/name: vkteams-mcp, app.kubernetes.io/instance: $rel}}
  template:
    metadata: {labels: {app.kubernetes.io/name: vkteams-mcp, app.kubernetes.io/instance: $rel}}
    spec:
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001}
      initContainers:
        - name: key
          image: $image
          imagePullPolicy: Never
          command: ["install", "-m", "0400", "/secret/key", "/run/vkteams-mcp/key"]
          volumeMounts: [{name: secret, mountPath: /secret, readOnly: true}, {name: key, mountPath: /run/vkteams-mcp}]
      containers:
        - name: server
          image: $image
          imagePullPolicy: Never
          env:
            - {name: VKTEAMS_MCP_PUBLIC_URL, value: "https://$host"}
            - {name: VKTEAMS_MCP_ALLOW_PRIVATE_NETWORKS, value: "1"}
            - {name: SSL_CERT_FILE, value: /etc/ca/ca.crt}
          ports: [{name: http, containerPort: 8000}]
          readinessProbe: {httpGet: {path: /healthz, port: http}}
          volumeMounts:
            - {name: key, mountPath: /run/vkteams-mcp}
            - {name: tmp, mountPath: /tmp}
            - {name: ca, mountPath: /etc/ca}
      volumes:
        - {name: secret, secret: {secretName: vkteams-mcp-key, defaultMode: 0440}}
        - {name: key, emptyDir: {medium: Memory}}
        - {name: tmp, emptyDir: {}}
        - {name: ca, configMap: {name: vk-ca}}
EOF
  k rollout status "deployment/$rel-vkteams-mcp" --timeout=180s
}

pods() { # running pods that are not on their way out (a rollout leaves the old ones Running for a few seconds)
  k get pod -l app.kubernetes.io/instance="$rel" -o json \
    | jq -r '.items[] | select(.metadata.deletionTimestamp == null and .status.phase == "Running") | .metadata.name' | sort
}

forward_all() {
  for p in $pf_pids; do kill "$p" 2>/dev/null || true; done
  pf_pids=""
  local i=0 pod
  for pod in $(pods); do
    kubectl -n "$ns" port-forward "pod/$pod" "${PUB[$i]}:8000" >/dev/null 2>&1 &
    pf_pids="$pf_pids $!"
    kubectl -n "$ns" port-forward "pod/$pod" "${MET[$i]}:9090" >/dev/null 2>&1 &
    pf_pids="$pf_pids $!"
    i=$((i + 1))
  done
  [ "$i" -eq 3 ] || { echo "expected 3 running pods, found $i"; return 1; }
  for i in 0 1 2; do
    for _ in $(seq 1 30); do
      curl -fsS --max-time 3 "http://127.0.0.1:${PUB[$i]}/healthz" >/dev/null 2>&1 && continue 2
      sleep 1
    done
    echo "pod $i never answered through its port-forward"
    return 1
  done
}

# ---------------------------------------------------------------- what a browser and a client do

VERIFIER=$(printf 'v%.0s' $(seq 1 64))
CHALLENGE=$(printf '%s' "$VERIFIER" | openssl dgst -sha256 -binary | openssl base64 -A | tr '+/' '-_' | tr -d '=')
CB='https://client.example.org/cb'
cb_enc='https%3A%2F%2Fclient.example.org%2Fcb'

register() {
  curl -fsS --max-time 15 -H "Host: $host" -H 'Content-Type: application/json' "http://127.0.0.1:${PUB[$1]}/register" \
    -d "{\"redirect_uris\":[\"$CB\"],\"token_endpoint_auth_method\":\"none\",\"grant_types\":[\"authorization_code\",\"refresh_token\"],\"response_types\":[\"code\"]}" \
    | jq -er .client_id
}
authorize() { # authorize <pod> <client id> → the sign-in request id
  curl -s --max-time 15 -o /dev/null -D - -H "Host: $host" \
    "http://127.0.0.1:${PUB[$1]}/authorize?response_type=code&client_id=$2&redirect_uri=$cb_enc&state=s&code_challenge=$CHALLENGE&code_challenge_method=S256" \
    | tr -d '\r' | sed -n 's/^[Ll]ocation: .*[?&]request=\([^&]*\).*/\1/p'
}
address_step() { # address_step <pod> <request id> <email> → body in $work/page, prints the http code
  curl -s --max-time 20 -o "$work/page" -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:${PUB[$1]}/signin" \
    --data-urlencode "request=$2" --data-urlencode step=address --data-urlencode "server=$fake" --data-urlencode "email=$3"
}
pending_of() { sed -n 's/.*name="pending" value="\([^"]*\)".*/\1/p' "$work/page" | head -1; }
code_step() { # code_step <pod> <request id> <pending> <code> → headers in $work/hdr, prints the http code
  curl -s --max-time 20 -o "$work/page" -D "$work/hdr" -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:${PUB[$1]}/signin" \
    --data-urlencode "request=$2" --data-urlencode step=code --data-urlencode "pending=$3" --data-urlencode "code=$4"
}
exchange() { # exchange <pod> <client id> <code> → http code, body in $work/tok
  curl -s --max-time 20 -o "$work/tok" -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:${PUB[$1]}/token" \
    --data-urlencode grant_type=authorization_code --data-urlencode "code=$3" --data-urlencode "redirect_uri=$CB" \
    --data-urlencode "client_id=$2" --data-urlencode "code_verifier=$VERIFIER"
}
fake_logins() { k logs deploy/fake-vk 2>/dev/null | grep -c '"step"' || true; }

# ---------------------------------------------------------------- the key story, unchanged since 0.3.0

key_material() { k get secret vkteams-mcp-key -o jsonpath='{.data.key}' | base64 -d; }

c_secret_mounts() {
  local pod
  pod=$(pods | head -1)
  local j
  j=$(k get pod "$pod" -o json)
  echo "$j" | jq -e '[.spec.containers[] | select(.name=="server") | .volumeMounts[].name] | (index("secret") == null) and (index("state-secret") == null)' >/dev/null \
    || { echo "the server container mounts a Secret"; return 1; }
  echo "$j" | jq -e '[.spec.initContainers[] | .volumeMounts[].name] | index("secret") != null' >/dev/null \
    || { echo "no init container mounts the Secret: this check proves nothing"; return 1; }
}

c_no_secret_anywhere_else() {
  : >"$work/needles"
  key_material >>"$work/needles"
  echo >>"$work/needles"
  k get secret valkey-auth -o jsonpath='{.data.password}' | base64 -d >>"$work/needles"
  echo >>"$work/needles"
  k get secret valkey-auth -o jsonpath='{.data.sentinelPassword}' | base64 -d >>"$work/needles"
  echo >>"$work/needles"
  [ "$(grep -c . "$work/needles")" -eq 3 ] || { echo "the needles are not three values"; return 1; }
  local pod
  for pod in $(pods); do
    k exec "$pod" -c server -- env >"$work/env"
    grep -q '^VKTEAMS_MCP_PUBLIC_URL=' "$work/env" || { echo "env printed nothing: the probe is broken"; return 1; }
    local rc=0
    grep -qF -f "$work/needles" "$work/env" || rc=$?
    [ "$rc" = 1 ] || { echo "a key or a password is in $pod's environment"; return 1; }
    k logs "$pod" --all-containers >"$work/log" 2>&1
    [ "$(wc -l <"$work/log")" -gt 0 ] || { echo "no log at all from $pod"; return 1; }
    rc=0
    grep -qF -f "$work/needles" "$work/log" || rc=$?
    [ "$rc" = 1 ] || { echo "a key or a password is in $pod's logs"; return 1; }
  done
}

c_staged_files() {
  local pod got
  for pod in $(pods); do
    got=$(k exec "$pod" -c server -- stat -c '%a %u %n' /run/vkteams-mcp/key /run/vkteams-mcp/state-password \
      /run/vkteams-mcp/state-sentinel-password)
    [ "$(echo "$got" | grep -c '^400 10001 ')" -eq 3 ] || { echo "$pod: $got"; return 1; }
    # a shell of the same user cannot read the server's memory or environment: the process is non-dumpable
    rc=0
    k exec "$pod" -c server -- sh -c 'cat /proc/1/environ >/dev/null' 2>"$work/err" || rc=$?
    [ "$rc" != 0 ] && grep -q 'Permission denied' "$work/err" || { echo "$pod: /proc/1/environ was readable"; return 1; }
  done
}

# ---------------------------------------------------------------- 1. a code is redeemed once, on any pod

c_code_once() {
  local cid rid pending code
  cid=$(register 0)
  rid=$(authorize 0 "$cid")
  [ -n "$rid" ] || { echo "authorize gave no sign-in request"; return 1; }
  [ "$(address_step 0 "$rid" code-once-$run@example.com)" = 200 ] || { echo "the address step failed"; cat "$work/page"; return 1; }
  pending=$(pending_of)
  [ -n "$pending" ] || { echo "no code step offered"; return 1; }
  [ "$(code_step 0 "$rid" "$pending" ab12cd)" = 302 ] || { echo "the code step failed"; return 1; }
  code=$(tr -d '\r' <"$work/hdr" | sed -n 's/^[Ll]ocation: .*[?&]code=\([^&]*\).*/\1/p')
  [ -n "$code" ] || { echo "no authorization code in the redirect"; return 1; }
  # the same code, redeemed on pod B first (the pod that did not issue it), then on C and A
  local b c a
  b=$(exchange 1 "$cid" "$code")
  c=$(exchange 2 "$cid" "$code")
  a=$(exchange 0 "$cid" "$code")
  echo "redeemed on B: $b, then on C: $c, then on A: $a"
  [ "$b" = 200 ] || { echo "the first redemption was refused"; cat "$work/tok"; return 1; }
  [ "$c" = 400 ] && [ "$a" = 400 ] || { echo "a code redeemed once was accepted again on another pod"; return 1; }
  grep -q invalid_grant "$work/tok" || { echo "the refusal is not invalid_grant"; return 1; }
}

# ---------------------------------------------------------------- 2. sign-in limits are shared

c_code_requests_limited() {
  local cid rid seq="" i code
  cid=$(register 0)
  rid=$(authorize 0 "$cid")
  # three codes an hour per address: the fourth request, wherever it lands, is refused
  for i in 0 1 2 0 1; do
    code=$(address_step "$i" "$rid" limited-$run@example.com)
    seq="$seq $code"
    if grep -q 'Too many codes' "$work/page"; then seq="$seq(limited)"; fi
  done
  echo "address step through pods A B C A B:$seq"
  [ "$seq" = " 200 200 200 400(limited) 400(limited)" ] || { echo "the limit is not shared across the pods"; return 1; }
}

c_wrong_codes_counted() {
  local cid rid pending seq="" i code
  cid=$(register 0)
  rid=$(authorize 0 "$cid")
  [ "$(address_step 0 "$rid" tries-$run@example.com)" = 200 ] || { echo "the address step failed"; return 1; }
  pending=$(pending_of)
  # five wrong codes per sign-in step, spread over the three pods; the sixth is refused on any of them
  for i in 0 1 2 0 1 2 0; do
    code=$(code_step "$i" "$rid" "$pending" WRONG1)
    seq="$seq $code"
  done
  echo "wrong codes through pods A B C A B C A:$seq"
  [ "$seq" = " 400 400 400 400 400 429 429" ] || { echo "wrong codes are not counted across the pods"; return 1; }
  # and the right code no longer helps this step, on a pod that has counted none of them itself
  [ "$(code_step 2 "$rid" "$pending" ab12cd)" = 429 ] || { echo "the right code was accepted after the limit"; return 1; }
}

# ---------------------------------------------------------------- 5. the Valkey master dies

sentinel_master() {
  k exec valkey-sentinel-0 -- sh -c 'valkey-cli -p 26379 -a "$(cat /auth/sentinelPassword)" --no-auth-warning --raw \
    sentinel get-master-addr-by-name mymaster' 2>/dev/null | head -1
}
ready_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:${PUB[$1]}/readyz" || true; }
metric() { # metric <pod> <exact series text> → its value
  curl -fsS --max-time 4 "http://127.0.0.1:${MET[$1]}/metrics" | sed -n "s/^$2 //p" | head -1
}

sentinels_calm() { # no Sentinel is in TILT mode (it enters it when the clock jumps, and starts no failover while in it)
  local i v
  for i in 0 1 2; do
    v=$(k exec "valkey-sentinel-$i" -- sh -c 'valkey-cli -p 26379 -a "$(cat /auth/sentinelPassword)" --no-auth-warning info sentinel' \
      | tr -d '\r' | sed -n 's/^sentinel_tilt://p')
    [ "$v" = 0 ] || return 1
  done
}

c_failover() {
  local before after master t0 first503="" recovered="" up_gauge="" logins_at_first503="" logins_now now n=0 dropped=0
  local rc code endpoints
  for _ in $(seq 1 40); do sentinels_calm && break; sleep 2; done
  sentinels_calm || { echo "the Sentinels stay in TILT mode (the machine's clock keeps jumping): no failover can be tested here"; return 1; }
  before=$(sentinel_master)
  master=$(k get pod -l app=valkey -o json | jq -r --arg ip "$before" '.items[] | select(.status.podIP == $ip) | .metadata.name')
  [ -n "$master" ] || { echo "no Valkey pod has the address $before"; return 1; }
  echo "master before: $before"
  [ "$(ready_code 0)" = 200 ] || { echo "pod A is not ready before the test"; return 1; }
  # the master stops answering: to the Sentinels and to every client it is dead. (A frozen process, not a deleted
  # pod: a deleted pod's StatefulSet replacement comes back in two seconds, before any Sentinel gives up on it.)
  k exec "$master" -- sh -c 'valkey-cli -a "$(cat /auth/password)" --no-auth-warning debug sleep 25' >/dev/null 2>&1 &
  local sleeper=$!
  t0=$(date +%s)
  local cid rid
  cid=$(register 1)
  rid=$(authorize 1 "$cid")
  for _ in $(seq 1 400); do
    now=$(date +%s)
    n=$((n + 1))
    # readiness does not follow the store: /readyz stays 200 on the public port of pod A and on the metrics port of B
    rc=$(ready_code 0)
    [ "$rc" = 200 ] || dropped=$((dropped + 1))
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:${MET[1]}/readyz" || true)" = 200 ] || dropped=$((dropped + 1))
    # a person trying to sign in on pod B, with an address of their own each time
    code=$(address_step 1 "$rid" "poll-$run-$n@example.com" || true)
    if [ -z "$first503" ] && [ "$code" = 503 ]; then
      first503=$now
      logins_at_first503=$(fake_logins)
      echo "sign-in on pod B: 503 at +$((now - t0))s"
      grep -qi 'try again shortly' "$work/page" || { echo "the 503 page has no 'try again shortly' sentence"; return 1; }
      # the pod that just refused saw the failure itself: its gauge is 0 (the other pods' heartbeats notice within 5 s)
      up_gauge=$(metric 1 'mcp_state_store_up{server="vkteams-mcp"}' || true)
      echo "mcp_state_store_up on pod B during the outage: ${up_gauge:-none}"
      endpoints=$(k get endpoints "$rel-vkteams-mcp" -o jsonpath='{.subsets[*].addresses[*].ip}' | wc -w | tr -d ' ')
      echo "pods in the Service during the outage: $endpoints"
      [ "$endpoints" = 3 ] || { echo "a pod left the Service because the store was down"; return 1; }
    elif [ -n "$first503" ] && [ "$code" != 200 ] && [ "$code" != 503 ]; then
      echo "sign-in answered $code during the outage"
      return 1
    fi
    if [ -n "$first503" ] && [ "$code" = 200 ]; then
      now=$(date +%s)  # when the answer came back, not when this round began
      recovered=$now
      echo "sign-in works again at +$((now - t0))s (outage $((now - first503))s)"
      break
    fi
    sleep 0.3
  done
  wait "$sleeper" 2>/dev/null || true
  [ -n "$first503" ] || { echo "sign-in never failed while the master was frozen"; return 1; }
  [ "$up_gauge" = 0.0 ] || { echo "mcp_state_store_up was not 0 during the outage"; return 1; }
  [ "$dropped" = 0 ] || { echo "/readyz was not 200 on $dropped probes during the outage"; return 1; }
  [ -n "$recovered" ] || { echo "sign-in never came back"; return 1; }
  [ $((recovered - first503)) -le 30 ] || { echo "recovery took more than 30 s"; return 1; }
  logins_now=$(fake_logins)
  # the one sign-in that succeeded is the only one that reached VK Teams since the first refusal
  [ "$logins_now" = $((logins_at_first503 + 1)) ] || { echo "VK Teams saw $((logins_now - logins_at_first503)) sign-ins during the outage"; return 1; }
  # sign-in works again on every pod, and Sentinel really moved the master
  local i
  cid=$(register 0)
  for i in 0 1 2; do
    rid=$(authorize "$i" "$cid")
    [ "$(address_step "$i" "$rid" "after-$run-$i@example.com")" = 200 ] || { echo "pod $i cannot sign in after the failover"; return 1; }
  done
  after=$(sentinel_master)
  echo "master after: $after"
  [ -n "$after" ] && [ "$after" != "$before" ] || { echo "the master did not change: no failover happened"; return 1; }
  # the errors were counted, and no container was restarted for it: liveness is the process alone
  local errors
  errors=$(curl -fsS --max-time 4 "http://127.0.0.1:${MET[0]}/metrics" | grep '^mcp_state_store_errors_total' | grep -v '_created' | awk '{s += $2} END {print s + 0}')
  echo "mcp_state_store_errors_total on pod A: $errors"
  [ "$errors" -gt 0 ] || { echo "no state store error was counted"; return 1; }
  local restarts
  restarts=$(k get pod -l app.kubernetes.io/instance="$rel" -o jsonpath='{range .items[*]}{.status.containerStatuses[*].restartCount}{"\n"}{end}' | tr -d '\n ')
  [ "${restarts:-x}" = 000 ] || { echo "a container restarted: $restarts"; return 1; }
  # the old master wakes up and is told to follow the new one
  for _ in $(seq 1 60); do
    [ "$(k exec "$master" -- sh -c 'valkey-cli -a "$(cat /auth/password)" --no-auth-warning --raw role' 2>/dev/null | head -1)" = slave ] && return 0
    sleep 2
  done
  echo "the old master did not become a replica"
  return 1
}

# ---------------------------------------------------------------- 7. metrics, on their own port

c_metrics() {
  local i body
  for i in 0 1 2; do
    body=$(curl -fsS --max-time 5 "http://127.0.0.1:${MET[$i]}/metrics") || { echo "pod $i: no /metrics on 9090"; return 1; }
    [ -n "$body" ] || { echo "pod $i: empty /metrics"; return 1; }
    echo "$body" | grep -q '^mcp_http_requests_in_flight{route_group="mcp",server="vkteams-mcp"}' \
      || { echo "pod $i: no mcp_http_requests_in_flight"; return 1; }
    echo "$body" | grep -q '^mcp_state_store_up{server="vkteams-mcp"} 1.0$' || { echo "pod $i: mcp_state_store_up is not 1"; return 1; }
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Host: $host" "http://127.0.0.1:${PUB[$i]}/metrics")" = 404 ] \
      || { echo "pod $i: the public port serves /metrics"; return 1; }
    for probe in healthz readyz; do
      [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${MET[$i]}/$probe")" = 200 ] \
        || { echo "pod $i: no /$probe on the metrics port"; return 1; }
    done
  done
}

# ---------------------------------------------------------------- 6. every log line is JSON

c_logs_are_json() {
  local pod c n total=0
  for pod in $(pods); do
    for c in stage-keys server; do
      k logs "$pod" -c "$c" >"$work/log" 2>&1
      n=$(wc -l <"$work/log" | tr -d ' ')
      [ "$n" -gt 0 ] || { echo "$pod/$c: no log lines: the check would pass vacuously"; return 1; }
      python3 - "$work/log" <<'PY' || { echo "$pod/$c: a line is not JSON"; return 1; }
import json, sys
n = 0
for line in open(sys.argv[1]):
    if line.strip():
        j = json.loads(line)
        assert isinstance(j, dict) and j.get("event") and j.get("ts"), line
        n += 1
assert n > 0
PY
      echo "$pod/$c: $n lines, all JSON"
      total=$((total + n))
    done
  done
  [ "$total" -gt 6 ] || { echo "only $total lines in all"; return 1; }
}

# ---------------------------------------------------------------- 8. the autoscaler has something to read

c_hpa() {
  if [ "$mode" != chart ]; then echo "chart only"; return 1; fi
  kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml" >/dev/null
  kubectl -n kube-system patch deployment metrics-server --type json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null
  kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s
  deploy_chart "$kid" --set autoscaling.enabled=true --set autoscaling.minReplicas=3 --set autoscaling.maxReplicas=4
  k get hpa "$rel-vkteams-mcp" >/dev/null
  local cur=""
  for _ in $(seq 1 60); do
    cur=$(k get hpa "$rel-vkteams-mcp" -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null || true)
    [ -n "$cur" ] && break
    sleep 5
  done
  k get hpa "$rel-vkteams-mcp"
  [ -n "$cur" ] || { echo "the HPA shows no current CPU value"; return 1; }
}

# ---------------------------------------------------------------- run

setup_namespace
start_state
kid=none
if [ "$mode" = chart ]; then
  kid=$(scripts/new-sealing-key.sh "$ns" vkteams-mcp-key)
  deploy_chart "$kid"
else
  kid=$(scripts/new-sealing-key.sh "$ns" vkteams-mcp-key)
  deploy_baseline
fi
forward_all

check "key: the Secrets are mounted into the init container only" c_secret_mounts
check "key: no key or password in any environment or log" c_no_secret_anywhere_else
check "key: staged files are 0400 for the server's user; the process is non-dumpable" c_staged_files
check "1. a code redeemed on one pod is refused on the others" c_code_once
check "2. code requests are limited across pods" c_code_requests_limited
check "2. wrong codes are counted across pods" c_wrong_codes_counted
check "5. the Valkey master freezes: sign-in fails closed (503), readiness and Service stay up, failover, sign-in again within 30 s" c_failover
check "7. /metrics on 9090 of every pod, none on the public port" c_metrics
check "6. every log line of every container is JSON" c_logs_are_json
check "8. the HPA renders and shows a CPU target" c_hpa

# 3. the send allowance across replicas is checked at contract level, on a real Valkey and Sentinel (CI job `state`):
#    tests/unit/test_replicas_on_valkey.py::test_the_send_allowance_is_one_bucket_across_replicas
echo "NOTE 3. the send allowance is one bucket across replicas: contract level, not here (needs an upstream chat)"
echo "NOTE 4. short links across pods: microsoft-365-mcp only"

echo "chart e2e ($mode): $passed passed, $failed failed"
if [ "$mode" = chart ]; then
  [ "$failed" -eq 0 ] || exit 1
  [ "$passed" -eq "$want" ] || { echo "$passed of $want checks ran"; exit 1; }
  echo "chart e2e: $passed of $want checks passed"
else
  echo "failed on the old image:$failures"
fi
