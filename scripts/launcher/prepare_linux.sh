#!/usr/bin/env bash
# Explicit, interactive setup in the same default WSL user used by Mabobot.
set -euo pipefail
installer=""
on_exit() {
    local code=$?
    if [ -n "$installer" ]; then rm -f "$installer"; fi
    if [ "$code" -ne 0 ] && [ -t 0 ]; then
        echo 'Setup did not finish. Read the error above, then press Enter to return and retry.'
        read -r _ || true
    fi
    exit "$code"
}
trap on_exit EXIT
mode="${1:-codex}"
case "$mode" in codex|file-tools) ;; *) echo 'Unknown setup component'; exit 2 ;; esac
if [ "$(id -u)" = 0 ]; then
    echo 'Please initialize Ubuntu with a normal Linux user, then reopen the wizard.'
    exit 1
fi
if ! command -v apt-get >/dev/null; then
    echo 'Automatic preparation supports Ubuntu/Debian. Install Python 3, curl and git with your distribution package manager, then retry.'
    exit 1
fi
echo 'Preparing Linux dependencies. Enter your Linux password if sudo asks (typing is invisible).'
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv curl ca-certificates git
# Match the runtime probe: never mistake a Windows npm shim for a Linux CLI.
export PATH="$HOME/.local/bin:$HOME/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin"
for node_bin in "$HOME"/.nvm/versions/node/*/bin; do
    if [ -x "$node_bin/codex" ]; then export PATH="$node_bin:$PATH"; fi
done
if [ "$mode" = codex ]; then
    # Do not replace a working installation or touch account/API configuration.
    if ! command -v codex >/dev/null; then
        installer="$(mktemp)"
        curl --fail --location --retry 2 --connect-timeout 30 --max-time 300 \
            https://chatgpt.com/codex/install.sh -o "$installer"
        sh "$installer"
    fi
    codex --version
else
    project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
    sudo apt-get install -y pandoc poppler-utils p7zip-full tesseract-ocr tesseract-ocr-chi-sim \
        tesseract-ocr-chi-tra libmagic1 libpango-1.0-0 ghostscript qpdf fonts-noto-cjk
    python3 -m pip install --user --break-system-packages -r "$project_root/requirements-file-tools.txt"
    bash "$project_root/scripts/file_tools/install_launchers.sh"
    python3 -c 'import docx, openpyxl, pptx, fitz'
    pdftotext -v
    pandoc --version
fi
echo 'Preparation complete. Return to the Mabobot wizard to check the result.'
