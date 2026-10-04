# Coding Agent

An AI coding agent with a private shadow workspace, approval policies and
isolated tool execution. Successful write/edit actions are saved to your selected
project, so new files appear immediately in your editor and file browser.
Shell commands run only against a private read-only copy.

## First-time setup

Requires Linux, Python 3.10+, Docker Compose, and running rootless Docker with
cgroups v2. Windows/macOS Docker Desktop is not supported by this safety profile.

From the repository root:

```sh
python3 scripts/setup.py
```

Setup detects Docker, builds the sandbox and saves local settings in `.env`.
It creates `.env` if needed and preserves existing API settings. Fill in
`API_KEY`, `MODEL` and `BASE_URL` in your `.env`. Each teammate runs setup locally;
do not share `.env`. Rerun setup when trusted sandbox helpers change.

## Run

Choose one command:

```sh
# Ask before every supported action
docker compose run --build --rm coding-agent --approval-mode ask_all

# Ask when an action needs review (default)
docker compose run --build --rm coding-agent --approval-mode ask_on_escalation

# Use the reviewer for eligible actions
docker compose run --build --rm coding-agent --approval-mode auto_review
```

Type `exit` before starting another mode. Opaque shell commands without complete
content analysis still need human approval in `auto_review`.

See [run guide](docs/run_guide.md) and [manual safety tests](docs/superpowers/safety-manual-test.md).
