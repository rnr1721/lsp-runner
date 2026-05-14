#!/bin/bash
# Install lsp-runner into a sandbox container or local machine
set -e

INSTALL_DIR="${LSP_RUNNER_DIR:-/usr/local/lib/lsp-runner}"
BIN_PATH="/usr/local/bin/lsp-runner"
REPO_URL="https://github.com/rnr1721/lsp-runner.git"
BRANCH="${LSP_RUNNER_BRANCH:-main}"

echo "Installing lsp-runner..."

# Check for Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 is required but not installed."
    exit 1
fi

# Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR"
    git fetch origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    echo "Cloning fresh installation..."
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# Create executable wrapper
cat > "$BIN_PATH" << 'WRAPPER'
#!/bin/bash
exec python3 INSTALL_DIR_PLACEHOLDER/lsp_runner.py "$@"
WRAPPER

sed -i "s|INSTALL_DIR_PLACEHOLDER|$INSTALL_DIR|" "$BIN_PATH"
chmod +x "$BIN_PATH"

echo ""
echo "lsp-runner installed successfully!"
echo "Run 'lsp-runner --help' for usage information."
echo ""
echo "Next steps:"
echo "  1. Make sure your project has a marker file (composer.json, go.mod, etc.)"
echo "  2. Install the appropriate LSP server for your language"
echo "  3. Run 'lsp-runner start' to start the server"
echo ""
echo "Install hints for common languages:"
python3 -c "
import json, os
config_path = os.path.join('$INSTALL_DIR', 'languages.json')
if os.path.exists(config_path):
    with open(config_path) as f:
        config = json.load(f)
    for s in config.get('servers', {}).values():
        print(f\"  {s.get('install_hint', '')}\")
"