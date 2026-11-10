# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""透過 libknowledge_codec.so 讀取加密的 shared RAG knowledge。"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path


_LOGGER = logging.getLogger(__name__)


def resolve_shared_knowledge_dir() -> Path | None:
    """從明確設定或使用者資料區解析 shared knowledge，不讀開發 checkout。"""
    configured = os.environ.get("TERMINUX_SHARED_KNOWLEDGE_DIR", "").strip()
    if configured:
        directory = Path(configured).expanduser()
        if not directory.is_absolute():
            raise ValueError("TERMINUX_SHARED_KNOWLEDGE_DIR must be an absolute path")
        return directory if directory.is_dir() else None

    user_root = Path.home() / ".terminux"
    plaintext_directory = user_root / "rag_knowledge"
    if plaintext_directory.is_dir():
        return plaintext_directory

    encoded_directory = user_root / "rag_knowledge_encoded"
    if encoded_directory.is_dir():
        return encoded_directory
    return None


def _library_candidates() -> list[Path]:
    """依既定優先序回傳可能的 codec library 路徑。"""
    candidates: list[Path] = []
    configured = os.environ.get("TERMINUX_CODEC_LIB")
    if configured:
        candidates.append(Path(configured))

    module_path = Path(__file__).resolve()
    candidates.append(module_path.parent / "libknowledge_codec.so")
    candidates.append(module_path.parent.parent / "libknowledge_codec.so")
    return candidates


def _load_library() -> ctypes.CDLL:
    for candidate in _library_candidates():
        if not candidate.is_file():
            continue
        try:
            library = ctypes.CDLL(str(candidate))
        except OSError as error:
            raise RuntimeError(f"無法載入 knowledge codec library: {candidate}: {error}") from error

        library.tk_knowledge_decrypt.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        library.tk_knowledge_decrypt.restype = ctypes.c_int
        library.tk_knowledge_free.argtypes = [ctypes.c_void_p]
        library.tk_knowledge_free.restype = None
        return library

    searched = ", ".join(str(path) for path in _library_candidates())
    raise RuntimeError(f"找不到 knowledge codec library（已搜尋: {searched}）")


def decrypt_file(enc_path: Path) -> bytes:
    """讀取 .enc 檔案並透過 shared library 解密為明文 bytes。"""
    encrypted = enc_path.read_bytes()
    library = _load_library()
    input_buffer = (ctypes.c_uint8 * len(encrypted)).from_buffer_copy(encrypted)
    output_pointer = ctypes.POINTER(ctypes.c_uint8)()
    output_length = ctypes.c_size_t()
    result = library.tk_knowledge_decrypt(
        input_buffer,
        len(encrypted),
        ctypes.byref(output_pointer),
        ctypes.byref(output_length),
    )
    if result != 0:
        errors = {
            -1: "加密檔格式錯誤",
            -2: "加密檔解密失敗（可能已被篡改）",
            -3: "解密配置記憶體失敗",
        }
        raise RuntimeError(errors.get(result, f"解密失敗（錯誤碼: {result}）"))

    try:
        return ctypes.string_at(output_pointer, output_length.value)
    finally:
        library.tk_knowledge_free(output_pointer)


def _directory_has_files(directory: Path) -> bool:
    return directory.is_dir() and any(path.is_file() for path in directory.rglob("*"))


def _load_plaintext(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _load_encoded(directory: Path) -> dict[str, str]:
    knowledge: dict[str, str] = {}
    for path in sorted(directory.rglob("*.enc")):
        if path.is_file():
            relative_path = path.relative_to(directory)
            knowledge[str(relative_path.with_suffix(""))] = decrypt_file(path).decode("utf-8")
    return knowledge


def load_shared_knowledge(base_dir: Path) -> dict[str, str]:
    """依明文優先序讀取 shared RAG knowledge；不存在時回傳空 dict。"""
    plaintext_directory = base_dir / "rag_knowledge"
    if _directory_has_files(plaintext_directory):
        return _load_plaintext(plaintext_directory)

    encoded_directory = base_dir / "rag_knowledge_encoded"
    if encoded_directory.is_dir():
        return _load_encoded(encoded_directory)

    _LOGGER.warning("找不到 shared RAG knowledge 目錄: %s", base_dir)
    return {}
