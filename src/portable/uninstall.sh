#!/bin/sh
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
set -eu
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$dir/share/portable_installer.py" ]; then
    exec "$dir/python/bin/python3.12" -I "$dir/share/portable_installer.py" uninstall "$dir" "$@"
fi
bootstrap=$(mktemp -d "${TMPDIR:-/tmp}/terminux-uninstall-XXXXXXXX")
trap 'rm -rf -- "$bootstrap"' EXIT HUP INT TERM
tar -xzf "$dir/python/cpython.tar.gz" -C "$bootstrap"
"$bootstrap/python/bin/python3.12" -I "$dir/portable_installer.py" uninstall "$dir" "$@"
