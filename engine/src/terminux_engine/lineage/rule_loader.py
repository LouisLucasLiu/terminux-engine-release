# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LineageRule:
    rule_id: str
    enabled: bool
    severity: str
    applies_to: str
    message: str


def load_lineage_rules(path: str | Path) -> list[LineageRule]:
    """
    從 YAML 載入 lineage rules，保持原始順序。
    各種結構錯誤一律 raise ValueError，不靜默失敗。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"lineage rules file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("lineage_rules.yaml: top-level must be a mapping")

    if "rules" not in data:
        raise ValueError("lineage_rules.yaml: missing required key 'rules'")

    raw_rules = data["rules"]
    if not isinstance(raw_rules, list):
        raise ValueError("lineage_rules.yaml: 'rules' must be a list")

    rules: list[LineageRule] = []
    for i, item in enumerate(raw_rules):
        for required in ("id", "enabled", "severity", "applies_to", "message"):
            if required not in item:
                raise ValueError(
                    f"lineage_rules.yaml: rules[{i}] missing required key '{required}'"
                )
        if not isinstance(item["enabled"], bool):
            raise ValueError(
                f"lineage_rules.yaml: rules[{i}] 'enabled' must be a bool"
            )
        rules.append(LineageRule(
            rule_id=item["id"],
            enabled=item["enabled"],
            severity=item["severity"],
            applies_to=item["applies_to"],
            message=item["message"],
        ))

    return rules
