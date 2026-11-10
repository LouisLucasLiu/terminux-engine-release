# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""proof 的**工具身分**（`proof_tool_identity`）。

為什麼需要它：setup repair 的 proof envelope 只記了 container image 與版本字串，
而同一個 pinned image 裡有**兩個** yosys——

```
/usr/local/bin/yosys                                  → 0.67
/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys  → 0.68+post
```

正式 flow 走後者、當時的 qualification 走前者。也就是說 image digest、`PATH`、
工具名稱或版本字串**都不足以**建立工具身分：`pinned image ≠ pinned tool`。

因此本模組把身分固定成「實際執行的那個 executable」：absolute realpath ＋
bytes SHA-256 ＋ 完整 version 輸出（及其 SHA-256）＋ immutable image ID，
再加上實際 argv／mode 與所有 proof input hashes。任一項缺失或與 qualification
不符時，proof 一律 `UNVERIFIABLE`——**不得** fallback 到 image 內另一個 yosys。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

#: 本身分結構的 schema 版本。canonical digest 材料改變時提升。
TOOL_IDENTITY_SCHEMA_VERSION = 1

#: 缺欄位／不符時使用的固定原因碼。
REASON_MISSING = "proof_tool_identity_incomplete"
REASON_MISMATCH = "proof_tool_identity_not_qualified"

#: 必須存在且非空的欄位。刻意不含 `repo_digest`／`image_tag`：
#: 前者不是所有環境都有，後者只是顯示資訊。
REQUIRED_FIELDS: tuple[str, ...] = (
    "image_id",
    "executable_realpath",
    "executable_sha256",
    "version_output",
    "version_sha256",
    "argv",
    "mode",
)

