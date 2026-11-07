#!/usr/bin/env bash
set -e
echo "INSTALL USER: $(id)"

cd "$(dirname "$0")"

# Allow polyglot to access plugin profile files.
chmod 755 .

# Install Python dependencies.
if [ -x ".venv/bin/pip3" ]; then
    .venv/bin/pip3 install -r requirements.txt
else
    python3 -m pip install --user -r requirements.txt
fi
