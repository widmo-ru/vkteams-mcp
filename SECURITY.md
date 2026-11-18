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
| the memory-backed file it copies to | until the server starts: the server reads it and deletes it |
| the server container's environment, files and logs | no; `tests/unit/test_key.py` and the CI step "The key is read once and left nowhere" check each by value |
| the server process's memory | yes, and only there |

What remains is the operator's side, not this server's: whoever may read Secrets in the namespace reads the
key, and whoever may `pods/exec` into the pod as the same user can read the process's memory. Both are the
cluster's RBAC.

The deprecated `VKTEAMS_MCP_SECRET` (0.3.x only) gives none of this: a variable in the pod spec is shown by
every `kubectl exec … env`, whatever the process does with it.
