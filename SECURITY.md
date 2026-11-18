# Security

Report a vulnerability through GitHub's private form:
[Security → Report a vulnerability](https://github.com/widmo-ru/vkteams-mcp/security/advisories/new).
Never in a public issue.

Say what an attacker can do, against which version or commit, and how to reproduce it. We answer
within five working days and say what we will fix and when.

What counts, for this server: anything that lets a model, a message, a bot or the sign-in page act
beyond the one signed-in person; read another person's session or drafts; reach a private network
from the sign-in page; send without the two-step send; or get a sign-in code, a token or message
text into a log. The rules are in [MANIFESTO.md](MANIFESTO.md), and the refusals with their tests in
[docs/requirements.md](docs/requirements.md).

## The sealing key

Every session, registration, draft and file link is sealed under one key per deploy. Where it is, when it
arrives as a file through an init container ([deploy/README.en.md](deploy/README.en.md)):

| Place | Holds the key |
|---|---|
| the Kubernetes Secret the deploy writes | yes |
| the init container that copies it | while it copies |
| the memory-backed file it copies to (emptyDir `medium: Memory`) | yes: written 0400 and owned by the server's user; at start a file the group or anyone else can read, or one owned by another user, is refused. It stays so a container restart keeps sessions |
| a Secret volume in the server's container | no: the Secret is mounted into the init container only |
| the server container's environment and logs | no; `tests/unit/test_key.py` and the CI step "The key is in its 0400 file and nowhere else" check each by value |
| the server process's memory | yes; the process sets `PR_SET_DUMPABLE=0`, so another process of the same user cannot read it through `/proc/<pid>/mem` or ptrace. On Linux a server that cannot set it does not start; CI checks that `/proc/1/environ` is refused, and fails against a server without it |

What remains is the operator's side, not this server's: whoever may `pods/exec` into the server's container
runs as its user and can read the key file, and whoever may read Secrets in the namespace reads the key there.
Both are closed by the cluster's RBAC on `pods/exec` and `secrets`.

The deprecated `VKTEAMS_MCP_SECRET` (0.3.x only) gives none of this: a variable in the pod spec is shown by
every `kubectl exec … env`, whatever the process does with it.
