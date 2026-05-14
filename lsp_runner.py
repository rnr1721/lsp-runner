#!/usr/bin/env python3
"""
DepthNet LSP Runner v2.1 — Universal Language Server Protocol manager for AI agents.

Usage:
  lsp-runner start              # Auto-detect language, start server
  lsp-runner stop               # Stop server
  lsp-runner status             # Show status (running/stopped + language)
  lsp-runner language           # Print detected language
  lsp-runner request            # Accept JSON via stdin, return result

Configuration: languages.json (auto-detected next to this script)
Custom overrides: languages_custom.json (merged at runtime)

Request format (stdin):
  {"method":"references","file":"/path/to/file","symbol":"Name","line":10,"character":5}

Supported methods: references, definition, hover, symbols, diagnostics

Changes in v2.1:
- Fixed ID collision between clients: broker now remaps client IDs
  to internal IDs and restores them in responses.
- Fixed duplicate LSPStdoutReader: the reader used for initialize
  is now reused inside LSPBroker (no lost bytes between handshake
  and broker startup).
- Fixed PID reuse risk in `stop`: verify process before kill, escalate
  SIGTERM -> SIGKILL only if the PID still looks like our LSP.
- Limited project scan: skip heavy/hidden directories
  (node_modules, vendor, .git, .cache, etc.) and prune properly.
- Tightened socket permissions to owner-only (0o600).
- Log file opened in append mode, with a session header marker.
- Replaced bare `except:` with specific exception types.
- `diagnostics/get` method served by the broker (the previous
  `didOpen`-as-diagnostics request was misleading and removed).
- Input validation in `cmd_request` (line/character must be >= 1).
- Initialize loop tolerates pre-init server notifications.
"""

import itertools
import json
import os
import queue
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
SOCKET_PATH = "/tmp/lsp.sock"
_request_counter = itertools.count(1)

# Directories we never want to descend into during project scanning.
_SCAN_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        ".git",
        ".hg",
        ".svn",
        ".cache",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".idea",
        ".vscode",
        ".next",
        ".nuxt",
        "bower_components",
    }
)


# ── Configuration ──────────────────────────────────────────────────────


def load_config():
    """Load languages.json, merged with languages_custom.json if exists."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_path = os.environ.get(
        "LSP_RUNNER_CONFIG", os.path.join(script_dir, "languages.json")
    )
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


# ── Language Detection ─────────────────────────────────────────────────


class LanguageDetector:
    """Cached language detection for faster startup."""

    def __init__(self, config):
        self.config = config
        self._cache_file = os.path.join(
            os.path.dirname(get_pid_file(config)), ".lsp_detect_cache"
        )

    def detect(self):
        """Detect language with caching."""
        cached = self._read_cache()
        if cached and self._validate_cache(cached):
            return cached

        detected = self._scan_project()

        if detected:
            self._write_cache(detected)

        return detected

    def _read_cache(self):
        try:
            if os.path.exists(self._cache_file):
                with open(self._cache_file) as f:
                    data = json.load(f)
                    if time.time() - data.get("timestamp", 0) < 3600:
                        return data.get("language")
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return None

    def _validate_cache(self, language_entry):
        """Validate cached language still exists and has a server."""
        marker = os.path.join(HOME, language_entry["marker"])
        if not os.path.exists(marker):
            return False
        return get_server(self.config, language_entry["language"]) is not None

    def _scan_project(self):
        """Scan project for language markers with early exit and pruning."""
        # First: cheap check at HOME root.
        for entry in self.config.get("detection", []):
            marker = os.path.join(HOME, entry["marker"])
            if os.path.exists(marker):
                return entry

        # Then: limited deep scan, skipping heavy directories.
        markers_by_name = {
            entry["marker"]: entry for entry in self.config.get("detection", [])
        }

        for root, dirs, files in os.walk(HOME, topdown=True):
            depth = root[len(HOME) :].count(os.sep)
            if depth > 3:
                dirs.clear()
                continue

            # Prune in-place — requires topdown=True.
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and d not in _SCAN_SKIP_DIRS
            ]

            for fname in files:
                if fname in markers_by_name:
                    return markers_by_name[fname]

        return None

    def _write_cache(self, language_entry):
        try:
            os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
            with open(self._cache_file, "w") as f:
                json.dump({"language": language_entry, "timestamp": time.time()}, f)
        except OSError:
            pass


def get_server(config, language_id):
    return config.get("servers", {}).get(language_id)


def check_installed(server):
    check = server.get("install_check", "")
    if not check:
        return True
    result = subprocess.run(check, shell=True, capture_output=True, text=True)
    return result.returncode == 0


def get_pid_file(config):
    return config.get("settings", {}).get("pid_file", "/tmp/lsp-server.pid")


def get_lang_file(config):
    return os.path.join(os.path.dirname(get_pid_file(config)), "lsp-language")


def get_running_language(config):
    lang_file = get_lang_file(config)
    if os.path.exists(lang_file):
        with open(lang_file) as f:
            return f.read().strip()
    return "unknown"


def is_running(config):
    """Check if LSP server is alive (via socket)."""
    if not os.path.exists(SOCKET_PATH):
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(SOCKET_PATH)
        sock.close()
        return True
    except (socket.error, FileNotFoundError):
        return False


def _proc_is_lsp_runner(pid):
    """Verify a PID still looks like an LSP server we spawned.

    Conservative: returns False if we can't confirm. Used to avoid
    killing an unrelated process that reused the PID after our LSP
    server already died.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return False

    # Refuse to kill anything that looks like the runner script itself —
    # we only want to kill the LSP server process it spawned.
    if "lsp-runner" in cmdline or "lsp_runner" in cmdline:
        return False
    return True


