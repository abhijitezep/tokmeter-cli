# tokmeter-cli

A command-line tool that reads your local GitHub Copilot session databases and produces a detailed markdown report of token usage, costs, cache efficiency, and model breakdown — no network calls, no authentication.

## How it works

Copilot CLI and VS Code Copilot Chat both write usage data to local SQLite databases. `tokmeter` discovers those databases, snapshots them (WAL-safe), merges the data, and joins published model rate cards to produce cost estimates.

**Data sources discovered automatically:**
- `~/.copilot/session-store.db` — Copilot CLI / desktop
- VS Code global storage (`github.copilot-chat/session-store.db`)
- VS Code workspace chat session journals (`workspaceStorage/*/chatSessions/`)

Override discovery with the `COPILOT_DB_PATH` environment variable (colon-separated list of paths).

## Requirements

- Python 3.10+
- No third-party dependencies (stdlib only)

## Running

From the project root:

```bash
python -m tokmeter_cli
```

Or run a specific script directly:

```bash
python tokmeter_cli/cli.py
```

To install as a command (optional):

```bash
pip install -e .
tokmeter
```

> **Note:** A `pyproject.toml` is not yet included. Until one is added, use `python -m tokmeter_cli`.

## Report sections

Each run produces a markdown document with seven sections:

| Section | Description |
|---|---|
| **Overview** | Grand totals: requests, sessions, tokens, cost, cache hit rate, data quality |
| **By Model** | Per-model breakdown of tokens and cost |
| **Top Sessions** | Sessions ranked by cost then tokens |
| **Recent Requests** | Individual request log (most recent first) |
| **Token Types** | Cost breakdown by token type from the embedded rate card |
| **Daily Timeseries** | Per-model daily totals |
| **Insights** | Run rate, projected monthly cost, cache savings, by-initiator breakdown |

## Flags

| Flag | Default | Description |
|---|---|---|
| `--days N` | all time | Restrict to the last N calendar days. `--days 1` means today only. |
| `--model MODEL` | all models | Filter to one model by exact name (e.g. `claude-sonnet-4.6`). |
| `--source SOURCE` | all sources | Restrict to `copilot-cli` or `vscode-chat`. |
| `--output FILE` | stdout | Write the markdown report to a file instead of printing it. `.md` extension is added automatically if absent. |
| `--limit-sessions N` | 25 | Maximum number of sessions shown in the Top Sessions table. |
| `--limit-requests N` | 50 | Maximum number of rows shown in the Recent Requests table. |
| `--no-vscode` | off | Skip scanning VS Code chat session journals. |
| `--version` | — | Print the version and exit. |

## Examples

```bash
# All-time report to stdout
python -m tokmeter_cli

# Last 7 days, written to a file
python -m tokmeter_cli --days 7 --output report.md

# Today only, Copilot CLI source only
python -m tokmeter_cli --days 1 --source copilot-cli

# Filter to a specific model, show up to 100 recent requests
python -m tokmeter_cli --model claude-sonnet-4.6 --limit-requests 100

# Skip VS Code scanning (faster if you only care about CLI usage)
python -m tokmeter_cli --no-vscode
```

## Environment variables

| Variable | Description |
|---|---|
| `COPILOT_DB_PATH` | Colon-separated list of explicit database paths. Disables auto-discovery and VS Code scanning. |
| `VSCODE_CHAT_PATH` | Colon-separated list of VS Code `workspaceStorage` root directories. Overrides the default search paths for chat session journals. |

## Project structure

```
tokmeter-cli/
└── tokmeter_cli/
    ├── __init__.py      version string
    ├── __main__.py      entry point for python -m tokmeter_cli
    ├── cli.py           argument parsing, query orchestration, output
    ├── db.py            SQLite discovery, snapshotting, and query engine
    ├── metrics.py       SQL fragment constants and row post-processor
    ├── pricing.py       published model rate table and cost helpers
    ├── reports.py       markdown table formatters for each section
    └── vscode.py        VS Code chat session journal parser
```