#: 參與 canonical digest 的欄位順序（固定，不依 dict 插入序）。
_DIGEST_FIELDS: tuple[str, ...] = (
    "schema_version",
    "image_id",
    "repo_digest",
    "executable_realpath",
    "executable_sha256",
    "version_sha256",
    "argv",
    "mode",
    "wrapper_sha256",
    "generator_sha256",
    "cell_model_sha256",
    "liberty_sha256",
    "gold_sha256",
    "gate_sha256",
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProofToolIdentity:
    """實際產生一份 proof 的工具與輸入身分。

    `image_tag` 只作顯示；`repo_digest` 有就存，沒有不推論。
    `derived_fields` 記錄哪些欄位**不是**原 envelope 直接保存、而是由別處
    重建的；空 tuple 代表全部都是直接保存的。
    """

    image_id: str | None = None
    repo_digest: str | None = None
    image_tag: str | None = None
    executable_realpath: str | None = None
    executable_sha256: str | None = None
    version_output: str | None = None
    version_sha256: str | None = None
    argv: tuple[str, ...] = ()
    mode: str | None = None
    wrapper_sha256: str | None = None
    generator_sha256: str | None = None
    cell_model_sha256: str | None = None
    liberty_sha256: str | None = None
    gold_sha256: str | None = None
    gate_sha256: str | None = None
    #: 由別處重建的欄位名稱 → 來源說明。
    derived_fields: tuple[tuple[str, str], ...] = ()

    @property
    def schema_version(self) -> int:
        return TOOL_IDENTITY_SCHEMA_VERSION

    def missing_fields(self) -> tuple[str, ...]:
        """回傳缺失或空白的必要欄位。"""
        missing: list[str] = []
        for name in REQUIRED_FIELDS:
            value = getattr(self, name)
            if value is None or (isinstance(value, str) and not value.strip()) \
                    or (isinstance(value, tuple) and not value):
                missing.append(name)
        return tuple(missing)

    def is_complete(self) -> bool:
        return not self.missing_fields()

    def canonical_material(self) -> str:
        """digest 的 canonical 材料：固定欄位順序、固定分隔、不含顯示資訊。"""
        parts: list[str] = []
        for name in _DIGEST_FIELDS:
            if name == "schema_version":
                value: Any = TOOL_IDENTITY_SCHEMA_VERSION
            else:
                value = getattr(self, name)
            if isinstance(value, tuple):
                value = "\x1f".join(str(v) for v in value)
            parts.append(f"{name}={'' if value is None else value}")
        return "\x1e".join(parts)

    @property
    def digest(self) -> str:
        return sha256_text(self.canonical_material())

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TOOL_IDENTITY_SCHEMA_VERSION,
            "image_id": self.image_id,
            "repo_digest": self.repo_digest,
            # tag 只作顯示資訊，明確標示，不參與 digest。
            "image_tag_display_only": self.image_tag,
            "executable_realpath": self.executable_realpath,
            "executable_sha256": self.executable_sha256,
            "version_output": self.version_output,
            "version_sha256": self.version_sha256,
            "argv": list(self.argv),
            "mode": self.mode,
            "wrapper_sha256": self.wrapper_sha256,
            "generator_sha256": self.generator_sha256,
            "cell_model_sha256": self.cell_model_sha256,
            "liberty_sha256": self.liberty_sha256,
            "gold_sha256": self.gold_sha256,
            "gate_sha256": self.gate_sha256,
            "derived_fields": [
                {"field": f, "source": s} for f, s in self.derived_fields],
            "digest": self.digest,
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "ProofToolIdentity":
        """讀回一份已落盤的身分。

        **未知欄位一律忽略**（向前相容），但缺少的必要欄位不補預設值——
        由 `missing_fields()` 如實回報，讓 gate 去 fail closed。
        """
        derived = tuple(
            (str(entry.get("field")), str(entry.get("source")))
            for entry in payload.get("derived_fields") or []
            if isinstance(entry, dict))
        return cls(
            image_id=payload.get("image_id"),
            repo_digest=payload.get("repo_digest"),
            image_tag=payload.get("image_tag_display_only") or payload.get("image_tag"),
            executable_realpath=payload.get("executable_realpath"),
            executable_sha256=payload.get("executable_sha256"),
            version_output=payload.get("version_output"),
            version_sha256=payload.get("version_sha256"),
            argv=tuple(payload.get("argv") or ()),
            mode=payload.get("mode"),
            wrapper_sha256=payload.get("wrapper_sha256"),
            generator_sha256=payload.get("generator_sha256"),
            cell_model_sha256=payload.get("cell_model_sha256"),
            liberty_sha256=payload.get("liberty_sha256"),
            gold_sha256=payload.get("gold_sha256"),
            gate_sha256=payload.get("gate_sha256"),
            derived_fields=derived,
        )


#: 判斷「同一個工具」時必須逐項相同的欄位。**不含** version_output 全文
#: （用它的 sha256）、不含 tag、不含 per-pair 的 gold／gate。
TOOL_MATCH_FIELDS: tuple[str, ...] = (
    "image_id",
    "executable_realpath",
    "executable_sha256",
    "version_sha256",
)


def same_tool(left: ProofToolIdentity, right: ProofToolIdentity) -> tuple[bool, tuple[str, ...]]:
    """兩份身分是否指同一個 executable；回傳 (相同, 不符欄位)。

    刻意**不**接受「同 image 就算同工具」——0.67 與 0.68+post 就在同一個
    image 內，realpath／bytes／version 任一不同即不同工具。
    """
    differing = tuple(
        name for name in TOOL_MATCH_FIELDS
        if getattr(left, name) != getattr(right, name))
    return (not differing), differing


def evaluate(
    identity: ProofToolIdentity | None,
    qualified: ProofToolIdentity | None,
) -> tuple[bool, str, tuple[str, ...]]:
    """gate：身分是否可信。回傳 (可接受, 原因碼, 細節)。

    規則（fail closed，無寬鬆預設）：

    1. 身分缺失 → 不可接受；
    2. 任一必要欄位缺失 → 不可接受；
    3. 沒有 qualification 身分可比 → 不可接受；
    4. qualification 身分本身不完整 → 不可接受；
    5. 兩者不是同一個 executable → 不可接受（**不** fallback 到 image
       內另一個 binary）。
    """
    if identity is None:
        return False, REASON_MISSING, ("proof_tool_identity",)
    missing = identity.missing_fields()
    if missing:
        return False, REASON_MISSING, missing
    if qualified is None:
        return False, REASON_MISMATCH, ("qualified_tool_identity",)
    qualified_missing = qualified.missing_fields()
    if qualified_missing:
        return False, REASON_MISSING, tuple(
            f"qualified.{name}" for name in qualified_missing)
    matched, differing = same_tool(identity, qualified)
    if not matched:
        return False, REASON_MISMATCH, differing
    return True, "", ()


def derived_image_identity_from_run(
    run_metadata: dict[str, Any] | None,
    *,
    source: str,
) -> ProofToolIdentity:
    """為**舊** envelope 由 durable run record 重建 image ID。

    只重建 image ID，且明確標成 `derived` 並記下來源。**不得**偽裝成原
    envelope 直接保存的欄位，也不重寫任何已落盤資料。
    """
    workflow = ((run_metadata or {}).get("orchestration") or {}).get(
        "workflow_config") or {}
    image_id = workflow.get("docker_image_id")
    return ProofToolIdentity(
        image_id=image_id,
        derived_fields=(("image_id", source),) if image_id else (),
    )