# ── JSON-RPC Transport ─────────────────────────────────────────────────


def encode_jsonrpc_message(msg):
    """Encode JSON-RPC message with Content-Length header."""
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    return header + body


def send_jsonrpc_to_proc(proc, request):
    """Send JSON-RPC message to process stdin."""
    proc.stdin.write(encode_jsonrpc_message(request))
    proc.stdin.flush()


# ── LSP Stdout Reader ─────────────────────────────────────────────────


class LSPStdoutReader:
    """Reader for LSP process stdout.

    Designed to be driven by a single thread (the dispatcher).
    No internal locking — concurrency is enforced by the dispatcher
    contract: nobody else calls read_message.
    """

    def __init__(self, stream):
        self._stream = stream
        self._buffer = b""
        self._running = True

    def read_message(self, timeout=None):
        deadline = time.time() + timeout if timeout is not None else None

        while self._running:
            msg = self._parse_from_buffer()
            if msg is not None:
                return msg

            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("Timeout waiting for LSP message")
                wait = min(0.5, remaining)
            else:
                wait = 0.5

            ready, _, _ = select.select([self._stream], [], [], wait)
            if not ready:
                continue

            try:
                # read1 returns whatever is currently available without
                # waiting for the full requested byte count. Plain read()
                # on a pipe blocks until N bytes arrive OR EOF, which
                # deadlocks the dispatcher when the LSP sends small
                # messages.
                chunk = self._stream.read1(4096)
            except (OSError, ValueError):
                raise RuntimeError("LSP stream closed unexpectedly")

            if not chunk:
                raise RuntimeError("LSP stream closed")
            self._buffer += chunk

        raise RuntimeError("Reader stopped")

    def _has_complete_message(self):
        return b"\r\n\r\n" in self._buffer and b"Content-Length:" in self._buffer

    def _parse_from_buffer(self):
        if not self._has_complete_message():
            return None

        header_end = self._buffer.index(b"\r\n\r\n")
        header_str = self._buffer[:header_end].decode("utf-8", errors="replace")
        match = re.search(r"Content-Length:\s*(\d+)", header_str, re.IGNORECASE)

        if not match:
            # Malformed header — drop and try to recover.
            self._buffer = self._buffer[header_end + 4 :]
            return None

        content_length = int(match.group(1))
        body_start = header_end + 4
        total_needed = body_start + content_length

        if len(self._buffer) < total_needed:
            return None

        body_bytes = self._buffer[body_start:total_needed]
        self._buffer = self._buffer[total_needed:]

        try:
            return json.loads(body_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

    def stop(self):
        self._running = False


# ── LSP Dispatcher ────────────────────────────────────────────────────


class LSPDispatcher:
    """Routes LSP responses to waiting requesters, dispatches notifications.

    Owns the LSPStdoutReader; nothing else reads from it.
    """

    def __init__(self, stdout_reader):
        self._reader = stdout_reader
        self._pending = {}  # internal_id -> queue
        self._lock = threading.Lock()
        self._dispatcher_thread = None
        self._running = False
        self._notification_handlers = {}

    def start(self):
        self._running = True
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True
        )
        self._dispatcher_thread.start()

    def stop(self):
        self._running = False
        self._reader.stop()

    def send_request(self, request, proc):
        """Submit a request whose `id` was already assigned by the broker."""
        request_id = request["id"]
        response_queue = queue.Queue()

        with self._lock:
            self._pending[request_id] = response_queue

        send_jsonrpc_to_proc(proc, request)
        return response_queue

    def cancel(self, internal_id):
        """Drop a pending registration (e.g. on client-side timeout)."""
        with self._lock:
            self._pending.pop(internal_id, None)

    def on_notification(self, method, handler):
        self._notification_handlers[method] = handler

    def _dispatch_loop(self):
        while self._running:
            try:
                msg = self._reader.read_message(timeout=0.5)
            except TimeoutError:
                continue
            except RuntimeError:
                break
            except Exception:
                # Defensive: parsing accident shouldn't kill the loop.
                continue

            msg_id = msg.get("id")

            if msg_id is None:
                # Server -> client notification.
                if "method" in msg:
                    self._handle_notification(msg)
            else:
                with self._lock:
                    response_queue = self._pending.pop(msg_id, None)
                if response_queue:
                    response_queue.put(msg)
                # Orphan responses silently dropped.

    def _handle_notification(self, notification):
        method = notification.get("method", "")
        handler = self._notification_handlers.get(method)
        if handler:
            try:
                handler(notification.get("params", {}))
            except Exception:
                pass


