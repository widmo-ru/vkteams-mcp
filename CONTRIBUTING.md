# Contributing

A change is ready when all of these pass:

```sh
uv sync
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run pytest -q
uv run python scripts/mutation_check.py
uv run python -m vkteams_mcp.budget --check
uv run python -m vkteams_mcp.surface --check
```

CI runs the same on every pull request, and builds and probes the Docker image.

Rules that review holds every change to:

- **Read [MANIFESTO.md](MANIFESTO.md) first.** Each rule says what a violation looks like.
- **A new refusal comes with a test that fails without it** and a row in
  [docs/requirements.md](docs/requirements.md) naming that test. When the guard is a line that can be
  removed on its own, add a mutation for it in `scripts/mutation_check.py`.
- **A request shape comes from a live server,** not from a description of the API. Write what you
  measured in [docs/live-findings.md](docs/live-findings.md), and make the fake in `tests/unit` answer
  in that shape.
- **A changed tool changes `tool-surface.lock.json`:** regenerate it with
  `uv run python -m vkteams_mcp.surface > tool-surface.lock.json` and say why in the pull request.
- **Nothing names a real installation, person or chat.** Use `example.com` and made-up ids.
