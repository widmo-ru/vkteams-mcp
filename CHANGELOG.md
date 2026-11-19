# Changelog

## 0.4.0 (2026-11-19)

Many replicas, JSON logs, metrics, and a chart.

- **A shared state store.** The counters that cannot travel sealed move out of each replica's memory into Redis or
  Valkey (standalone with `VKTEAMS_MCP_STATE_URL`, or behind Sentinel with `VKTEAMS_MCP_STATE_SENTINELS` and
  `VKTEAMS_MCP_STATE_SENTINEL_MASTER`; `_STATE_DB`, `_STATE_TLS`, `_STATE_USERNAME`, `_STATE_PREFIX`; passwords only from
  files, `_STATE_PASSWORD_FILE` and `_STATE_SENTINEL_PASSWORD_FILE`, with the key file's checks). Used authorization
  codes (a code redeemed on one replica was accepted again on another), sign-in code requests per address and per
  network, wrong codes per sign-in step, the send allowance and the upload locks. Sessions, drafts and links stay sealed
  and are never stored. Keys are HMACs of the value under a sub-key of the sealing key: no login, address or IP in the
  store; every key has a TTL, and the scripts carry `#!lua` so a store out of memory refuses them instead of letting a limit through (Redis or Valkey 7 or later). Nothing set is memory, right for one replica, with a warning at start.
- **A store that cannot be reached fails closed.** Sign-in, spending a code and sending refuse with "try again shortly";
  reading goes on. A refused token request answers 503 and leaves the code unspent. `/readyz` does not depend on the store.
- **JSON logs, all of them.** Every line of every process, the init container and a failed start included, is one
  JSON object (`ts`, `level`, `logger`, `event`, `msg`); a quote or a traceback can no longer break a record, a traceback
  is frames and exception types without messages, and a record that looks like content is dropped and counted.
  `LOG_LEVEL`.
- **Metrics** on `VKTEAMS_MCP_METRICS_PORT` (9090), never the public port, with `/healthz` and `/readyz` there too:
  `mcp_http_requests_in_flight` and the family's other series (requests, tools, VK Teams calls, sign-in, refusals,
  links, the store, `mcp_build_info`), all labelled `server="vkteams-mcp"`.
- **`VKTEAMS_MCP_TRUSTED_PROXIES`.** Behind an ingress the per-network sign-in limit counted the whole company as one
  address; now only the listed proxies may say who the client is, and without the setting that limit is off (the
  per-address limit stays). `VKTEAMS_MCP_CA_BUNDLE` for a VK Teams server behind an internal CA.