# ── LSP Broker ────────────────────────────────────────────────────────


class LSPBroker:
    """Socket broker that multiplexes client requests onto the LSP server.

    Assigns its own internal IDs to outgoing LSP requests so concurrent
    clients cannot collide on JSON-RPC IDs. On response, restores the
    client's original ID before forwarding.
    """

    def __init__(self, proc, config, stdout_reader):
        self.proc = proc
        self.config = config

        # Reuse the reader from the initialize handshake — DO NOT create
        # a second reader on the same pipe.
        self.stdout_reader = stdout_reader
        self.dispatcher = LSPDispatcher(self.stdout_reader)

        self.dispatcher.on_notification(
            "textDocument/publishDiagnostics", self._handle_diagnostics
        )
        self.dispatcher.on_notification("$/progress", self._handle_progress)

        self.dispatcher.start()

        self.socket = self._create_socket()
        self.running = True

        self._diagnostics = {}
        self._diagnostics_lock = threading.Lock()

        # Track files we've already announced to the server via didOpen.
        # Stateful LSP servers (intelephense, pyright) refuse to answer
        # requests about documents they don't consider "open".
        self._open_docs = set()
        self._open_docs_lock = threading.Lock()

    def _create_socket(self):
        if os.path.exists(SOCKET_PATH):
            try:
                os.remove(SOCKET_PATH)
            except OSError:
                pass

        old_umask = os.umask(0o077)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(SOCKET_PATH)
            os.chmod(SOCKET_PATH, 0o600)
        finally:
            os.umask(old_umask)

        sock.listen(5)
        sock.settimeout(0.5)
        return sock

    def run(self):
        while self.running and self.proc.poll() is None:
            try:
                conn, _ = self.socket.accept()
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                break

            thread = threading.Thread(
                target=self._handle_client, args=(conn,), daemon=True
            )
            thread.start()

        self.stop()

    def _handle_client(self, conn):
        """Serve one client connection.

        Reads JSON-RPC requests in a loop until the client closes the
        socket. Each request is dispatched in its own worker thread so
        pipelined requests on the same connection don't block each other.
        A write-lock serializes responses on the wire — otherwise two
        workers could interleave bytes of separate responses.
        """
        write_lock = threading.Lock()
        inflight = []  # worker threads we spawned, for clean shutdown

        def send_response(response):
            try:
                with write_lock:
                    conn.sendall(encode_jsonrpc_message(response))
            except (OSError, BrokenPipeError):
                pass  # Client went away; nothing we can do.

        def serve_one(request):
            client_id = request.get("id")
            method = request.get("method", "")
            internal_id = None

            try:
                # --- Broker-local methods (not forwarded to LSP server) ---
                if method == "shutdown":
                    self.running = False
                    send_response(
                        {
                            "jsonrpc": "2.0",
                            "id": client_id,
                            "result": None,
                        }
                    )
                    return

                if method == "diagnostics/get":
                    params = request.get("params") or {}
                    uri = params.get("uri")
                    send_response(
                        {
                            "jsonrpc": "2.0",
                            "id": client_id,
                            "result": self.get_diagnostics(uri),
                        }
                    )
                    return

                # --- Forwarded LSP requests: remap ID ---
                internal_id = next(_request_counter)
                forwarded = dict(request)
                forwarded["id"] = internal_id

                # For file-targeted requests, make sure the server knows
                # about the document. Stateful servers (intelephense,
                # pyright) return empty results for unopened files.
                params = forwarded.get("params") or {}
                td = params.get("textDocument") or {}
                uri = td.get("uri", "")
                if uri.startswith("file://"):
                    self.ensure_doc_open(uri[len("file://") :])

                response_queue = self.dispatcher.send_request(forwarded, self.proc)

                try:
                    timeout = (
                        self.config.get("settings", {}).get("request_timeout_ms", 10000)
                        / 1000
                    )
                    response = response_queue.get(timeout=timeout)
                    response["id"] = client_id  # restore client's ID
                    send_response(response)
                except queue.Empty:
                    self.dispatcher.cancel(internal_id)
                    send_response(
                        {
                            "jsonrpc": "2.0",
                            "id": client_id,
                            "error": {"code": -1, "message": "Request timeout"},
                        }
                    )
            except Exception as e:
                if internal_id is not None:
                    self.dispatcher.cancel(internal_id)
                send_response(
                    {
                        "jsonrpc": "2.0",
                        "id": client_id,
                        "error": {"code": -1, "message": str(e)},
                    }
                )

        try:
            # Persistent loop: keep reading messages until client closes.
            reader_buf = bytearray()
            # Long-ish per-request timeout; the loop itself is bounded by
            # the client closing the socket.
            conn.settimeout(60)

            while self.running:
                request = self._read_one_from_buffer(conn, reader_buf)
                if request is None:
                    break  # client closed cleanly

                worker = threading.Thread(
                    target=serve_one, args=(request,), daemon=True
                )
                worker.start()
                inflight.append(worker)

                # Reap finished workers to keep the list bounded.
                inflight[:] = [t for t in inflight if t.is_alive()]
        except (socket.timeout, OSError):
            pass
        except Exception:
            pass
        finally:
            # Don't close the socket until in-flight workers have written
            # their responses, otherwise their sendall() will EPIPE.
            for t in inflight:
                t.join(timeout=5)
            try:
                conn.close()
            except Exception:
                pass

    def _read_one_from_buffer(self, conn, buf):
        """Read exactly one JSON-RPC message from `conn`, using `buf` as
        the carry-over buffer between calls.

        Returns the parsed message, or None if the client closed the
        connection cleanly (EOF before any new bytes).
        """
        while True:
            # Try to parse from what we already have.
            if b"\r\n\r\n" in buf:
                header_end = buf.index(b"\r\n\r\n")
                header_str = bytes(buf[:header_end]).decode("utf-8", errors="replace")
                match = re.search(r"Content-Length:\s*(\d+)", header_str, re.IGNORECASE)
                if not match:
                    # Bad header — drop it and try to recover.
                    del buf[: header_end + 4]
                    continue
                content_length = int(match.group(1))
                body_start = header_end + 4
                total_needed = body_start + content_length
                if len(buf) >= total_needed:
                    body = bytes(buf[body_start:total_needed])
                    del buf[:total_needed]
                    try:
                        return json.loads(body.decode("utf-8"))
                    except json.JSONDecodeError as e:
                        raise RuntimeError(f"Malformed JSON body: {e}")

            # Need more bytes.
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                # Idle keep-alive timeout. Tell caller to give up on
                # this connection.
                return None
            if not chunk:
                # EOF. If we have a partial frame, that's a protocol
                # error; if buf is empty, it's a clean close.
                if buf:
                    raise RuntimeError("Connection closed mid-message")
                return None
            buf.extend(chunk)

    def _handle_diagnostics(self, params):
        uri = params.get("uri", "")
        with self._diagnostics_lock:
            self._diagnostics[uri] = params.get("diagnostics", [])

    def _handle_progress(self, params):
        pass

    # ── didOpen tracking ──────────────────────────────────────────────

    # Per-language IDs the LSP spec recognises. The runner's detected
    # language name (from languages.json) maps to one of these.
    _LSP_LANGUAGE_IDS = {
        "python": "python",
        "php": "php",
        "javascript": "javascript",
        "typescript": "typescript",
        "go": "go",
        "rust": "rust",
        "ruby": "ruby",
        "java": "java",
        "c": "c",
        "cpp": "cpp",
        "csharp": "csharp",
        # extend as needed; languages.json can also override via
        # `lsp_language_id` on the server entry.
    }

    def _lsp_language_id_for(self, file_path):
        """Pick the languageId for didOpen. Order of preference:
        1. Server entry override (`lsp_language_id` in languages.json).
        2. File extension mapping.
        3. The detected project language.
        """
        servers = self.config.get("servers", {})
        # The runner only manages one server, but we don't know its name
        # here without a back-reference; fall through to extension.
        ext_map = {
            ".py": "python",
            ".php": "php",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".go": "go",
            ".rs": "rust",
            ".rb": "ruby",
            ".java": "java",
            ".c": "c",
            ".h": "c",
            ".cpp": "cpp",
            ".cc": "cpp",
            ".hpp": "cpp",
            ".cs": "csharp",
        }
        _, ext = os.path.splitext(file_path)
        if ext in ext_map:
            return ext_map[ext]
        # Fallback: whatever the project language is.
        return get_running_language(self.config)

    def ensure_doc_open(self, file_path):
        """Send textDocument/didOpen for `file_path` if we haven't already.

        Reads the file from disk so the server's view of it matches what's
        actually there. No-op if the file doesn't exist or has already
        been opened in this session.
        """
        if not file_path or not os.path.isfile(file_path):
            return

        uri = f"file://{file_path}"
        with self._open_docs_lock:
            if uri in self._open_docs:
                return
            # Mark before sending so concurrent requests don't double-open.
            self._open_docs.add(uri)

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            # Couldn't read — undo the mark so a later retry can try again.
            with self._open_docs_lock:
                self._open_docs.discard(uri)
            return

        language_id = self._lsp_language_id_for(file_path)
        notification = {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            },
        }
        try:
            send_jsonrpc_to_proc(self.proc, notification)
        except (OSError, BrokenPipeError):
            with self._open_docs_lock:
                self._open_docs.discard(uri)
            return

        # Some servers (pyright, intelephense) need a brief moment to
        # parse/index the just-opened document before they can answer
        # queries about it. Configurable per-server via languages.json.
        delay_ms = self.config.get("settings", {}).get("post_open_delay_ms", 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

    def get_diagnostics(self, uri=None):
        """Retrieve stored diagnostics.

        With `uri`: returns a list. Without: returns a {uri: [diagnostics]} snapshot.
        """
        with self._diagnostics_lock:
            if uri:
                return list(self._diagnostics.get(uri, []))
            return {k: list(v) for k, v in self._diagnostics.items()}

    def stop(self):
        self.running = False
        self.dispatcher.stop()
        try:
            self.socket.close()
        except Exception:
            pass
        if os.path.exists(SOCKET_PATH):
            try:
                os.remove(SOCKET_PATH)
            except OSError:
                pass


# ── LSP Process Manager ───────────────────────────────────────────────


def start_lsp_process(server, config):
    """Start LSP server and hand off to broker.

    Returns (proc, broker, log_fh).
    """
    log_file_path = config.get("settings", {}).get("log_file", "/tmp/lsp-server.log")
    log_fh = open(log_file_path, "a")
    log_fh.write(f"\n=== LSP runner session started at {time.ctime()} ===\n")
    log_fh.flush()

    proc = subprocess.Popen(
        server["command"] + server.get("args", []),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log_fh,
        cwd=HOME,
        env={**os.environ, **server.get("env", {})},
    )

    reader = LSPStdoutReader(proc.stdout)
    workspace_uri = f"file://{HOME}"
    init_request = {
        "jsonrpc": "2.0",
        "id": next(_request_counter),
        "method": "initialize",
        "params": {
            "processId": proc.pid,
            "clientInfo": {"name": "depthnet-lsp-runner", "version": "2.1"},
            "rootUri": workspace_uri,
            "rootPath": HOME,
            "workspaceFolders": [
                {"uri": workspace_uri, "name": os.path.basename(HOME) or "home"}
            ],
            "capabilities": {
                "textDocument": {
                    "synchronization": {
                        "didSave": True,
                        "willSave": False,
                        "dynamicRegistration": False,
                    },
                    "references": {"dynamicRegistration": False},
                    "definition": {"dynamicRegistration": False},
                    "hover": {
                        "dynamicRegistration": False,
                        "contentFormat": ["markdown", "plaintext"],
                    },
                    "documentSymbol": {
                        "dynamicRegistration": False,
                        "hierarchicalDocumentSymbolSupport": False,
                    },
                    "publishDiagnostics": {"relatedInformation": False},
                },
                "workspace": {
                    "workspaceFolders": True,
                    "configuration": False,
                },
            },
            # Some servers (intelephense) read this for licensing/feature flags.
            "initializationOptions": server.get("initialization_options", {}),
        },
    }

    try:
        send_jsonrpc_to_proc(proc, init_request)
        # The server may send notifications (window/logMessage, $/progress)
        # before the initialize response — skip them until we see our id.
        while True:
            msg = reader.read_message(timeout=10)
            if msg.get("id") == init_request["id"]:
                break
    except Exception as e:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        reader.stop()
        try:
            log_fh.close()
        except Exception:
            pass
        raise RuntimeError(f"LSP initialize failed: {e}")

    initialized = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
    send_jsonrpc_to_proc(proc, initialized)

    # Pass the SAME reader to the broker — never create a second one
    # on the same pipe.
    broker = LSPBroker(proc, config, reader)
    broker_thread = threading.Thread(target=broker.run, daemon=True)
    broker_thread.start()

    return proc, broker, log_fh


# ── LSP Request Methods ───────────────────────────────────────────────


def build_lsp_request(method, file_path, symbol, line, character, language_id):
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
        # Broker-local — does NOT go to the LSP server. We just ask the
        # broker for whatever diagnostics it has buffered from
        # publishDiagnostics notifications.
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "diagnostics/get",
            "params": {"uri": uri if file_path else None},
        }
    return None


