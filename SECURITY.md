# Security

Report a vulnerability privately through GitHub:
[Security → Report a vulnerability](https://github.com/widmo-ru/vkteams-mcp/security/advisories/new).
Never in a public issue.

Say what an attacker can do, against which version or commit, and how to reproduce it. We answer
within five working days and say what we will fix and when.

What counts, for this server: anything that lets a model, a message, a bot or the sign-in page act
beyond the one signed-in person; read another person's session or drafts; reach a private network
from the sign-in page; send without the two-step send; or get a sign-in code, a token or message
text into a log. The rules are in [MANIFESTO.md](MANIFESTO.md), and the refusals with their tests in
[docs/requirements.md](docs/requirements.md).
