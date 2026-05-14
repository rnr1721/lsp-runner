#!/usr/bin/env python3
"""
DepthNet LSP Runner v1.0 — Universal Language Server Protocol manager for AI agents.

Usage:
  lsp-runner start              # Auto-detect language, start server
  lsp-runner stop               # Stop server
  lsp-runner status             # Show status (running/stopped + language)
  lsp-runner language           # Print detected language
  lsp-runner request            # Accept JSON via stdin, return result

Configuration: languages.json (auto-detected next to this script)

Request format (stdin):
  {"method":"references","file":"/path/to/file","symbol":"Name","line":10,"character":5}

Supported methods: references, definition, hover, symbols, diagnostics
"""

import itertools
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = os.environ.get("LSP_RUNNER_CONFIG", str(SCRIPT_DIR / "languages.json"))
HOME = os.path.expanduser("~")

# Global request ID counter (survives across requests within same process)
_request_counter = itertools.count(1)


def load_config():
    """Load languages.json, merged with languages_custom.json if exists."""
    base_path = os.environ.get("LSP_RUNNER_CONFIG", str(SCRIPT_DIR / "languages.json"))
    custom_path = os.path.join(os.path.dirname(base_path), "languages_custom.json")

    if not os.path.exists(base_path):
        print(json.dumps({"error": f"Configuration file not found: {base_path}"}))
        sys.exit(1)

    with open(base_path) as f:
        config = json.load(f)

    if os.path.exists(custom_path):
        with open(custom_path) as f:
            custom = json.load(f)

        if "detection" in custom:
            existing = {e["language"]: e for e in config.get("detection", [])}
            for entry in custom["detection"]:
                existing[entry["language"]] = entry
            config["detection"] = list(existing.values())

        if "servers" in custom:
            config.setdefault("servers", {}).update(custom["servers"])

        if "settings" in custom:
            config.setdefault("settings", {}).update(custom["settings"])

    return config


def detect_language(config):
    """Auto-detect project language by marker files in home directory."""
    for entry in config.get("detection", []):
        marker = os.path.join(HOME, entry["marker"])
        if os.path.exists(marker):
            return entry
    return None


def get_server(config, language_id):
    """Get server configuration for a language."""
    return config.get("servers", {}).get(language_id)


def check_installed(server):
    """Check if LSP server is installed."""
    check = server.get("install_check", "")
    if not check:
        return True
    result = subprocess.run(check, shell=True, capture_output=True, text=True)
    return result.returncode == 0


def get_pid_file(config):
    """Get PID file path from config."""
    return config.get("settings", {}).get("pid_file", "/tmp/lsp-server.pid")


def get_lang_file(config):
    """Get language file path."""
    return os.path.join(os.path.dirname(get_pid_file(config)), "lsp-language")


def get_running_language(config):
    """Get language of currently running server."""
    lang_file = get_lang_file(config)
    if os.path.exists(lang_file):
        with open(lang_file) as f:
            return f.read().strip()
    return "unknown"


def is_running(config):
    """Check if server process is alive."""
    pid_file = get_pid_file(config)
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, FileNotFoundError, ValueError):
        if os.path.exists(pid_file):
            os.remove(pid_file)
        return False


def get_lsp_streams(config):
    """Reconnect to running LSP process via /proc filesystem."""
    pid_file = get_pid_file(config)
    if not os.path.exists(pid_file):
        return None, None

    with open(pid_file) as f:
        pid = int(f.read().strip())

    try:
        os.kill(pid, 0)  # Проверяем что жив
    except ProcessLookupError:
        return None, None

    try:
        stdin = open(f"/proc/{pid}/fd/0", "w")
        stdout = open(f"/proc/{pid}/fd/1", "rb")
        return stdin, stdout
    except (FileNotFoundError, PermissionError):
        return None, None


# ── JSON-RPC Transport ────────────────────────────────────────────────────