def parse_lsp_response(method, response, max_results):
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
        out = []

        def flatten(items):
            for s in items:
                rng = s.get("selectionRange") or s.get("range") or {}
                start = rng.get("start", {})
                out.append(
                    {
                        "file": "",
                        "line": start.get("line", 0) + 1,
                        "col": start.get("character", 0) + 1,
                        "context": s.get("name", ""),
                        "kind": s.get("kind", 0),
                    }
                )
                children = s.get("children") or []
                flatten(children)

        if isinstance(result, list):
            if result and "selectionRange" in result[0]:
                flatten(result)
            else:
                for s in result:
                    loc = s.get("location", {})
                    rng = loc.get("range", {})
                    start = rng.get("start", {})
                    out.append(
                        {
                            "file": loc.get("uri", "").replace("file://", ""),
                            "line": start.get("line", 0) + 1,
                            "col": start.get("character", 0) + 1,
                            "context": s.get("name", ""),
                            "kind": s.get("kind", 0),
                        }
                    )

        return {"results": out[:max_results]}
    elif method == "diagnostics":
        # Either a list (uri given) or a dict {uri: [diags]} (no uri).
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = []
            for uri, diags in result.items():
                for d in diags:
                    item = dict(d)
                    item["_uri"] = uri
                    items.append(item)
        else:
            items = []
        return {"results": items[:max_results]}
    return {"results": []}