- **The chart** `oci://ghcr.io/widmo-ru/charts/vkteams-mcp` replaces `deploy/kubernetes/` and `deploy/helm/`: the key
  through the same init container (now `python -m vkteams_mcp.stage`, JSON log, also staging the store's passwords), a
  rolling update, a PodDisruptionBudget, soft spreading, probes on the metrics port, explicit ingress paths, an optional
  ServiceMonitor (off by default), no HPA by default.
- **The key is stable across deploys.** 0.3.x drew a new random key on every Helm render, so every deploy signed
  everyone out. The key is now a Secret you own (`scripts/new-sealing-key.sh` makes one), and a deploy keeps everyone
  signed in. Replacing it is a deliberate rotation: everyone signs in again once.
- **Breaking: `VKTEAMS_MCP_SECRET` is refused**, as 0.3.0 announced. The server does not start while the variable is
  set, whether or not a key file exists, and the error names the variable. To migrate: put the key in a Secret (the one
  you already run with, if you want nobody signed out: `kubectl get secret … -o jsonpath='{.data.key}'` holds it, or
  write the value of the variable under the field `key`), deploy with the chart (or add the init container
  `python -m vkteams_mcp.stage /secret/key:/run/vkteams-mcp/key`), and remove the variable from the Deployment, `envFrom`
  and any secret manager that sets it. The same key value in the file keeps everyone signed in. Steps in
  [deploy/README.en.md](deploy/README.en.md#moving-from-03x).
- The image's bases are pinned by digest and replaceable (`BASE_IMAGE`, `UV_IMAGE`). The image is tagged `x.y.z` and `x.y`; there is no `latest` tag any more: pin a version.
- A number that is not a number in a header or a setting is refused, not a crash.

Proof is in CI: the state store's contract suite on real Valkey and Redis (standalone and Sentinel), and the chart in
kind with three replicas, Valkey with Sentinel and a fake VK Teams (`scripts/chart-e2e.sh`).

## 0.3.0 (2026-11-18)

The sealing key leaves the environment.

- The key arrives as a file, `VKTEAMS_MCP_KEY_FILE` (default `/run/vkteams-mcp/key`), mode 0400 and owned by the
  server's user. In Kubernetes an init container copies it there from a Secret into an emptyDir with
  `medium: Memory`; the server's container gets no Secret volume and no variable, so `kubectl exec … env` shows
  nothing. The file stays, so a container restarted inside the pod (an OOM kill, a crash, a liveness probe)
  keeps everyone signed in. Without the file the server does not start and says why; a file the group or
  anyone else can read is refused.
- The process makes itself non-dumpable (`PR_SET_DUMPABLE=0`) on Linux: another process of the same user cannot
  read its memory through `/proc/<pid>/mem` or ptrace. What remains is `kubectl exec` into the server's
  container reading the file as the same user: that is closed by the cluster's RBAC on `pods/exec`.
- The deploy pipeline writes a new random key into the Secret on every deploy: `deploy/kubernetes/new-key.sh`
  for any CI, `deploy/helm/secret.yaml` for Helm and werf. **Every deploy signs everyone out once**: old tokens
  get 401 `invalid_token` with `resource_metadata`, the old registration gets `invalid_client`, and the client
  signs in again. A pod restarted or rescheduled without a deploy reads the same Secret and keeps everyone signed in.
  Replicas are fine; `strategy: Recreate` keeps two keys from serving during a rollout.
- **`VKTEAMS_MCP_SECRET` is deprecated.** In 0.3.x it is still used when there is no key file, with a warning
  in the log, so an upgrade with unchanged manifests neither crash-loops nor signs anyone out; when a key file
  is there the variable is ignored. The next minor release refuses to start while it is set. Either way it is
  dropped from the process's environment.
- The settings no longer print the key in their `repr`.
- The image has `/run/vkteams-mcp`, owned by the server's user.

**Verified live on 2026-11-19** against a real VK Teams server, on the published image in kind: a signed-in
session survived a container restart with the same access token; after a new key the old access token got 401
`invalid_token` with `resource_metadata`, the old refresh token 401 `invalid_client`, and a second sign-in
worked. How to repeat it: [deploy/README.en.md](deploy/README.en.md#live-proof).

## 0.2.6 (2026-11-18)

From the first production report.

- One-to-one chats no longer vanish from the chat list. An older session's event stream replayed the
  group list without dialog states; the server now opens a new session whenever the stream carries no
  dialog states, not only when it is empty.
- A chat can be named by a colleague's name even when it is not in the chat list yet: the directory is
  searched, as `find_people` does, and several matches are offered rather than guessed. "Избранное",
  "Favorites" and "Saved messages" name the person's own chat.
- The image is built on Alpine and has no HIGH or CRITICAL findings in Trivy; the Debian base carried 101,
  none with a fix from Debian. CI now fails on any such finding.
- The image is published as a plain Docker manifest list with linux/amd64 and linux/arm64 and nothing else.
  The provenance entries of earlier releases made a Nexus proxy offer only arm64.

## 0.2.5 (2026-11-16)

The open QA remarks.

- Search across all chats reports `chats_matched` instead of a `total` that counted chats, not messages,
  and stops offering a cursor once every matching chat has come.
- Voting checks the poll first: a closed poll and an option the poll lacks are refused by name, before the
  action allowance is spent.
- `find_people` says when there are more people than shown, marks bots as `kind: bot`, refuses an empty
  query and says how many chats a person shares when it lists only ten.
- `list_chats` follows the event stream past the session's first batch, so `last_at`, unread counts and last
  messages show what happened since the session began (live: `last_at` stayed hours behind).
- Sending a draft that went out before says so: `already_sent` with a note. VK Teams answers such a resend with
  the first message's id and time, even if that message was deleted, and posts nothing new.
- A call needs a title. A damaged cursor, garbled or with an altered signature, is called damaged. `get_chat` with `include_members` on a
  one-to-one chat says who its members are instead of ignoring the request.

## 0.2.4 (2026-11-16)

From the sixth QA round, a full regression of 158 scenarios with no failures.

- A sealed link, token or draft has one spelling: the spare bits of its last character must be zero, so
  four spellings of one link no longer all work.
- A reaction that is not one emoji is refused as such, not as a missing reaction.

## 0.2.3 (2026-11-16)

Fixes from the fifth QA round.

- Control characters, private-use and unassigned code points count as invisible: a poll answer, a message
  or an edit made only of them is refused. Live, a poll answer of U+0001 alone was sent and kept. Emoji
  blocks stay visible, so an emoji newer than the server's Unicode database is not taken for empty.
- A reply to someone else's message names the quoted author among the draft's mentions and counts them
  towards the 20-people cap: VK Teams notifies them (live: 20 mentions and a quote recorded 21).

## 0.2.2 (2026-11-16)

Fixes from the fourth QA round.

- A mention written with spaces inside, `@[ address ]`, counts towards the 20-people cap in drafts and
  edits and is listed in the draft preview. VK Teams treats it as a mention (live: an edit with 21 of them
  notified 21 people).
- An edit to invisible characters only is refused, as a draft already was. Hangul fillers (U+3164 and
  its kin) and the blank Braille pattern (U+2800) now count as invisible in drafts, polls and edits.
- A bot button's print is keyed by the server's secret. The unkeyed 24-bit print let a bot find another
  button with the same print in under a minute and have it pressed. Its fields are encoded one by one,
  so moving a line from the label into the data changes the print.
- `@someone@example.com` in an edit becomes a mention, as in a draft, and counts towards the cap.

## 0.2.1 (2026-11-16)

- A client the server no longer knows (registered with 0.1, or before the secret changed) used to get
  raw JSON from `/authorize`. The browser now shows a page in the person's language that says to clear
  the connector's authorization in the AI app and connect again. Redirects are unchanged.

## 0.2.0 (2026-11-16)

The server keeps nothing: any number of replicas sharing one secret serve the same clients.

- The person's VK Teams session travels sealed (AES-GCM) inside their OAuth tokens, which the client
  refreshes itself by the standard. The client's registration is its `client_id`; the authorization code,
  the sign-in code step and file links carry what they need. No database, no `/data` volume,
  `VKTEAMS_MCP_DATABASE` is gone.
- `save_draft` returns a sealed `draft`; `send_draft` takes it, and with attachments the `files` each upload
  printed. Sending the same draft again posts nothing twice: VK Teams merges a resend with the same
  `localMsgId` (live), and scheduled drafts are checked against the queue, which VK Teams does not merge.
- Disconnecting ends the session in VK Teams. A single issued token can no longer be revoked before it
  expires (access tokens live an hour), a refresh token is not single-use, and the rate allowance is counted
  per replica. A scheduled poll or quote sent again is queued again.
- A test runs a sign-in, a draft and an upload across fresh replicas; CI starts two containers and checks
  that one accepts what the other issued.

## 0.1.0 (2026-11-16)

The first public release.

- 12 tools by default (14 with group administration): chats, messages, people, files, call links and
  bots, about 4,100 tokens of catalogue.
- Streamable HTTP with OAuth 2.1: client registration, PKCE, refresh tokens. A sign-in page in
  Russian and English where the person types the code VK Teams e-mails them; the model never sees it.
- Two-step send, a confirmation flag on every action others see, one rate allowance per person, at most
  20 people per action, mentions included.
- Message ids travel as strings: live ids exceed 2^53, which a JavaScript client rounds.
- A bot's buttons are shown by number and a print; the server presses with the bot's own data and
  refuses a button the bot changed after it was read.
- Files move through signed links that expire in minutes; downloads pass a byte range through.
- Checked on a live VK Teams server (API 139) in three QA rounds; every defect found is fixed and held by
  a test.
