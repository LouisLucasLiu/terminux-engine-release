# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import re
from datetime import datetime, timezone


# ── Regex patterns ────────────────────────────────────────────────────────────

_UUID_V4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

_ISO8601_UTC_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$',
)

# ── Enum value sets ───────────────────────────────────────────────────────────

_COMMAND_TYPES      = {"file_edit", "file_op", "eda_tool", "dfa_op", "job_submit", "other"}
_STREAMS            = {"stdout", "stderr"}
_NOTIFY_TYPES       = {"inline", "sidebar", "email"}
_PROVENANCE_SOURCES = {"dfa_ticket", "dvc", "runset", "plugin"}
_QUERY_ORDERS       = {"asc", "desc"}

_KNOWN_METHODS = {
    "session.init",
    "session.end",
    "command.submit",
    "command.output",
    "command.complete",
    "chat.query",
    "ecc.query",
    "provenance.write_spec",
    "provenance.diff",
    "plugin.notify",
}


# ── Exception ─────────────────────────────────────────────────────────────────

class SchemaValidationError(Exception):
    """
    IPC schema 驗證失敗時拋出。

    Attributes:
        code:    int，對應 Error Code 定義（通常為 -32602）
        message: str，人類可讀的錯誤說明
        field:   str | None，發生錯誤的欄位名稱，巢狀以 . 分隔（如 "filters.limit"）
    """

    def __init__(self, message: str, code: int = -32602, field: str | None = None) -> None:
        super().__init__(message)
        self.code    = code
        self.message = message
        self.field   = field


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require(params: dict, field: str) -> object:
    """確認 field 存在於 params，缺少時拋出 SchemaValidationError。"""
    if field not in params:
        raise SchemaValidationError(
            message=f"Missing required field: {field}",
            field=field,
        )
    return params[field]


def _check_uuid(value: object, field: str) -> None:
    if not isinstance(value, str) or not _UUID_V4_RE.match(value):
        raise SchemaValidationError(
            message=f"Field '{field}' must be a UUID v4 string",
            field=field,
        )


def _check_iso8601_utc(value: object, field: str) -> None:
    if not isinstance(value, str) or not _ISO8601_UTC_RE.match(value):
        raise SchemaValidationError(
            message=f"Field '{field}' must be an ISO 8601 UTC timestamp (Z suffix)",
            field=field,
        )


def _check_project_id(value: object, field: str) -> None:
    """project 識別碼：非空字串、無空白／控制字元、長度 ≤ 128。"""
    _check_nonempty_str(value, field)
    text = str(value)
    if len(text) > 128 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise SchemaValidationError(
            message=f"Field '{field}' must not contain whitespace or control characters "
                    "and must be at most 128 characters",
            field=field,
        )