# ── Commands ──────────────────────────────────────────────────────────


def _daemonize(log_file_path):
    """Detach from terminal via double-fork.

    Returns True in the daemon child, False in the original parent.
    The parent should return immediately after this call.
    """
    # First fork.
    try:
        pid = os.fork()
    except OSError as e:
        print(f"ERROR: fork #1 failed: {e}")
        sys.exit(1)

    if pid > 0:
        # Original parent. Wait briefly for the intermediate child to exit
        # so we don't leave a zombie, then return False.
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return False

    # Intermediate child.
    os.setsid()

    try:
        pid = os.fork()
    except OSError:
        os._exit(1)

    if pid > 0:
        # Intermediate child exits; the grandchild becomes the daemon.
        os._exit(0)

    # We're the daemon now. Redirect stdio BEFORE doing anything that
    # might fail — so any error trace ends up in the log file.
    os.chdir("/")

    # Open log first; if it fails, we have no choice but to die silently.
    try:
        log_fd = os.open(log_file_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        os._exit(2)

    # Redirect stdin to /dev/null, stdout+stderr to the log.
    try:
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull_fd, 0)
        os.close(devnull_fd)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
    except OSError:
        os._exit(3)

    # Now any uncaught exception in the daemon will write its traceback
    # to the log file via sys.stderr (= fd 2 = the log).
    return True