def start_lsp_process(server, config):
    """Start LSP server with proper stdin/stdout pipes and initialize handshake."""
    log_file = config.get("settings", {}).get("log_file", "/tmp/lsp-server.log")
    log = open(log_file, "w")

    proc = subprocess.Popen(
        server["command"] + server.get("args", []),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log,
        cwd=HOME,
        env={**os.environ, **server.get("env", {})},
    )

    # Initialize handshake
    init_request = {
        "jsonrpc": "2.0",
        "id": next(_request_counter),
        "method": "initialize",
        "params": {
            "processId": proc.pid,
            "rootUri": f"file://{HOME}",
            "capabilities": {
                "textDocument": {
                    "references": {},
                    "definition": {},
                    "hover": {},
                    "documentSymbol": {},
                    "publishDiagnostics": {},
                }
            },
        },
    }

    try:
        send_jsonrpc_to_proc(proc, init_request)
        response = read_jsonrpc_from_proc(proc, timeout=5)
    except Exception as e:
        proc.kill()
        raise RuntimeError(f"LSP initialize failed: {e}")

    # Send initialized notification
    initialized = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
    send_jsonrpc_to_proc(proc, initialized)

    return proc


def send_jsonrpc_to_proc(proc, request):
    """Send a JSON-RPC request with proper Content-Length header (UTF-8 safe)."""
    body = json.dumps(request, ensure_ascii=False)
    body_bytes = body.encode("utf-8")
    header = f"Content-Length: {len(body_bytes)}\r\n\r\n"
    proc.stdin.write(header.encode() + body_bytes)
    proc.stdin.flush()


def send_jsonrpc_to_fd(stdin_fd, request):
    """Send a JSON-RPC request to a file descriptor."""
    body = json.dumps(request, ensure_ascii=False)
    body_bytes = body.encode("utf-8")
    header = f"Content-Length: {len(body_bytes)}\r\n\r\n"
    stdin_fd.write(header + body.decode("utf-8"))
    stdin_fd.flush()


def read_jsonrpc_from_proc(proc, timeout=10):
    """Read a JSON-RPC response from subprocess with Content-Length framing."""
    return _read_jsonrpc(proc.stdout, timeout)


def read_jsonrpc_from_fd(stdout_fd, timeout=10):
    """Read a JSON-RPC response from file descriptor."""
    return _read_jsonrpc(stdout_fd, timeout)


def _read_jsonrpc(stream, timeout=10):
    """Core JSON-RPC reader — reads header + body."""
    header_bytes = b""
    deadline = time.time() + timeout

    while time.time() < deadline:
        ready, _, _ = select.select([stream], [], [], 0.1)
        if not ready:
            continue

        chunk = stream.read(1)
        if not chunk:
            raise RuntimeError("LSP server closed stdout")

        header_bytes += chunk
        if b"\r\n\r\n" in header_bytes:
            break

    if b"\r\n\r\n" not in header_bytes:
        raise RuntimeError("Timeout waiting for LSP response header")

    header_str = header_bytes.decode("utf-8", errors="replace")
    match = re.search(r"Content-Length:\s*(\d+)", header_str, re.IGNORECASE)

    if not match:
        raise RuntimeError(f"Invalid LSP response header: {header_str}")

    content_length = int(match.group(1))
    body_bytes = b""

    while len(body_bytes) < content_length:
        chunk = stream.read(content_length - len(body_bytes))
        if not chunk:
            raise RuntimeError("LSP server closed stdout mid-response")
        body_bytes += chunk

    return json.loads(body_bytes.decode("utf-8", errors="replace"))


# ── LSP Request Methods ───────────────────────────────────────────────────


def build_lsp_request(method, file_path, symbol, line, character, language_id):
    """Build a JSON-RPC request for the LSP server."""
    uri = f"file://{file_path}" if file_path else "file:///"
    position = {"line": max(0, line - 1), "character": max(0, character - 1)}
    request_id = next(_request_counter)

    if method == "references":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/references",
            "params": {
                "textDocument": {"uri": uri},
                "position": position,
                "context": {"includeDeclaration": True},
            },
        }

    elif method == "definition":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/definition",
            "params": {"textDocument": {"uri": uri}, "position": position},
        }

    elif method == "hover":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/hover",
            "params": {"textDocument": {"uri": uri}, "position": position},
        }

    elif method == "symbols":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/documentSymbol",
            "params": {"textDocument": {"uri": uri}},
        }

    elif method == "diagnostics":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": "",
                }
            },
        }

    return None


