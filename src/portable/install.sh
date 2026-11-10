#!/bin/sh
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
set -eu
case "$(uname -m)" in x86_64) ;; *) echo '只支援 x86_64。' >&2; exit 1;; esac
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# 安裝前只在獨立暫存目錄展開已隨包附上的 Python。
bootstrap=$(mktemp -d "${TMPDIR:-/tmp}/terminux-install-XXXXXXXX")
trap 'rm -rf -- "$bootstrap"' EXIT HUP INT TERM
tar -xzf "$dir/python/cpython.tar.gz" -C "$bootstrap"
"$bootstrap/python/bin/python3.12" -I "$dir/portable_installer.py" install "$dir" "$@"