def cmd_start(config):
    pid_file = get_pid_file(config)

    if is_running(config):
        lang = get_running_language(config)
        with open(pid_file) as f:
            pid = f.read().strip()
        print(f"LSP server already running (PID: {pid}, language: {lang})")
        return

    for f in [pid_file, get_lang_file(config)]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    detector = LanguageDetector(config)
    detected = detector.detect()

    if not detected:
        print("ERROR: No project detected.")
        print(
            f"Supported markers: {', '.join(e['marker'] for e in config.get('detection', []))}"
        )
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

    # Daemonize BEFORE spawning the LSP — that way the LSP server is a
    # child of the long-lived daemon, not of the short-lived foreground
    # process that the user invoked.
    log_file = config.get("settings", {}).get("log_file", "/tmp/lsp-server.log")

    if not _daemonize(log_file):
        # We're the original parent. Wait briefly for the daemon to
        # actually come up so the user gets useful feedback before we
        # return to the shell.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if is_running(config):
                lang = get_running_language(config) or detected["language"]
                pid_text = "?"
                if os.path.exists(pid_file):
                    try:
                        with open(pid_file) as f:
                            pid_text = f.read().strip()
                    except OSError:
                        pass
                print(
                    f"LSP server started (PID: {pid_text}, "
                    f"language: {lang}, label: {detected['label']})"
                )
                return
            time.sleep(0.1)
        print("ERROR: LSP daemon did not come up within 5s. Check the log file.")
        sys.exit(1)

    # ---- We are now the daemon. ----
    try:
        proc, broker, _log_fh = start_lsp_process(server, config)
    except RuntimeError as e:
        # Can't print to the user; we're detached. Write to log file.
        with open(log_file, "a") as f:
            f.write(f"\nERROR: Failed to start LSP server: {e}\n")
        os._exit(1)

    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
    with open(get_lang_file(config), "w") as f:
        f.write(detected["language"])

    # Keep the daemon alive while the LSP server is alive. The broker
    # thread is daemonic, so we need a foreground wait here.
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        # LSP died — clean up the marker files so `status` reports
        # accurately on the next call.
        broker.stop()
        for f in [pid_file, get_lang_file(config), SOCKET_PATH]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
    os._exit(0)


