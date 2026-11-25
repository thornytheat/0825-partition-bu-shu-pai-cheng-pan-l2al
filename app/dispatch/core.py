"""领槽执行域：纯领域工具（时隙区间、状态归一、参数别名）。

时隙约定与排程盘一致：整数时隙，区间为左闭右开 ``[start, end)``，
首尾相接（如 ``[2,4)`` 与 ``[4,6)``）不算重叠。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.business import FlexStr

# ---- 状态同义词 ---------------------------------------------------------

CLAIMED = FlexStr('CLAIMED', ('ACTIVE', 'ACQUIRED', 'LEASED', 'IN_PROGRESS', 'RUNNING', 'OPEN', 'HELD'))
SUCCEEDED = FlexStr('SUCCEEDED', ('COMPLETED', 'DONE', 'SUCCESS', 'FINISHED', 'OK', 'COMPLETE'))
FAILED = FlexStr('FAILED', ('FAILURE', 'ERROR', 'REJECTED_EXECUTION', 'UNSUCCESSFUL'))
TIMED_OUT = FlexStr('TIMED_OUT', ('TIMEOUT', 'OVERDUE', 'EXPIRED', 'LEASE_TIMEOUT', 'TIME_OUT'))
RELEASED = FlexStr('RELEASED', ('FREED', 'CLOSED', 'RETURNED'))
BLOCKED = FlexStr('BLOCKED', ('DEPENDENCY_BLOCKED', 'UPSTREAM_BLOCKED', 'WAITING_UPSTREAM'))
ISSUED = FlexStr('ISSUED', ('PENDING_CLAIM', 'READY_TO_CLAIM', 'PENDING'))

SUCCESS_TOKENS = {'SUCCESS', 'SUCCEEDED', 'COMPLETED', 'COMPLETE', 'DONE', 'FINISHED', 'OK', 'PASS', 'PASSED'}
FAILURE_TOKENS = {'FAILED', 'FAILURE', 'ERROR', 'ERRORED', 'UNSUCCESSFUL', 'FAIL', 'REJECTED'}

TERMINAL_OUTCOMES = {'SUCCEEDED', 'FAILED'}
ACTIVE_CLAIM_STATUSES = {'CLAIMED', 'TIMED_OUT'}

_START_KEYS = ('start', 'slot_start', 'begin', 'left', 'lo', 'from_slot', 'from', 'start_slot', 'slot_lo')
_END_KEYS = ('end', 'slot_end', 'finish', 'right', 'hi', 'to_slot', 'to', 'stop', 'slot_hi')
_RANGE_KEYS = ('slot', 'slots', 'range', 'window', 'interval', 'span', 'slot_range', 'time_slot', 'timeslot')
_KEY_KEYS = ('idempotency_key', 'request_id', 'client_token', 'claim_key', 'idempotent_key',
             'token', 'nonce', 'dedup_key', 'request_key', 'key')
_TASK_KEYS = ('task_id', 'partition_id', 'partition', 'task', 'name', 'partition_key', 'target', 'item')
_RUN_KEYS = ('run_id', 'batch_id', 'batch', 'run', 'batch_no', 'batch_key', 'case_batch')
_CASE_KEYS = ('case_id', 'case', 'group', 'group_id', 'case_key')
_WORKER_KEYS = ('worker', 'owner', 'claimer', 'assignee', 'executor', 'worker_id', 'agent', 'operator')
_DEP_KEYS = ('depends_on', 'dependencies', 'needs', 'upstream', 'prerequisites', 'requires', 'after', 'deps')
_TIMEOUT_KEYS = ('timeout_seconds', 'timeout', 'ttl', 'lease_seconds', 'lease', 'lease_ttl',
                 'ack_timeout', 'timeout_s', 'expires_in')
_NOW_KEYS = ('now', 'at', 'as_of', 'clock', 'current_time', 'claimed_at')


def utc_now():
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None):
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def parse_time(value):
    """宽松解析时间：datetime / ISO 字符串 / 数值（按秒级时间戳）。"""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def first_key(data, keys):
    for key in keys:
        if key in data and data[key] not in (None, ''):
            return data[key]
    return None


def coerce_int(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    # 兼容全角数字与千分位
    table = {ord('０') + i: ord('0') + i for i in range(10)}
    text = text.translate(table).replace(',', '').replace('，', '')
    return int(float(text))


def _range_from_value(value):
    """从 slot/range/window 等多种形态解析 [start, end)。"""
    if value is None:
        return None
    if isinstance(value, range):
        return [value.start, value.stop]
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and not isinstance(value[0], (list, tuple)):
            return [coerce_int(value[0]), coerce_int(value[1])]
        # 显式占用的离散时隙集合，如 [2,3] 或 [2,3,4]
        units = sorted(coerce_int(v) for v in value)
        if not units:
            return None
        if len(units) == 1:
            return [units[0], units[0] + 1]
        return [units[0], units[-1] + 1]
    if isinstance(value, dict):
        start = first_key(value, _START_KEYS)
        end = first_key(value, _END_KEYS)
        if start is not None and end is not None:
            return [coerce_int(start), coerce_int(end)]
        units = value.get('units')
        if units:
            return _range_from_value(units)
        return None
    text = str(value).strip()
    for left, right in (('[', ']'), ('(', ')')):
        if text.startswith(left) and text.endswith(right):
            text = text[1:-1]
    for sep in (',', '-', ':', '~', '—', '→'):
        if sep in text:
            parts = text.split(sep, 1)
            try:
                a, b = coerce_int(parts[0]), coerce_int(parts[1])
            except (ValueError, TypeError):
                continue
            # "2-4" 习惯上可能表示槽 2 到 4（含端点），统一按左闭右开 [2,4)
            return [a, b]
    number = coerce_int(text)
    return [number, number + 1]


def normalize_interval(data):
    """返回 ``(start, end)``；无法解析时返回 ``None``。"""
    start = first_key(data, _START_KEYS)
    end = first_key(data, _END_KEYS)
    if start is not None and end is not None:
        interval = [coerce_int(start), coerce_int(end)]
    else:
        interval = _range_from_value(first_key(data, _RANGE_KEYS))
    if interval is None:
        return None
    left, right = interval
    if right <= left:
        # 允许把 slots/duration 当作长度：仅给 start + slots
        duration = first_key(data, ('duration', 'length', 'count'))
        slots = data.get('slots')
        if duration is not None and start is not None and end is None:
            return coerce_int(start), coerce_int(start) + max(1, coerce_int(duration))
        if isinstance(slots, (int, float)) and not isinstance(slots, bool) and start is not None and end is None:
            return coerce_int(start), coerce_int(start) + max(1, coerce_int(slots))
        raise ValueError('INVALID_INTERVAL')
    return left, right


def intervals_overlap(a_start, a_end, b_start, b_end):
    """左闭右开区间重叠判定；首尾相接不重叠。"""
    return a_start < b_end and b_start < a_end


def normalize_outcome(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 'SUCCEEDED' if value else 'FAILED'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return 'SUCCEEDED' if int(value) == 0 else 'FAILED'
    token = str(value).strip().upper().replace('-', '_').replace(' ', '_')
    if token in SUCCESS_TOKENS or any(w in token for w in ('SUCCESS', 'COMPLET', 'FINISH', 'DONE', 'PASS', 'OK')):
        return 'SUCCEEDED'
    if token in FAILURE_TOKENS or any(w in token for w in ('FAIL', 'ERROR', 'UNSUCCESS', 'REJECT')):
        return 'FAILED'
    return token or None


def dependency_ids(value):
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except ValueError:
            pass
        return [part.strip() for part in text.replace(';', ',').split(',') if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def natural_key(*parts):
    raw = '|'.join('' if p is None else str(p) for p in parts)
    return 'nat:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def payload_fingerprint(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def as_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, 'get_json'):  # Flask request 兜底（一般在视图层已解析）
        data = value.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return dict(value)
