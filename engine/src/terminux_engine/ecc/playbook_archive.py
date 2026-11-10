# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Playbook archive 寫入與讀取。

將 playbook YAML 按 (playbook_id, version) 存入 archive 目錄，
供未來 forensic audit / policy simulation 使用。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import yaml
from pydantic import ValidationError

from terminux_engine.ecc.playbook import Playbook

logger = logging.getLogger(__name__)


def archive_playbook_if_absent(
    playbook_id: str,
    version: str,
    source_yaml_path: Path,
    archive_root: Path,
) -> Path | None:
    """將 playbook YAML 檔案存入 archive（若尚未存在）。

    Archive 路徑：<archive_root>/<playbook_id>/<version>.yaml

    行為：
    - 檔案不存在 → byte-for-byte copy source_yaml_path 到 archive 路徑
    - 檔案存在且內容與 source 一致（byte-for-byte 比對）→ no-op
    - 檔案存在但內容與 source 不同 → log warning，不覆蓋

    永不 raise。失敗（權限、磁碟等）時 log error 並回 None。

    Args:
        playbook_id: Playbook.id value
        version: Playbook.version value
        source_yaml_path: 原始 YAML 檔案路徑
        archive_root: archive 根目錄（通常是 ~/.terminux/ecc/archive）

    Returns:
        成功寫入或已存在一致 → archive 檔案路徑
        衝突不覆蓋 → 既有 archive 路徑
        失敗 → None
    """
    # 防衛性：驗證參數
    if not playbook_id or playbook_id == "unknown":
        logger.warning("archive_playbook_if_absent: invalid playbook_id=%r", playbook_id)
        return None
    if not version or version == "unknown":
        logger.warning("archive_playbook_if_absent: invalid version=%r", version)
        return None

    # 建立 archive 目錄
    try:
        archive_dir = archive_root / playbook_id
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("archive_playbook_if_absent: mkdir failed: %s", e)
        return None

    archive_file = archive_dir / f"{version}.yaml"

    # 來源檔案讀取
    try:
        source_bytes = source_yaml_path.read_bytes()
    except (FileNotFoundError, OSError) as e:
        logger.error("archive_playbook_if_absent: read source failed: %s", e)
        return None

    # 處理已存在情境
    if archive_file.exists():
        try:
            archive_bytes = archive_file.read_bytes()
        except OSError as e:
            logger.error("archive_playbook_if_absent: read existing archive failed: %s", e)
            return None
        if archive_bytes == source_bytes:
            return archive_file  # no-op
        else:
            logger.warning(
                "archive_playbook_if_absent: conflict at %s — source differs from archive, "
                "NOT overwriting. This indicates the YAML was modified without bumping version.",
                archive_file,
            )
            return archive_file

    # 寫入
    try:
        shutil.copyfile(source_yaml_path, archive_file)
        logger.info("archive_playbook_if_absent: archived to %s", archive_file)
        return archive_file
    except OSError as e:
        logger.error("archive_playbook_if_absent: write failed: %s", e)
        return None


def load_playbook_by_identity(
    playbook_id: str,
    version: str,
    archive_root: Path,
) -> Playbook | None:
    """從 archive 讀取指定版本的 playbook。

    Archive 路徑：<archive_root>/<playbook_id>/<version>.yaml

    回傳 None 的情境：
    - archive 檔案不存在
    - YAML parse 失敗
    - Pydantic validation 失敗
    - 任何 I/O 錯誤

    永不 raise。失敗情境 log warning 後回 None。

    Args:
        playbook_id: Playbook.id value
        version: Playbook.version value
        archive_root: archive 根目錄

    Returns:
        成功 → Playbook instance
        失敗 → None（caller 自行決定 fallback）
    """
    archive_file = archive_root / playbook_id / f"{version}.yaml"

    if not archive_file.exists():
        return None

    try:
        raw = archive_file.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as e:
        logger.warning("load_playbook_by_identity: read/parse failed for %s: %s", archive_file, e)
        return None

    # Playbook spec 規定頂層 key 是 "playbook"
    if isinstance(data, dict) and "playbook" in data:
        playbook_data = data["playbook"]
    else:
        playbook_data = data

    try:
        return Playbook.model_validate(playbook_data)
    except (ValidationError, TypeError, KeyError, ValueError, AttributeError) as e:
        logger.warning("load_playbook_by_identity: validation failed for %s: %s", archive_file, e)
        return None