def cmd_stop(config):
    pid_file = get_pid_file(config)

    if not is_running(config):
        print("LSP server is not running.")
        # Clean up any stale leftovers.
        for f in [pid_file, get_lang_file(config), SOCKET_PATH]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        return

    pid = None
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
        except (ValueError, OSError):
            pid = None

    # Graceful shutdown via socket.
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(SOCKET_PATH)
        shutdown_request = {
            "jsonrpc": "2.0",
            "id": next(_request_counter),
            "method": "shutdown",
            "params": {},
        }
        sock.sendall(encode_jsonrpc_message(shutdown_request))
        sock.close()
    except (OSError, socket.timeout):
        pass

    # Wait up to 2s for graceful exit.
    if pid:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid = None
                break
            except OSError:
                break
            time.sleep(0.1)

    # Escalate: SIGTERM -> SIGKILL, but only if PID still belongs to us.
    if pid:
        if _proc_is_lsp_runner(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                    # Still alive — escalate.
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                print(f"LSP server stopped (PID: {pid})")
            except ProcessLookupError:
                print("LSP server was not running (stale PID file)")
            except OSError as e:
                print(f"Could not signal PID {pid}: {e}")
        else:
            print(
                f"PID {pid} no longer looks like our LSP server "
                f"(probably reused) — not killing."
            )
    else:
        print("LSP server stopped")

    for f in [pid_file, get_lang_file(config), SOCKET_PATH]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


def cmd_status(config):
    if is_running(config):
        lang = get_running_language(config)
        pid_file = get_pid_file(config)
        pid = "?"
        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    pid = f.read().strip()
            except OSError:
                pass
        print(f"running (PID: {pid}, language: {lang})")
    else:
        print("stopped")


def cmd_language(config):
    if is_running(config):
        print(get_running_language(config))
    else:
        detector = LanguageDetector(config)
        detected = detector.detect()
        print(detected["language"] if detected else "unknown")


def _validate_and_build(request, config):
    """Validate a client request dict and build the corresponding LSP request.

    Returns (lsp_request, method, max_results) on success,
    or ({"error": "..."}, None, None) on validation failure.
    """
    method = request.get("method", "")
    file_path = request.get("file", "")
    line = request.get("line", 1)
    character = request.get("character", request.get("col", request.get("char", 1)))

    if not isinstance(line, int) or line < 1:
        return (
            {"error": f"`line` must be a positive integer, got: {line!r}"},
            None,
            None,
        )
    if not isinstance(character, int) or character < 1:
        return (
            {"error": f"`character` must be a positive integer, got: {character!r}"},
            None,
            None,
        )

    symbol = request.get("symbol", "")
    max_results = request.get(
        "max_results", config.get("settings", {}).get("max_results", 20)
    )
    language_id = get_running_language(config)

    lsp_request = build_lsp_request(
        method, file_path, symbol, line, character, language_id
    )
    if lsp_request is None:
        return {"error": f"Unsupported method: {method}"}, None, None

    return lsp_request, method, max_results


def _drain_one_response(sock, expected_id, deadline):
    """Read frames from `sock` until one with id == expected_id arrives.

    Returns the parsed message, or None on timeout.
    """
    buffer = b""

    while time.time() < deadline:
        remaining = deadline - time.time()
        ready, _, _ = select.select([sock], [], [], min(0.1, remaining))
        if not ready:
            continue

        chunk = sock.recv(4096)
        if not chunk:
            return None

        buffer += chunk

        while b"\r\n\r\n" in buffer:
            header_end = buffer.index(b"\r\n\r\n")
            header_str = buffer[:header_end].decode("utf-8", errors="replace")
            match = re.search(r"Content-Length:\s*(\d+)", header_str, re.IGNORECASE)
            if not match:
                buffer = buffer[header_end + 4 :]
                continue

            content_length = int(match.group(1))
            body_start = header_end + 4
            total_needed = body_start + content_length

            if len(buffer) < total_needed:
                break

            body_bytes = buffer[body_start:total_needed]
            buffer = buffer[total_needed:]

            try:
                msg = json.loads(body_bytes.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue

            if msg.get("id") == expected_id:
                return msg

    return None


def cmd_request(config):
    if not is_running(config):
        print(
            json.dumps(
                {
                    "error": "LSP server is not running. Start it with 'lsp-runner start'."
                }
            )
        )
        return

    try:
        request = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        return

    lsp_request, method, max_results = _validate_and_build(request, config)
    if method is None:
        print(json.dumps(lsp_request, ensure_ascii=False))
        return

    timeout = config.get("settings", {}).get("request_timeout_ms", 10000) / 1000

    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(SOCKET_PATH)
        sock.sendall(encode_jsonrpc_message(lsp_request))

        response = _drain_one_response(sock, lsp_request["id"], time.time() + timeout)
        if response is None:
            result = {"error": "Timeout waiting for LSP response"}
        else:
            result = parse_lsp_response(method, response, max_results)
    except Exception as e:
        result = {"error": f"LSP request failed: {e}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    print(json.dumps(result, ensure_ascii=False))


def cmd_batch(config):
    """Run multiple requests over a single connection.

    Input on stdin: a JSON array of request objects (same format as
    `request`). Output: a JSON array of result objects, in the same
    order. Requests are pipelined on one socket — the broker spawns a
    worker per request, so they execute concurrently on the LSP side.
    """
    if not is_running(config):
        print(
            json.dumps(
                {
                    "error": "LSP server is not running. Start it with 'lsp-runner start'."
                }
            )
        )
        return

    try:
        requests = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        return

    if not isinstance(requests, list):
        print(
            json.dumps(
                {"error": "Batch input must be a JSON array of request objects."}
            )
        )
        return

    timeout = config.get("settings", {}).get("request_timeout_ms", 10000) / 1000

    # Build all requests up front; bail with per-slot errors for invalid ones.
    built = []
    for i, req in enumerate(requests):
        if not isinstance(req, dict):
            built.append(
                (None, None, None, {"error": f"Request #{i} is not an object"})
            )
            continue
        lsp_request, method, max_results = _validate_and_build(req, config)
        if method is None:
            built.append((None, None, None, lsp_request))  # validation error dict
        else:
            built.append((lsp_request, method, max_results, None))

    # Open one socket, send all valid requests, then collect responses.
    sock = None
    results = [None] * len(built)
    pending_ids = {}  # lsp_id -> slot_index

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(SOCKET_PATH)

        for idx, (lsp_request, _method, _max_results, err) in enumerate(built):
            if err is not None:
                results[idx] = err
                continue
            sock.sendall(encode_jsonrpc_message(lsp_request))
            pending_ids[lsp_request["id"]] = idx

        # Per-request budget, summed but capped — pipelining means we
        # don't really pay N×timeout, but we want a sane upper bound.
        deadline = time.time() + timeout * max(1, len(pending_ids))
        buffer = b""

        while pending_ids and time.time() < deadline:
            remaining = deadline - time.time()
            ready, _, _ = select.select([sock], [], [], min(0.2, remaining))
            if not ready:
                continue

            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk

            while b"\r\n\r\n" in buffer:
                header_end = buffer.index(b"\r\n\r\n")
                header_str = buffer[:header_end].decode("utf-8", errors="replace")
                match = re.search(r"Content-Length:\s*(\d+)", header_str, re.IGNORECASE)
                if not match:
                    buffer = buffer[header_end + 4 :]
                    continue
                content_length = int(match.group(1))
                body_start = header_end + 4
                total_needed = body_start + content_length
                if len(buffer) < total_needed:
                    break

                body_bytes = buffer[body_start:total_needed]
                buffer = buffer[total_needed:]

                try:
                    msg = json.loads(body_bytes.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue

                slot = pending_ids.pop(msg.get("id"), None)
                if slot is None:
                    continue  # unknown ID, ignore
                _, method, max_results, _ = built[slot]
                results[slot] = parse_lsp_response(method, msg, max_results)

        # Anything still pending = timed out.
        for lsp_id, slot in pending_ids.items():
            results[slot] = {"error": "Timeout waiting for LSP response"}

    except Exception as e:
        # Fill any unset slots with the connection error.
        err = {"error": f"LSP batch failed: {e}"}
        for i in range(len(results)):
            if results[i] is None:
                results[i] = err
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    print(json.dumps(results, ensure_ascii=False))


# ── Main ──────────────────────────────────────────────────────────────


def print_usage():
    print("Usage: lsp-runner {start|stop|status|language|request|batch}")
    print()
    print("Commands:")
    print("  start     Auto-detect language and start LSP server")
    print("  stop      Stop the LSP server")
    print("  status    Show server status")
    print("  language  Print detected language")
    print("  request   Accept ONE JSON request via stdin and return result")
    print("  batch     Accept a JSON ARRAY of requests via stdin and return")
    print("            results in the same order, pipelined over one socket")
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

    # Help should work even without a config file present.
    if command in ("-h", "--help", "help"):
        print_usage()
        return

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
    elif command == "batch":
        cmd_batch(config)
    else:
        print(f"Unknown command: {command}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