def _check_nonempty_str(value: object, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise SchemaValidationError(
            message=f"Field '{field}' must be a non-empty string",
            field=field,
        )


def _check_abs_path(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.startswith("/"):
        raise SchemaValidationError(
            message=f"Field '{field}' must be an absolute path (starting with /)",
            field=field,
        )


def _check_enum(value: object, allowed: set[str], field: str) -> None:
    if value not in allowed:
        raise SchemaValidationError(
            message=f"Field '{field}' must be one of {sorted(allowed)}, got {value!r}",
            field=field,
        )


def _check_str_array(value: object, field: str) -> None:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise SchemaValidationError(
            message=f"Field '{field}' must be an array of strings",
            field=field,
        )


def _check_int_ge(value: object, minimum: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SchemaValidationError(
            message=f"Field '{field}' must be an integer >= {minimum}",
            field=field,
        )


def _parse_iso8601_utc(value: str) -> datetime:
    """Parse ISO 8601 UTC string（已通過 _check_iso8601_utc）為 datetime。"""
    # 標準化：去掉尾端 Z，換成 +00:00
    return datetime.fromisoformat(value.rstrip("Z") + "+00:00")


# ── Per-method param validators ───────────────────────────────────────────────

def _validate_session_init(params: dict) -> None:
    member_id      = _require(params, "member_id")
    shell          = _require(params, "shell")
    work_directory = _require(params, "work_directory")

    _check_nonempty_str(member_id,      "member_id")
    # `project_id` 可省略或為 null ⇒ session **未宣告**
    # project（由 daemon 標成 project_source="unset"）。給了就必須是合法的
    # 非空字串——空字串／非字串／含控制字元或空白一律報錯，不以 fallback 掩蓋。
    project_id = params.get("project_id")
    if project_id is not None:
        _check_project_id(project_id, "project_id")
    _check_abs_path(shell,              "shell")
    _check_abs_path(work_directory,     "work_directory")


def _validate_session_end(params: dict) -> None:
    session_id = _require(params, "session_id")
    _check_uuid(session_id, "session_id")


def _validate_command_submit(params: dict) -> None:
    session_id     = _require(params, "session_id")
    command        = _require(params, "command")
    work_directory = _require(params, "work_directory")
    timestamp      = _require(params, "timestamp")
    command_type   = _require(params, "command_type")
    targets        = _require(params, "targets")

    _check_uuid(session_id,             "session_id")
    _check_nonempty_str(command,        "command")
    _check_abs_path(work_directory,     "work_directory")
    _check_iso8601_utc(timestamp,       "timestamp")
    _check_enum(command_type, _COMMAND_TYPES, "command_type")
    _check_str_array(targets,           "targets")

    if "env_snapshot" in params and not isinstance(params["env_snapshot"], dict):
        raise SchemaValidationError(
            message="Field 'env_snapshot' must be an object",
            field="env_snapshot",
        )


def _validate_command_output(params: dict) -> None:
    session_id = _require(params, "session_id")
    stream     = _require(params, "stream")
    content    = _require(params, "content")
    timestamp  = _require(params, "timestamp")

    _check_uuid(session_id,          "session_id")
    _check_enum(stream, _STREAMS,    "stream")
    _check_nonempty_str(content,     "content")
    _check_iso8601_utc(timestamp,    "timestamp")


def _validate_command_complete(params: dict) -> None:
    session_id         = _require(params, "session_id")
    exit_code          = _require(params, "exit_code")
    duration_ms        = _require(params, "duration_ms")
    timestamp          = _require(params, "timestamp")
    targets_confirmed  = _require(params, "targets_confirmed")
    artifacts_detected = _require(params, "artifacts_detected")

    _check_uuid(session_id,                     "session_id")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise SchemaValidationError(
            message="Field 'exit_code' must be an integer",
            field="exit_code",
        )
    _check_int_ge(duration_ms, 0,               "duration_ms")
    _check_iso8601_utc(timestamp,               "timestamp")
    _check_str_array(targets_confirmed,         "targets_confirmed")
    _check_str_array(artifacts_detected,        "artifacts_detected")


def _validate_chat_query(params: dict) -> None:
    session_id = _require(params, "session_id")
    query      = _require(params, "query")
    timestamp  = _require(params, "timestamp")

    _check_uuid(session_id,       "session_id")
    _check_nonempty_str(query,    "query")
    _check_iso8601_utc(timestamp, "timestamp")


def _validate_ecc_query(params: dict) -> None:
    session_id = _require(params, "session_id")
    filters    = _require(params, "filters")

    _check_uuid(session_id, "session_id")

    if not isinstance(filters, dict):
        raise SchemaValidationError(
            message="Field 'filters' must be an object",
            field="filters",
        )

    # Optional filter fields
    if "limit" in filters:
        limit = filters["limit"]
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise SchemaValidationError(
                message="Field 'filters.limit' must be an integer >= 0",
                field="filters.limit",
            )

    if "order" in filters:
        if filters["order"] not in _QUERY_ORDERS:
            raise SchemaValidationError(
                message=f"Field 'filters.order' must be one of {sorted(_QUERY_ORDERS)}",
                field="filters.order",
            )

    if "after" in filters:
        _check_iso8601_utc(filters["after"], "filters.after")

    if "before" in filters:
        _check_iso8601_utc(filters["before"], "filters.before")

    if "after" in filters and "before" in filters:
        after_dt  = _parse_iso8601_utc(filters["after"])
        before_dt = _parse_iso8601_utc(filters["before"])
        if after_dt > before_dt:
            raise SchemaValidationError(
                message="Field 'filters.after' must not be greater than 'filters.before'",
                field="filters.after",
            )

    if "command_type" in filters:
        _check_enum(filters["command_type"], _COMMAND_TYPES, "filters.command_type")


def _validate_provenance_write_spec(params: dict) -> None:
    session_id = _require(params, "session_id")
    step       = _require(params, "step")
    member_id  = _require(params, "member_id")
    data       = _require(params, "data")
    source     = _require(params, "source")

    _check_uuid(session_id,                      "session_id")
    _check_nonempty_str(step,                    "step")
    _check_nonempty_str(member_id,               "member_id")
    if not isinstance(data, dict) or not data:
        raise SchemaValidationError(
            message="Field 'data' must be a non-empty object",
            field="data",
        )
    _check_enum(source, _PROVENANCE_SOURCES,     "source")


def _validate_provenance_diff(params: dict) -> None:
    session_id = _require(params, "session_id")
    step       = _require(params, "step")
    member_id  = _require(params, "member_id")

    _check_uuid(session_id,       "session_id")
    _check_nonempty_str(step,     "step")
    _check_nonempty_str(member_id, "member_id")


def _validate_plugin_notify(params: dict) -> None:
    session_id  = _require(params, "session_id")
    notify_type = _require(params, "notify_type")
    title       = _require(params, "title")
    content     = _require(params, "content")
    timestamp   = _require(params, "timestamp")

    _check_uuid(session_id,                    "session_id")
    _check_enum(notify_type, _NOTIFY_TYPES,    "notify_type")
    _check_nonempty_str(title,                 "title")
    _check_nonempty_str(content,               "content")
    _check_iso8601_utc(timestamp,              "timestamp")

    if "detail" in params and not isinstance(params["detail"], str):
        raise SchemaValidationError(
            message="Field 'detail' must be a string",
            field="detail",
        )


_REQUEST_VALIDATORS: dict[str, object] = {
    "session.init":           _validate_session_init,
    "session.end":            _validate_session_end,
    "command.submit":         _validate_command_submit,
    "command.output":         _validate_command_output,
    "command.complete":       _validate_command_complete,
    "chat.query":             _validate_chat_query,
    "ecc.query":              _validate_ecc_query,
    "provenance.write_spec":  _validate_provenance_write_spec,
    "provenance.diff":        _validate_provenance_diff,
    "plugin.notify":          _validate_plugin_notify,
}


# ── Public API ────────────────────────────────────────────────────────────────

def validate_request(method: str, params: dict) -> None:
    """
    驗證 incoming JSON-RPC request 的 params 是否符合 schema。
    驗證失敗時拋出 SchemaValidationError。
    """
    if method not in _KNOWN_METHODS:
        raise SchemaValidationError(
            message=f"Unknown method: {method!r}",
            code=-32601,
            field=None,
        )
    validator = _REQUEST_VALIDATORS[method]
    validator(params)  # type: ignore[operator]


def validate_response(method: str, result: dict) -> None:
    """
    驗證 outgoing response 的 result 是否符合 schema。
    用於開發階段的防禦性檢查。
    """
    if method not in _KNOWN_METHODS:
        raise SchemaValidationError(
            message=f"Unknown method: {method!r}",
            code=-32601,
            field=None,
        )

    simple_ok_methods = {
        "session.end", "command.submit", "command.output",
        "command.complete", "provenance.write_spec",
    }
    if method in simple_ok_methods:
        if result.get("status") != "ok":
            raise SchemaValidationError(
                message="Response result must contain status='ok'",
                field="status",
            )
        return

    if method == "session.init":
        session_id = result.get("session_id")
        if not isinstance(session_id, str) or not _UUID_V4_RE.match(session_id):
            raise SchemaValidationError(
                message="Response result.session_id must be a UUID v4 string",
                field="session_id",
            )

    elif method == "ecc.query":
        if not isinstance(result.get("events"), list):
            raise SchemaValidationError(
                message="Response result.events must be an array",
                field="events",
            )
        if not isinstance(result.get("total"), int):
            raise SchemaValidationError(
                message="Response result.total must be an integer",
                field="total",
            )

    elif method == "provenance.diff":
        if not isinstance(result.get("mismatches"), list):
            raise SchemaValidationError(
                message="Response result.mismatches must be an array",
                field="mismatches",
            )
        if not isinstance(result.get("has_mismatch"), bool):
            raise SchemaValidationError(
                message="Response result.has_mismatch must be a boolean",
                field="has_mismatch",
            )
