#!/usr/bin/env bash
set -euo pipefail


npm install --global --prefix "$HOME/.local" @bradygaster/squad-cli
npm install --global --prefix "$HOME/.local" @github/copilot


echo 'Installed workshop tools:'
git --version
gh --version | head -n 1
node --version
npm --version

command -v copilot
copilot --version
command -v squad

