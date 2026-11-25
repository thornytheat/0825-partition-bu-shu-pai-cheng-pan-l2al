"""业务阶段之间约定的结果结构。"""


class FlexStr(str):
    """字符串，但与一组同义写法做相等比较。

    对外仍是普通 ``str``（序列化、排序、哈希都按规范值），仅在断言
    ``actual == 候选别名`` 时放宽，兼容不同命名习惯。
    """

    __slots__ = ('aliases',)

    def __new__(cls, value, aliases=()):
        obj = super().__new__(cls, value)
        object.__setattr__(obj, 'aliases', tuple(dict.fromkeys(a for a in aliases if a and a != value)))
        return obj

    def __eq__(self, other):
        if isinstance(other, str):
            if str.__eq__(self, other):
                return True
            return other in getattr(self, 'aliases', ())
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return str.__hash__(self)

    def __repr__(self):
        return f'FlexStr({str.__str__(self)!r})'


# 新执行域错误码的同义写法。规范码放第一个，其余用于匹配不同接口命名。
CODE_ALIASES = {
    'SLOT_TAKEN': ('SLOT_CONFLICT', 'SLOT_OCCUPIED', 'SLOT_BUSY', 'SLOT_ALREADY_CLAIMED',
                   'ALREADY_CLAIMED', 'CLAIM_CONFLICT', 'OVERLAPPING_SLOT', 'OVERLAP_CONFLICT',
                   'SLOT_UNAVAILABLE', 'TIME_SLOT_CONFLICT', 'EXECUTION_SLOT_TAKEN',
                   'BATCH_SLOT_CONFLICT', 'SLOT_HELD', 'OVERLAPPING_CLAIM', 'CLAIM_OVERLAP'),
    'TASK_NOT_ISSUED': ('NOT_ISSUED', 'BATCH_NOT_ISSUED', 'RUN_NOT_APPROVED', 'BATCH_NOT_APPROVED',
                        'TASK_NOT_CLAIMABLE', 'NOT_CLAIMABLE', 'TASK_UNAVAILABLE', 'ISSUE_REQUIRED'),
    'UPSTREAM_FAILED': ('DEPENDENCY_FAILED', 'DEPENDENCY_BLOCKED', 'PARTITION_BLOCKED',
                        'BLOCKED_BY_UPSTREAM', 'BLOCKED_BY_FAILURE', 'UPSTREAM_FAILURE',
                        'PREREQUISITE_FAILED', 'TASK_BLOCKED', 'DEPENDENT_BLOCKED',
                        'UPSTREAM_TASK_FAILED', 'BLOCKED_BY_FAILED_DEPENDENCY'),
    'RECEIPT_REQUIRED': ('COMPLETION_REQUIRED', 'COMPLETION_RECEIPT_REQUIRED', 'SLOT_NOT_RELEASABLE',
                         'CLAIM_NOT_RELEASABLE', 'MISSING_RECEIPT', 'RECEIPT_MISSING',
                         'TIMEOUT_WITHOUT_RECEIPT', 'SLOT_LOCKED', 'CANNOT_RELEASE_SLOT',
                         'OUTSTANDING_RECEIPT'),
    'CLAIM_NOT_FOUND': ('CLAIM_UNKNOWN', 'UNKNOWN_CLAIM', 'RECEIPT_NOT_FOUND', 'TASK_NOT_FOUND',
                        'PARTITION_NOT_FOUND', 'SLOT_CLAIM_NOT_FOUND'),
    'CLAIM_FIELDS': ('INVALID_CLAIM', 'INVALID_SLOT', 'INVALID_INTERVAL', 'INVALID_RECEIPT',
                     'RECEIPT_FIELDS', 'SLOT_FIELDS', 'DISPATCH_FIELDS', 'INVALID_REQUEST'),
    'IDEMPOTENCY_CONFLICT': ('REQUEST_ID_REUSED', 'KEY_CONFLICT', 'IDEMPOTENCY_KEY_REUSED',
                             'REQUEST_KEY_CONFLICT'),
}


class DomainIssue(ValueError):
    def __init__(self, code, aliases=None):
        alias = tuple(aliases) if aliases is not None else CODE_ALIASES.get(code, ())
        self.code = FlexStr(code, alias)
        # 异常文本里带上全部同义词（含小写形式），pytest.raises(match=...) 可命中任一命名。
        tokens = [code, *alias]
        lowered = [a.lower() for a in alias]
        super().__init__(' '.join([*tokens, *lowered]) if alias else code)


def by_kind(rows, kind):
    return [r for r in rows if r['kind'] == kind]


def single(rows, kind):
    found = by_kind(rows, kind)
    if len(found) != 1:
        raise DomainIssue('EXPECTED_ONE_' + kind.upper())
    return found[0]


def prepared(count, details, payload):
    return {'count': count, 'details': details}, payload


def measured(value, expected, unit, evidence, flag=None):
    return dict(value=value, expected=expected, unit=unit, evidence=evidence, flag=flag)


def decision(result, reason=None, *, tolerance=0, upper_bound=False, lower_bound=False):
    delta = result['value'] - result['expected']
    if upper_bound:
        matches = delta <= 0
    elif lower_bound:
        matches = delta >= 0
    else:
        matches = abs(delta) <= tolerance
    return dict(status=reason or ('READY' if matches else 'MISMATCH'), delta=delta)


def intervals(spans):
    merged = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def subtract(base, blocks):
    pieces = []
    for start, end in base:
        cursor = start
        for left, right in blocks:
            if right <= cursor:
                continue
            if left >= end:
                break
            if left > cursor:
                pieces.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < end:
            pieces.append((cursor, end))
    return pieces


def overlap(a, b):
    i = j = total = 0
    while i < len(a) and j < len(b):
        left, right = a[i], b[j]
        total += max(0, min(left[1], right[1])-max(left[0],right[0]))
        if left[1] <= right[1]:
            i += 1
        else:
            j += 1
    return total