def parse_lsp_response(method, response, max_results):
    """Parse LSP response into normalized format."""
    if "error" in response:
        return {"error": str(response["error"])}

    result = response.get("result")
    if result is None:
        return {"results": []}

    if method == "references":
        locations = result if isinstance(result, list) else []
        return {
            "results": [
                {
                    "file": loc["uri"].replace("file://", ""),
                    "line": loc["range"]["start"]["line"] + 1,
                    "col": loc["range"]["start"]["character"] + 1,
                }
                for loc in locations[:max_results]
            ]
        }

    elif method == "definition":
        if isinstance(result, list) and len(result) > 0:
            loc = result[0]
        elif isinstance(result, dict) and "uri" in result:
            loc = result
        else:
            return {"results": []}

        return {
            "results": [
                {
                    "file": loc["uri"].replace("file://", ""),
                    "line": loc["range"]["start"]["line"] + 1,
                    "col": loc["range"]["start"]["character"] + 1,
                }
            ]
        }

    elif method == "hover":
        contents = result.get("contents", {})
        if isinstance(contents, dict):
            text = contents.get("value", str(contents))
        elif isinstance(contents, list):
            text = "\n".join(
                c.get("value", str(c)) if isinstance(c, dict) else str(c)
                for c in contents
            )
        else:
            text = str(contents)
        return {"hover": text[:1000]}

    elif method == "symbols":
        symbols = result if isinstance(result, list) else []
        return {
            "results": [
                {
                    "file": "",
                    "line": s.get("range", {}).get("start", {}).get("line", 0) + 1,
                    "col": s.get("range", {}).get("start", {}).get("character", 0) + 1,
                    "context": s.get("name", ""),
                }
                for s in symbols[:max_results]
            ]
        }

    elif method == "diagnostics":
        return {"results": [], "note": "Diagnostics published asynchronously."}

    return {"results": []}


# ── Commands ──────────────────────────────────────────────────────────────


def cmd_start(config):
    """Start the LSP server."""
    settings = config.get("settings", {})
    pid_file = get_pid_file(config)

    if is_running(config):
        lang = get_running_language(config)
        with open(pid_file) as f:
            pid = f.read().strip()
        print(f"LSP server already running (PID: {pid}, language: {lang})")
        return

    for f in [pid_file, get_lang_file(config)]:
        if os.path.exists(f):
            os.remove(f)

    detected = detect_language(config)
    if not detected:
        print("ERROR: No project detected.")
        markers = ", ".join(e["marker"] for e in config.get("detection", []))
        print(f"Supported markers: {markers}")
        sys.exit(1)

    server = get_server(config, detected["language"])
    if not server:
        print(f"ERROR: No LSP server configured for language '{detected['language']}'.")
        sys.exit(1)

    if not check_installed(server):
        hint = server.get("install_hint", "install it manually")
        print(f"ERROR: LSP server not found for {detected['label']}.")
        print(f"Install hint: {hint}")
        sys.exit(1)

    try:
        proc = start_lsp_process(server, config)
    except RuntimeError as e:
        print(f"ERROR: Failed to start LSP server: {e}")
        sys.exit(1)

    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
    with open(get_lang_file(config), "w") as f:
        f.write(detected["language"])

    print(
        f"LSP server started (PID: {proc.pid}, language: {detected['language']}, label: {detected['label']})"
    )


