# lsp-runner

Universal LSP server manager for AI agents running in sandboxed environments.

Auto-detects project language and launches the appropriate Language Server
Protocol server. Runs as a background daemon with a Unix socket broker for
concurrent code intelligence queries via JSON-RPC.

## Quick Start

```bash
# Install
curl -sSL https://raw.githubusercontent.com/rnr1721/lsp-runner/main/install.sh | bash

# Start LSP server (auto-detects language, runs as daemon)
lsp-runner start

# Check status
lsp-runner status

# Query code intelligence
echo '{"method":"references","file":"/home/user/app/Models/User.php","symbol":"User","line":15,"character":8}' | lsp-runner request

# Get diagnostics
echo '{"method":"diagnostics","file":"/home/user/app/Models/User.php"}' | lsp-runner request
```

## Installation

### Via install script

```bash
curl -sSL https://raw.githubusercontent.com/rnr1721/lsp-runner/main/install.sh | bash
```

### Manual

```bash
git clone https://github.com/rnr1721/lsp-runner.git /usr/local/lib/lsp-runner
ln -s /usr/local/lib/lsp-runner/lsp_runner.py /usr/local/bin/lsp-runner
chmod +x /usr/local/bin/lsp-runner
```

### Docker sandbox

```dockerfile
RUN git clone --branch main https://github.com/rnr1721/lsp-runner.git /usr/local/lib/lsp-runner \
    && printf '#!/bin/bash\nexec python3 /usr/local/lib/lsp-runner/lsp_runner.py "$@"\n' > /usr/local/bin/lsp-runner \
    && chmod +x /usr/local/bin/lsp-runner
```

## Usage

| Command | Description |
|---|---|
| `lsp-runner start` | Auto-detect language, daemonize, start LSP server |
| `lsp-runner stop` | Graceful shutdown with SIGTERM/SIGKILL escalation |
| `lsp-runner status` | Show status (running/stopped + language + PID) |
| `lsp-runner language` | Print detected language (go, php, typescript, python...) |
| `lsp-runner request` | Accept JSON via stdin, return LSP result |

### Request format (stdin)

```json
{
  "method": "references",
  "file": "/home/user/app/Models/User.php",
  "symbol": "User",
  "line": 15,
  "character": 8,
  "max_results": 20
}
```

Fields `line` and `character` must be positive integers (1-based).

### Supported methods

| Method | Description | Required fields |
|---|---|---|
| `references` | Find all references to a symbol | `file`, `line`, `character` |
| `definition` | Go to definition of a symbol | `file`, `line`, `character` |
| `hover` | Get hover information (type, docs) | `file`, `line`, `character` |
| `symbols` | List document symbols in a file | `file` |
| `diagnostics` | Get buffered diagnostics for file(s) | `file` (optional) |

For `diagnostics`:
- With `file`: returns diagnostics for that file only
- Without `file`: returns all cached diagnostics grouped by URI

### Response format

#### References / Definition / Symbols
```json
{
  "results": [
    {
      "file": "/home/user/app/Http/Controllers/UserController.php",
      "line": 15,
      "col": 8,
      "context": "UserController"
    }
  ]
}
```

#### Hover
```json
{
  "hover": "class User extends Model\n\nRepresents an application user"
}
```

#### Diagnostics
```json
{
  "results": [
    {
      "uri": "file:///home/user/app/Models/User.php",
      "range": {"start": {"line": 14, "character": 0}, "end": {"line": 14, "character": 4}},
      "severity": 2,
      "message": "Undefined variable '$usr'"
    }
  ]
}
```

#### Error
```json
{
  "error": "LSP server is not running. Start it with 'lsp-runner start'."
}
```

## Supported Languages

| Language | Marker File | LSP Server |
|---|---|---|
| Go | `go.mod` | gopls |
| PHP | `composer.json` | phpactor |
| TypeScript/JavaScript | `package.json` | typescript-language-server |
| Rust | `Cargo.toml` | rust-analyzer |
| Python | `pyproject.toml` / `requirements.txt` | pyright-langserver |
| Ruby | `Gemfile` | solargraph |

## Configuration

Edit `languages.json` to add new languages or change server settings:

