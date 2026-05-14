# lsp-runner

Universal LSP server manager for AI agents running in sandboxed environments.

Auto-detects project language and launches the appropriate Language Server
Protocol server. Provides a simple CLI for lifecycle management and code
intelligence queries via JSON-RPC.

## Quick Start

```bash
# Install
curl -sSL https://raw.githubusercontent.com/rnr1721/lsp-runner/main/install.sh | bash

# Start LSP server (auto-detects language)
lsp-runner start

# Check status
lsp-runner status

# Query code intelligence
echo '{"method":"references","file":"/home/sandbox-user/app/Models/User.php","symbol":"User","line":15,"character":8}' | lsp-runner request
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
| `lsp-runner start` | Auto-detect language and start LSP server |
| `lsp-runner stop` | Stop the LSP server |
| `lsp-runner status` | Show status (running/stopped + language) |
| `lsp-runner language` | Print detected language (php, go, typescript...) |
| `lsp-runner request` | Accept JSON via stdin, return LSP result |

### Request format (stdin)

```json
{
  "method": "references",
  "file": "/home/sandbox-user/app/Models/User.php",
  "symbol": "User",
  "line": 15,
  "character": 8,
  "max_results": 20
}
```

### Supported methods

| Method | Description | Required fields |
|---|---|---|
| `references` | Find all references to a symbol | `file`, `symbol`, `line`, `character` |
| `definition` | Go to definition of a symbol | `file`, `symbol`, `line`, `character` |
| `hover` | Get hover information (type, docs) | `file`, `symbol`, `line`, `character` |
| `symbols` | List symbols in a file | `file` |
| `diagnostics` | Get diagnostics for a file | `file` |

### Response format

```json
{
  "results": [
    {
      "file": "/home/sandbox-user/app/Http/Controllers/UserController.php",
      "line": 15,
      "col": 8
    }
  ]
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

`languages_custom.json` is merged with `languages.json` at runtime.
Custom values override defaults. The file is gitignored — safe for local changes.

## Requirements

- Python 3.7+
- Appropriate LSP server for your language (see install hints)

## Architecture

```
lsp-runner start    → Detects language → Starts LSP server → Saves PID
lsp-runner request  → Reads PID → Connects via /proc/{pid}/fd/* → Sends JSON-RPC → Returns result
lsp-runner stop     → Sends shutdown → Kills process → Cleans up
```

Requests are sent with proper `Content-Length` framing as required by the
Language Server Protocol specification. Responses are parsed and normalized
into a simple JSON format for easy consumption by AI agents.

## License

MIT