def cmd_stop(config):
    """Stop the LSP server."""
    pid_file = get_pid_file(config)

    if not os.path.exists(pid_file):
        print("LSP server is not running.")
        return

    with open(pid_file) as f:
        pid = int(f.read().strip())

    # Try graceful shutdown via /proc
    try:
        stdin, stdout = get_lsp_streams(config)
        if stdin and stdout:
            shutdown = {
                "jsonrpc": "2.0",
                "id": next(_request_counter),
                "method": "shutdown",
                "params": {},
            }
            send_jsonrpc_to_fd(stdin, shutdown)
            exit_notif = {"jsonrpc": "2.0", "method": "exit", "params": {}}
            send_jsonrpc_to_fd(stdin, exit_notif)
            stdin.close()
            stdout.close()
    except Exception:
        pass

    # Force kill if still running
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"LSP server stopped (PID: {pid})")
    except ProcessLookupError:
        print("LSP server was not running (stale PID file)")

    for f in [pid_file, get_lang_file(config)]:
        if os.path.exists(f):
            os.remove(f)


def cmd_status(config):
    """Show server status."""
    if is_running(config):
        lang = get_running_language(config)
        pid_file = get_pid_file(config)
        with open(pid_file) as f:
            pid = f.read().strip()
        print(f"running (PID: {pid}, language: {lang})")
    else:
        print("stopped")


def cmd_language(config):
    """Print detected language."""
    if is_running(config):
        print(get_running_language(config))
    else:
        detected = detect_language(config)
        if detected:
            print(detected["language"])
        else:
            print("unknown")


def cmd_request(config):
    """Handle a JSON request from stdin."""
    if not is_running(config):
        result = {
            "error": "LSP server is not running. Start it with 'lsp-runner start'."
        }
        print(json.dumps(result))
        return

    try:
        request = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        result = {"error": f"Invalid JSON: {e}"}
        print(json.dumps(result))
        return

    method = request.get("method", "")
    file_path = request.get("file", "")
    symbol = request.get("symbol", "")
    line = request.get("line", 0)
    character = request.get("character", request.get("col", request.get("char", 0)))
    max_results = request.get(
        "max_results", config.get("settings", {}).get("max_results", 20)
    )
    language_id = get_running_language(config)

    lsp_request = build_lsp_request(
        method, file_path, symbol, line, character, language_id
    )

    if lsp_request is None:
        result = {"error": f"Unsupported method: {method}"}
        print(json.dumps(result))
        return

    # Reconnect to running process
    stdin, stdout = get_lsp_streams(config)
    if stdin is None or stdout is None:
        result = {
            "error": "Cannot connect to LSP server process. Try restarting with 'lsp-runner start'."
        }
        print(json.dumps(result))
        return

    try:
        send_jsonrpc_to_fd(stdin, lsp_request)
        response = read_jsonrpc_from_fd(
            stdout,
            timeout=config.get("settings", {}).get("request_timeout_ms", 10000) / 1000,
        )
        result = parse_lsp_response(method, response, max_results)
    except Exception as e:
        result = {"error": f"LSP request failed: {e}"}
    finally:
        try:
            stdin.close()
            stdout.close()
        except Exception:
            pass

    print(json.dumps(result, ensure_ascii=False))


# ── Main ──────────────────────────────────────────────────────────────────


def print_usage():
    """Print usage information."""
    print("Usage: lsp-runner {start|stop|status|language|request}")
    print()
    print("Commands:")
    print("  start     Auto-detect language and start LSP server")
    print("  stop      Stop the LSP server")
    print("  status    Show server status")
    print("  language  Print detected language")
    print("  request   Accept JSON via stdin and return LSP result")
    print()
    print("Request format (stdin):")
    print(
        '  {"method":"references","file":"/path/to/file","symbol":"Name","line":10,"character":5}'
    )
    print()
    print("Supported methods: references, definition, hover, symbols, diagnostics")


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command = sys.argv[1]
    config = load_config()

    if command == "start":
        cmd_start(config)
    elif command == "stop":
        cmd_stop(config)
    elif command == "status":
        cmd_status(config)
    elif command == "language":
        cmd_language(config)
    elif command == "request":
        cmd_request(config)
    elif command in ("-h", "--help", "help"):
        print_usage()
    else:
        print(f"Unknown command: {command}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