```json
{
  "detection": [
    { "marker": "go.mod", "language": "go", "label": "Go", "icon": "🔵" }
  ],
  "servers": {
    "go": {
      "command": ["gopls"],
      "args": [],
      "env": {},
      "install_check": "which gopls",
      "install_hint": "go install golang.org/x/tools/gopls@latest"
    }
  },
  "settings": {
    "max_results": 20,
    "request_timeout_ms": 10000,
    "pid_file": "/tmp/lsp-server.pid",
    "log_file": "/tmp/lsp-server.log"
  }
}
```

### Custom configuration

To override server settings without modifying the default `languages.json`,
create a `languages_custom.json` file in the same directory:

```json
{
  "servers": {
    "php": {
      "command": ["phpactor", "language-server"],
      "install_check": "which phpactor",
      "install_hint": "composer global require phpactor/phpactor"
    }
  },
  "settings": {
    "max_results": 30,
    "pid_file": "/tmp/my-lsp.pid"
  }
}
```

`languages_custom.json` is deep-merged with `languages.json` at runtime.
Custom values override defaults. The file is gitignored — safe for local changes.

## Architecture (v2.1)

```
AI agent -> lsp-runner request -> Unix Socket (/tmp/lsp.sock) -> LSPBroker -V
 lsp-runner start -> Daemon (forkx2) -> Language Detector -> LSP Process (gopls,phpactor…)
```

### Key design decisions

**Daemon mode.** `lsp-runner start` double-forks into a background daemon.
The LSP server survives terminal sessions and runs until explicitly stopped
with `lsp-runner stop` or the machine shuts down. Language detection is
cached for 1 hour to make restarts instant.

**Unix socket broker.** Requests from AI agents flow through a Unix domain
socket (`/tmp/lsp.sock`) to a broker thread. The broker assigns internal
JSON-RPC IDs to avoid collisions between concurrent clients. A central
dispatcher thread reads LSP responses and routes them back to waiting
clients by ID — no polling, no races.

**Concurrent request handling.** Multiple clients can query the LSP server
simultaneously. Each client request gets its own thread on the broker side
and its own response queue on the dispatcher side. ID remapping ensures
responses always reach the correct client even under load.

**Notification buffering.** LSP diagnostics arrive asynchronously via
`textDocument/publishDiagnostics` notifications. The broker buffers the
latest diagnostics per file and serves them on demand through the
`diagnostics` method — no need to wait or poll.

**Graceful shutdown.** `lsp-runner stop` sends a `shutdown` request through
the socket, waits 2 seconds, escalates to SIGTERM, waits 0.5 seconds, then
SIGKILL. PID verification before SIGKILL prevents accidentally killing a
reused PID that no longer belongs to the LSP server.

**Safe project scanning.** Language detection scans project directories but
prunes heavy subtrees: `node_modules`, `vendor`, `.git`, virtualenvs,
build directories, and IDE folders. Maximum scan depth is 3 levels from
the home directory root.

**Crash resilience.** If the LSP process dies unexpectedly, the daemon
detects it, cleans up PID and socket files, and exits. The next
`lsp-runner start` will spawn a fresh instance.

## Requirements

- Python 3.7+
- Linux with `/proc` filesystem (for PID verification)
- Appropriate LSP server for your language (see install hints)

## Troubleshooting

### Server won't start

Check the log file:
```bash
cat /tmp/lsp-server.log
```

The session header (`=== LSP runner session started at … ===`) marks each
start attempt. Look for errors after that marker.

### "No project detected"

Ensure you're in a directory with a recognized marker file:
```bash
ls ~/go.mod ~/composer.json ~/package.json ~/Cargo.toml ~/pyproject.toml ~/Gemfile 2>/dev/null
```

### "LSP server not found"

Install the required LSP server:
```bash
# Go
go install golang.org/x/tools/gopls@latest

# TypeScript
npm install -g typescript-language-server typescript

# Python
pip install pyright

# PHP
composer global require phpactor/phpactor

# Rust
rustup component add rust-analyzer

# Ruby
gem install solargraph
```

### Stale PID files

If `lsp-runner status` shows "running" but the server is dead:
```bash
lsp-runner stop   # Cleans up stale files
lsp-runner start  # Fresh start
```

## License

MIT