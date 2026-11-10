# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""AnalysisTask 的固定拒絕碼。

這些字串的意義是固定的對外契約，**不得改動語義**。typed IPC 與 slash
control 回傳同一組 code；預期中的 deny 一律是結構化回覆，不是 unhandled
exception。
"""

from __future__ import annotations

#: task 不存在，或 caller 沒有讀／操作範圍。兩者刻意共用同一個 code——
#: 不對外洩漏「這個 task 是否存在」。
TASK_NOT_FOUND_OR_OUT_OF_SCOPE = "TASK_NOT_FOUND_OR_OUT_OF_SCOPE"
#: 同 scope 下 digest 不符。
TASK_DIGEST_MISMATCH = "TASK_DIGEST_MISMATCH"
#: `start` 不是建立這筆 task 的 session。
TASK_ORIGIN_SESSION_REQUIRED = "TASK_ORIGIN_SESSION_REQUIRED"
#: 不是本 daemon instance 的 live task，因此沒有執行權（重啟後的歷史）。
TASK_INSTANCE_EXPIRED = "TASK_INSTANCE_EXPIRED"
#: worker 名額被已接受的 task 佔用；**未消耗任何新授權**。
TASK_BUSY = "TASK_BUSY"
#: 舊 action 未確認結束，名額不得重用。
TASK_WORKER_UNCONFIRMED = "TASK_WORKER_UNCONFIRMED"
#: 相同 task 不可重複核准派送。
TASK_ALREADY_STARTED = "TASK_ALREADY_STARTED"
#: **歷史碼**（目前不再發出）：草案不因時間經過失效。
#: 保留只為了讀取舊 evidence／文件中的既有記錄。
TASK_DRAFT_EXPIRED = "TASK_DRAFT_EXPIRED"
#: snapshot／schema／risk／arguments／identity 等重驗不符。
TASK_REVALIDATION_FAILED = "TASK_REVALIDATION_FAILED"
#: 無法證明指定 run 的存取範圍。
TASK_RUN_ACCESS_UNVERIFIABLE = "TASK_RUN_ACCESS_UNVERIFIABLE"
#: 不能用舊的單筆 proposal control 操作 task 的 child proposal。
TASK_PROPOSAL_CONTROL_DENIED = "TASK_PROPOSAL_CONTROL_DENIED"
#: 不再派工／不再發出新的 LLM dispatch。
TASK_BUDGET_EXHAUSTED = "TASK_BUDGET_EXHAUSTED"
TASK_DEADLINE_EXCEEDED = "TASK_DEADLINE_EXCEEDED"
#: 記錄／scope／hash／reference 不一致或缺失。
TASK_EVIDENCE_INVALID = "TASK_EVIDENCE_INVALID"
#: 不知道實際執行／必要持久化是否完成；**禁止自動重送**。
TASK_OUTCOME_UNCONFIRMED = "TASK_OUTCOME_UNCONFIRMED"

#: 結果 envelope 相關。這兩個是 step receipt 的 error_code，
#: 不是 public request 的拒絕碼，但語義同樣固定。
RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
RESULT_NOT_SERIALIZABLE = "RESULT_NOT_SERIALIZABLE"

#: create 階段的輸入驗證（host validator 與 Agent intent 共用同一份）。
TASK_INPUT_INVALID = "TASK_INPUT_INVALID"

#: 分析**執行依賴**（provider adapter／router）不可用。
#: 這**不是** service 未配置——task control、查詢與取消都仍然可用。
TASK_PROVIDER_UNAVAILABLE = "TASK_PROVIDER_UNAVAILABLE"

#: 要求以舊 Docker container 模式開始新的執行；歷史 task 仍可讀取。
LEGACY_DOCKER_EXECUTION_DISABLED = "LEGACY_DOCKER_EXECUTION_DISABLED"

#: 要求以沒有 native tool binding 的舊 host runner 開始新的執行；歷史 task 仍可讀取。
LEGACY_HOST_RUNNER_EXECUTION_DISABLED = "LEGACY_HOST_RUNNER_EXECUTION_DISABLED"

ALL_CODES: frozenset[str] = frozenset({
    TASK_NOT_FOUND_OR_OUT_OF_SCOPE, TASK_DIGEST_MISMATCH,
    TASK_ORIGIN_SESSION_REQUIRED, TASK_INSTANCE_EXPIRED, TASK_BUSY,
    TASK_WORKER_UNCONFIRMED, TASK_ALREADY_STARTED, TASK_DRAFT_EXPIRED,
    TASK_REVALIDATION_FAILED, TASK_RUN_ACCESS_UNVERIFIABLE,
    TASK_PROPOSAL_CONTROL_DENIED, TASK_BUDGET_EXHAUSTED,
    TASK_DEADLINE_EXCEEDED, TASK_EVIDENCE_INVALID, TASK_OUTCOME_UNCONFIRMED,
    RESULT_TOO_LARGE, RESULT_NOT_SERIALIZABLE, TASK_INPUT_INVALID,
    LEGACY_DOCKER_EXECUTION_DISABLED, LEGACY_HOST_RUNNER_EXECUTION_DISABLED,
})


class TaskError(RuntimeError):
    """帶固定 code 的 task error。`details` 只放有界、可安全外露的欄位。"""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details)

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"status": "error", "error_code": self.code,
                                      "message": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


__all__ = ["ALL_CODES", "TaskError"] + sorted(ALL_CODES)
