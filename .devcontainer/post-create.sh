#!/usr/bin/env bash
set -euo pipefail

if ! gh copilot --help >/dev/null 2>&1; then
  if ! gh extension list | awk '{print $1}' | grep -qx 'github/gh-copilot'; then
    gh extension install github/gh-copilot
  fi
fi


echo "Installing workshop tools..."
echo "Installing Squad CLI..."
npm config set registry "https://packagefeedproxy.microsoft.io/npm/"
npm config get registry
npm install -g @bradygaster/squad-cli

echo 'Installed workshop tools:'
git --version
gh --version | head -n 1
node --version
npm --version
command -v squad
gh copilot --help >/dev/null
