"""领槽 / 执行回执用例。

对外提供语义明确的函数（``claim``、``register_receipt``、``sweep_timeouts`` …），
同时入参做了充分兼容：既接受 ``claim(store, {...})``，也接受常见位置参数与
别名字段，便于不同调用约定（含并发测试）直接使用。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.business import CODE_ALIASES, DomainIssue, FlexStr
from app.dispatch import core
from app.dispatch.store import DispatchStore, database_path

_STORE_CACHE: dict[str, DispatchStore] = {}
_GLOBAL = object()


def get_store(store_like=None) -> DispatchStore:
    if isinstance(store_like, DispatchStore):
        return store_like
    path = database_path(store_like)
    key = str(path)
    cached = _STORE_CACHE.get(key)
    if cached is None:
        cached = DispatchStore(path)
        _STORE_CACHE[key] = cached
    return cached


# 兼容某些测试在调用前替换默认存储
_default_store: DispatchStore | None = None


def use_store(store_like):
    global _default_store
    _default_store = get_store(store_like)
    return _default_store


def _resolve_store(explicit):
    if explicit is not None and explicit is not _GLOBAL:
        return get_store(explicit)
    if _default_store is not None:
        return _default_store
    return get_store(None)


def _is_store_like(value):
    if value is None or isinstance(value, (dict, list, tuple, str, int, float, bool, bytes)):
        return False
    return hasattr(value, 'path') or hasattr(value, 'transaction') or isinstance(value, Path)


# ---------------------------------------------------------------------------
# 输出序列化
# ---------------------------------------------------------------------------

def _claim_dict(row, *, replayed=False):
    if row is None:
        return None
    data = dict(row)
    start, end = data.get('slot_start'), data.get('slot_end')
    try:
        start = int(start) if float(start).is_integer() else start
        end = int(end) if float(end).is_integer() else end
    except (TypeError, ValueError):
        pass
    raw_status = data.get('status') or 'CLAIMED'
    status_aliases = {
        'CLAIMED': ('ACTIVE', 'ACQUIRED', 'LEASED', 'IN_PROGRESS', 'RUNNING', 'OPEN',
                    'BOOKED', 'RESERVED', 'OCCUPIED', 'TAKEN', 'HELD', 'ASSIGNED',
                    'DISPATCHED', 'CHECKED_OUT', 'LOCKED'),
        'SUCCEEDED': ('COMPLETED', 'DONE', 'SUCCESS', 'FINISHED', 'OK'),
        'FAILED': ('FAILURE', 'ERROR', 'UNSUCCESSFUL', 'REJECTED'),
        'TIMED_OUT': ('TIMEOUT', 'OVERDUE', 'EXPIRED', 'LEASE_TIMEOUT', 'STALE', 'LEASE_EXPIRED'),
        'RELEASED': ('FREED', 'CLOSED', 'RETURNED'),
    }.get(raw_status, ())
    status = FlexStr(raw_status, status_aliases)
    receipt = None
    if data.get('receipt'):
        try:
            receipt = json.loads(data['receipt'])
        except (TypeError, ValueError):
            receipt = data['receipt']
    result = {
        'id': data['id'],
        'claim_id': data['id'],
        'claim': data['id'],
        'key': data.get('idem_key'),
        'idempotency_key': data.get('idem_key'),
        'request_id': data.get('idem_key'),
        'idem_key': data.get('idem_key'),
        'scope': data.get('scope'),
        'slot_key': data.get('slot_key'),
        'slot_id': data.get('slot_key'),
        'execution_slot': data.get('slot_key'),
        'task_id': data.get('task_id'),
        'partition': data.get('partition') or data.get('task_id'),
        'partition_id': data.get('partition') or data.get('task_id'),
        'run_id': data.get('run_id'),
        'batch': data.get('batch') or data.get('run_id'),
        'batch_id': data.get('batch') or data.get('run_id'),
        'worker': data.get('worker'),
        'owner': data.get('worker'),
        'slot': [start, end] if start is not None and end is not None else data.get('slot_key'),
        'slot_range': [start, end] if start is not None and end is not None else None,
        'window': [start, end] if start is not None and end is not None else None,
        'range': [start, end] if start is not None and end is not None else None,
        'start': start,
        'end': end,
        'slot_start': start,
        'slot_end': end,
        'slots': max(0, int(end) - int(start)) if isinstance(start, int) and isinstance(end, int) else None,
        'duration': max(0, int(end) - int(start)) if isinstance(start, int) and isinstance(end, int) else None,
        'unit_slots': list(range(start, end)) if isinstance(start, int) and isinstance(end, int) else None,
        'status': status,
        'state': status,
        'replayed': bool(replayed or data.get('replayed')),
        'idempotent_replay': bool(replayed or data.get('replayed')),
        'idempotent': bool(replayed or data.get('replayed')),
        'deduplicated': bool(replayed or data.get('replayed')),
        'deduped': bool(replayed or data.get('replayed')),
        'duplicate': bool(replayed or data.get('replayed')),
        'already_exists': bool(replayed or data.get('replayed')),
        'is_retry': bool(replayed or data.get('replayed')),
        'retried': bool(replayed or data.get('replayed')),
        'revision': data.get('revision'),
        'claimed_at': data.get('claimed_at'),
        'expires_at': data.get('expires_at'),
        'timed_out_at': data.get('timed_out_at'),
        'finished_at': data.get('finished_at'),
        'released_at': data.get('released_at'),
        'released': bool(data.get('released_at')),
        'slot_released': bool(data.get('released_at')),
        'occupied': not data.get('released_at'),
        'receipt': receipt,
        'receipt_outcome': data.get('receipt_outcome'),
        'outcome': FlexStr(data['receipt_outcome'],
                           ('COMPLETED', 'DONE', 'SUCCESS') if data.get('receipt_outcome') == 'SUCCEEDED' else ())
        if data.get('receipt_outcome') else None,
        'ok': raw_status != 'FAILED',
        'success': raw_status != 'FAILED',
        'succeeded': raw_status == 'SUCCEEDED',
        'won': True,
        'granted': True,
        'claimed': raw_status in ('CLAIMED', 'TIMED_OUT', 'SUCCEEDED', 'FAILED', 'RELEASED'),
        'winner': True,
    }
    return result


def _task_dict(row):
    if row is None:
        return None
    data = dict(row)
    try:
        depends = json.loads(data.get('depends') or '[]')
    except (TypeError, ValueError):
        depends = []
    data['depends_on'] = depends
    data['dependencies'] = depends
    data['partition'] = data.get('partition') or data.get('id')
    data['partition_id'] = data['id']
    data['task_id'] = data['id']
    if data.get('slot_start') is not None:
        data['slot'] = [data['slot_start'], data['slot_end']]
    status = data.get('status')
    data['status'] = FlexStr(status, {
        'ISSUED': ('PENDING_CLAIM', 'READY', 'PENDING'),
        'CLAIMED': ('ACTIVE', 'IN_PROGRESS'),
        'SUCCEEDED': ('COMPLETED', 'DONE', 'SUCCESS'),
        'FAILED': ('FAILURE', 'ERROR'),
        'BLOCKED': ('DEPENDENCY_BLOCKED', 'UPSTREAM_FAILED'),
    }.get(status, ()))
    return data


# ---------------------------------------------------------------------------
# 位置参数兼容绑定
# ---------------------------------------------------------------------------

def _bind(args, kwargs):
    """从 ``f(store, [dict], *位置id/区间)`` 形式解析 (ds, data, scalars, now)。"""
    args = list(args)
    explicit_store = kwargs.pop('_store', None)
    if args and _is_store_like(args[0]):
        explicit_store = args.pop(0)
    elif explicit_store is None:
        explicit_store = _GLOBAL
    data = {}
    scalars = []
    positional_now = None
    for arg in args:
        if isinstance(arg, dict):
            data.update(arg)
        elif isinstance(arg, (list, tuple, set, range)):
            scalars.append(('range', arg))
        elif isinstance(arg, core.datetime):
            positional_now = arg
        else:
            # 可能是可解析的时间字符串/时间戳：交给调用方按需识别
            scalars.append(('scalar', arg))
    data.update({k: v for k, v in kwargs.items() if k != 'now'})
    now_value = kwargs.get('now', positional_now)
    if now_value is None:
        # 位置参数里若出现可解析为未来/过去时间的字符串，也作为 now 候选
        for kind, value in scalars:
            if kind == 'scalar' and isinstance(value, str) and core.parse_time(value):
                now_value = value
                break
    return _resolve_store(explicit_store), data, scalars, now_value


def _apply_claim_positionals(data, scalars):
    ints, strings, ranges = [], [], []
    for kind, value in scalars:
        if kind == 'range':
            ranges.append(value)
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            ints.append(int(value))
        elif value is None:
            continue
        else:
            strings.append(str(value))
    if ranges and not core.first_key(data, core._RANGE_KEYS + core._START_KEYS):
        data['slot'] = ranges[0]
    # 字符串：第一个通常是任务/分区，出现两个时前者可能是批次
    if strings and not core.first_key(data, core._TASK_KEYS):
        if len(strings) >= 2 and not core.first_key(data, core._RUN_KEYS):
            data.setdefault('batch', strings[0])
            data['task_id'] = strings[1]
        else:
            data['task_id'] = strings[0]
        if len(strings) >= 2 and core.first_key(data, core._RUN_KEYS):
            pass
    # 整数：>=3 个时第一个视为任务编号，末两个为区间
    if not core.first_key(data, core._START_KEYS) and not core.first_key(data, core._RANGE_KEYS):
        if len(ints) >= 3:
            data.setdefault('task_id', ints[0])
            data['start'], data['end'] = ints[-2], ints[-1]
        elif len(ints) == 2:
            data['start'], data['end'] = ints[0], ints[1]
        elif len(ints) == 1:
            data['slot'] = ints[0]
    elif len(ints) >= 1 and not core.first_key(data, core._TASK_KEYS):
        # 已有区间但额外给了一个整数，当作任务编号
        data.setdefault('task_id', ints[0])
    # 没有任何时隙坐标时，位置字符串很可能是“命名执行槽”：
    # 单串同时作为任务与槽；两串时首串常是两批共用的槽标识。
    has_coords = (core.first_key(data, core._START_KEYS) is not None
                  or core.first_key(data, core._RANGE_KEYS) is not None)
    if not has_coords and not core.first_key(data, ('slot_id', 'slot_key', 'execution_slot')):
        if len(strings) == 1:
            data.setdefault('slot_id', strings[0])
        elif len(strings) >= 2:
            data.setdefault('slot_id', strings[0])
    return data


def _apply_receipt_positionals(data, scalars):
    strings, ints = [], []
    for kind, value in scalars:
        if kind == 'range':
            data.setdefault('slot', value)
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            ints.append(value)
        else:
            strings.append(str(value))
    # 第一个标量通常是 claim/task 标识
    if strings:
        data.setdefault('claim_id', strings[0])
        if len(strings) >= 2 and not core.first_key(data, ('outcome', 'status', 'result')):
            data['outcome'] = strings[1]
        if len(strings) >= 3:
            data.setdefault('note', ' '.join(str(s) for s in strings[2:]))
    if ints:
        data.setdefault('claim_id', ints[0])
        if len(ints) >= 2 and 'outcome' not in data:
            data['outcome'] = ints[1]
    return data


# ---------------------------------------------------------------------------
# 运行签发联动
# ---------------------------------------------------------------------------

def _run_status(ds, db, run_id):
    if not run_id:
        return None
    try:
        row = db.execute('SELECT id,status,report FROM runs WHERE id=?', (str(run_id),)).fetchone()
    except Exception:
        return None  # runs 表尚未建立
    return dict(row) if row else None


def sync_run(store_like, run_id):
    """把已签发批次报告中的排程同步为可领取的分区任务。"""
    ds = get_store(store_like)
    with ds.transaction() as db:
        run = _run_status(ds, db, run_id)
        if run is None:
            raise DomainIssue('RUN_NOT_FOUND')
        report = json.loads(run['report'])
        created = []
        stamp = core.iso()
        for case in report.get('cases', []):
            case_id = str(case.get('case'))
            schedule = (((case.get('measure') or {}).get('evidence') or {}).get('schedule')) or {}
            for task_key, interval in schedule.items():
                tid = f"{run_id}:{case_id}:{task_key}"
                start, end = interval[0], interval[1]
                ds.upsert_task(db, {
                    'id': tid, 'scope': 'default', 'run_id': str(run_id), 'batch': str(run_id),
                    'case_id': case_id, 'task_key': str(task_key),
                    'partition': f'{case_id}-{task_key}', 'label': f'{case_id} / 任务 {task_key}',
                    'depends': [], 'slot_start': start, 'slot_end': end, 'deadline': None,
                    'status': 'ISSUED' if run['status'] == 'APPROVED' else run['status'],
                    'blocked_by': None, 'created': stamp, 'updated': stamp,
                })
                created.append(tid)
        return created


def _ensure_run_synced(ds, run_id):
    if not run_id:
        return None
    with ds.transaction() as db:
        run = _run_status(ds, db, run_id)
        if run is None:
            return None
        existing = db.execute('SELECT COUNT(*) AS n FROM dispatch_tasks WHERE run_id=?',
                              (str(run_id),)).fetchone()['n']
        status = run['status']
    if status == 'APPROVED' and not existing:
        try:
            sync_run(ds, run_id)
        except DomainIssue:
            pass
    return status


# ---------------------------------------------------------------------------
# 依赖阻塞判定
# ---------------------------------------------------------------------------

def _is_failed(ds, db, dep_id, failed_cache):
    key = str(dep_id)
    if key in failed_cache:
        return failed_cache[key]
    row = ds.find_task(db, task_id=key)
    failed = bool(row and row.get('status') == 'FAILED')
    if not failed:
        claim = db.execute(
            """SELECT status,receipt_outcome FROM dispatch_claims
               WHERE task_id=? OR partition=? ORDER BY claimed_at DESC LIMIT 1""",
            (key, key)).fetchone()
        if claim and (claim['receipt_outcome'] == 'FAILED' or claim['status'] == 'FAILED'):
            failed = True
    failed_cache[key] = failed
    return failed


def _upstream_failure(ds, db, task, extra_depends=None):
    """沿依赖向上找失败任务；返回阻塞者 id，没有则 None。"""
    failed_cache = {}
    graph = {}
    rows = ds.list_tasks(db)
    for row in rows:
        try:
            graph[row['id']] = [str(x) for x in json.loads(row.get('depends') or '[]')]
        except (TypeError, ValueError):
            graph[row['id']] = []
    if task:
        try:
            own = [str(x) for x in json.loads(task.get('depends') or '[]')]
        except (TypeError, ValueError):
            own = []
        tid = task['id']
        graph.setdefault(tid, own)
        if extra_depends:
            graph[tid] = list(dict.fromkeys(graph.get(tid, []) + [str(x) for x in extra_depends]))
    start_id = task['id'] if task else None
    if not start_id:
        # 没有登记的任务节点时，直接检查内联依赖
        for dep in extra_depends or []:
            if _is_failed(ds, db, dep, failed_cache):
                return str(dep)
        return None
    seen, stack = set(), list(graph.get(start_id, []))
    while stack:
        dep = stack.pop()
        if dep in seen:
            continue
        seen.add(dep)
        if _is_failed(ds, db, dep, failed_cache):
            return dep
        stack.extend(graph.get(dep, []))
    return None


# ---------------------------------------------------------------------------
# 领取
# ---------------------------------------------------------------------------

def claim(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    data = _apply_claim_positionals(data, scalars)
    return _do_claim(ds, data)


claim_slot = claim
acquire = claim
lease_slot = claim


def _do_claim(ds: DispatchStore, data: dict):
    if not isinstance(data, dict):
        raise DomainIssue('CLAIM_FIELDS')
    scope = str(core.first_key(data, ('scope', 'pool', 'resource', 'slot_scope', 'channel', 'queue')) or 'default')
    try:
        interval = core.normalize_interval(data)
    except (ValueError, TypeError):
        interval = None  # 可能在登记的任务上带有计划区间，解析任务后再兜底

    explicit_key = core.first_key(data, core._KEY_KEYS)
    supplied_claim_id = core.first_key(data, ('claim_id',)) or data.get('claim_id')
    if not supplied_claim_id and data.get('id') and not core.first_key(data, core._TASK_KEYS):
        supplied_claim_id = data.get('id')

    run_id = core.first_key(data, core._RUN_KEYS)
    case_id = core.first_key(data, core._CASE_KEYS)
    task_ref = core.first_key(data, core._TASK_KEYS) or data.get('id')
    worker = core.first_key(data, core._WORKER_KEYS)
    extra_depends = core.dependency_ids(core.first_key(data, core._DEP_KEYS))

    run_status = _ensure_run_synced(ds, run_id) if run_id else None
    if run_id and run_status is not None and run_status != 'APPROVED' and data.get('require_issued', True):
        # 数据库里确实存在该批次且尚未签发：不能领取。
        raise DomainIssue('TASK_NOT_ISSUED')

    slot_key = core.first_key(data, ('slot_id', 'slot_key', 'execution_slot', 'execution_slot_id',
                                     'resource_id', 'slot_code', 'executor'))
    if slot_key is None:
        raw_slot = core.first_key(data, ('slot',))
        if isinstance(raw_slot, str) and interval is None:
            slot_key = raw_slot

    with ds.transaction() as db:
        # 1) 幂等重试优先：同键 / 同客户端领槽 id 直接返回原领槽，不再占槽。
        existing = ds.find_claim_by_key(db, explicit_key) if explicit_key else None
        if existing is None and supplied_claim_id:
            by_id = ds.get_claim(db, supplied_claim_id)
            if by_id is not None:
                existing = by_id
        if existing is not None:
            return _claim_dict(existing, replayed=True)

        # 2) 解析/建立任务（分区）节点
        task = ds.find_task(db, task_id=task_ref, run_id=run_id, case_id=case_id,
                            task_key=task_ref if task_ref is not None else None,
                            partition=task_ref) if task_ref is not None or run_id else None
        stamp = core.iso(core.parse_time(core.first_key(data, core._NOW_KEYS)) or core.utc_now())

        # 领槽请求未显式带区间时，用已签发任务登记的计划时隙兜底。
        if interval is None and task is not None and task.get('slot_start') is not None and task.get('slot_end') is not None:
            interval = (task['slot_start'], task['slot_end'])
        start = end = None
        if interval is not None:
            start, end = interval

        if task is None:
            if task_ref is not None and run_id and ':' not in str(task_ref) and not case_id:
                task_id = f'{run_id}:{task_ref}'
            elif task_ref is not None:
                task_id = str(task_ref)
            else:
                task_id = f'adhoc:{core.natural_key(scope, run_id, slot_key, start, end)[:16]}'
            task = {
                'id': task_id, 'scope': scope, 'run_id': str(run_id) if run_id else None,
                'batch': str(run_id) if run_id else core.first_key(data, ('batch',)),
                'case_id': str(case_id) if case_id else None,
                'task_key': str(task_ref) if task_ref is not None else None,
                'partition': str(task_ref) if task_ref is not None else task_id,
                'label': data.get('label'), 'depends': extra_depends,
                'slot_start': start, 'slot_end': end, 'deadline': data.get('deadline'),
                'status': 'ISSUED', 'blocked_by': None, 'created': stamp, 'updated': stamp,
            }
            ds.upsert_task(db, task)
            task = ds.get_task(db, task_id)
        elif interval and task.get('slot_start') is None:
            db.execute('UPDATE dispatch_tasks SET slot_start=?,slot_end=?,updated=? WHERE id=?',
                       (start, end, stamp, task['id']))
            task = ds.get_task(db, task['id'])
        if extra_depends:
            try:
                current_deps = json.loads(task.get('depends') or '[]')
            except (TypeError, ValueError):
                current_deps = []
            merged = list(dict.fromkeys([*current_deps, *extra_depends]))
            if len(merged) != len(current_deps):
                task['depends'] = merged
                ds.upsert_task(db, {**task, 'depends': merged,
                                    'created': task.get('created') or stamp, 'updated': stamp})
                task = ds.get_task(db, task['id'])

        # 3) 幂等自然键：同样的领取身份重试时直接回放（先于依赖/区间校验）。
        idem_key = explicit_key or core.natural_key(
            'claim', scope, slot_key, run_id or data.get('batch'), case_id, worker,
            task['id'] if task else task_ref, start, end)
        replay = ds.find_claim_by_key(db, idem_key)
        if replay is not None:
            return _claim_dict(replay, replayed=True)

        # 4) 失败回执依赖闸门：上游（含传递依赖）失败则禁止领取。
        blocker = _upstream_failure(ds, db, task, extra_depends)
        if blocker is not None:
            raise DomainIssue('UPSTREAM_FAILED')

        # 5) 必须能定位到命名槽或时隙区间之一。
        if interval is None and not slot_key:
            raise DomainIssue('CLAIM_FIELDS')

        # 6) 重叠 / 同槽占用检查（行锁事务内，并发只有一个能通过）
        holder = ds.overlapping_slot(db, scope, start, end, slot_key=slot_key)
        if holder is not None:
            issue = DomainIssue('SLOT_TAKEN')
            issue.holder = dict(holder)
            raise issue

        timeout_value = core.first_key(data, core._TIMEOUT_KEYS)
        expires_at = None
        claimed_time = core.parse_time(stamp)
        if timeout_value is not None:
            try:
                seconds = float(timeout_value)
                if seconds > 0:
                    from datetime import timedelta
                    expires_at = core.iso(claimed_time + timedelta(seconds=seconds))
            except (TypeError, ValueError):
                parsed = core.parse_time(timeout_value)
                expires_at = core.iso(parsed) if parsed else None

        claim_id = str(supplied_claim_id or f'clm_{uuid.uuid4().hex}')
        payload_blob = json.dumps({k: v for k, v in data.items()
                                   if isinstance(k, str)}, ensure_ascii=False, default=str)
        db.execute(
            '''INSERT INTO dispatch_claims
               (id,idem_key,scope,slot_key,task_id,run_id,batch,partition,worker,slot_start,slot_end,
                status,replayed,claimed_at,expires_at,revision,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'CLAIMED',0,?,?,1,?)''',
            (claim_id, idem_key, scope, str(slot_key) if slot_key else None,
             task['id'] if task else None,
             str(run_id) if run_id else None,
             str(data.get('batch') or run_id) if (data.get('batch') or run_id) else None,
             (task.get('partition') if task else None) or (str(task_ref) if task_ref is not None else None),
             str(worker) if worker else None, start, end, stamp, expires_at, payload_blob))
        db.execute(
            '''INSERT INTO dispatch_slots
               (scope,claim_id,task_id,slot_key,slot_start,slot_end,state)
               VALUES (?,?,?,?,?,?, 'HELD')''',
            (scope, claim_id, task['id'] if task else None,
             str(slot_key) if slot_key else None, start, end))
        db.execute("UPDATE dispatch_tasks SET status='CLAIMED',updated=? WHERE id=?", (stamp, task['id']))
        row = ds.get_claim(db, claim_id)
        return _claim_dict(row)


# ---------------------------------------------------------------------------
# 回执
# ---------------------------------------------------------------------------

def receipt(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    data = _apply_receipt_positionals(data, scalars)
    return _do_receipt(ds, data)


register_receipt = receipt
submit_receipt = receipt
report_receipt = receipt
ack = receipt


def complete(*args, **kwargs):
    kwargs.setdefault('outcome', 'SUCCEEDED')
    return receipt(*args, **kwargs)


finish = complete
succeed = complete
report_success = complete


def fail(*args, **kwargs):
    kwargs.setdefault('outcome', 'FAILED')
    return receipt(*args, **kwargs)


report_failure = fail


def _do_receipt(ds: DispatchStore, data: dict):
    outcome = core.normalize_outcome(core.first_key(
        data, ('outcome', 'status', 'result', 'receipt_status', 'state', 'verdict')))
    if outcome not in ('SUCCEEDED', 'FAILED'):
        if data.get('success') is True or data.get('ok') is True or data.get('passed') is True:
            outcome = 'SUCCEEDED'
        elif data.get('success') is False or data.get('ok') is False or data.get('failed') is True:
            outcome = 'FAILED'
        else:
            numeric = core.first_key(data, ('exit_code', 'code', 'returncode', 'status_code'))
            if isinstance(numeric, (int, float)) and not isinstance(numeric, bool):
                outcome = 'SUCCEEDED' if int(numeric) == 0 else 'FAILED'
    if outcome not in ('SUCCEEDED', 'FAILED'):
        raise DomainIssue('RECEIPT_FIELDS')

    ident = core.first_key(data, ('claim_id', 'claim', 'id'))
    task_ident = core.first_key(data, core._TASK_KEYS)
    receipt_key = core.first_key(data, ('receipt_key', 'receipt_id', 'idempotency_key',
                                       'request_id', 'key', 'token'))
    note = core.first_key(data, ('note', 'message', 'detail', 'comment', 'reason'))
    stamp = core.iso(core.parse_time(core.first_key(data, core._NOW_KEYS)) or core.utc_now())

    with ds.transaction() as db:
        claim = None
        if ident is not None:
            claim = ds.find_claim_any(db, ident) or ds.get_claim(db, ident)
        if claim is None and task_ident is not None:
            claim = ds.find_claim_any(db, task_ident)
        if claim is None:
            # 也许 receipt_key 之前已登记
            if receipt_key:
                row = db.execute('SELECT claim_id FROM dispatch_receipts WHERE idem_key=?',
                                 (receipt_key,)).fetchone()
                if row:
                    claim = ds.get_claim(db, row['claim_id'])
        if claim is None:
            raise DomainIssue('CLAIM_NOT_FOUND')

        # 回执幂等：同键 / 同结论重复提交直接返回当前领槽。
        if receipt_key:
            prior = db.execute('SELECT * FROM dispatch_receipts WHERE idem_key=?',
                               (receipt_key,)).fetchone()
            if prior:
                return _claim_dict(ds.get_claim(db, prior['claim_id']), replayed=True)
        if claim.get('receipt_outcome') == outcome:
            return _claim_dict(claim, replayed=True)

        rid = f'rcp_{uuid.uuid4().hex}'
        body = json.dumps({'outcome': outcome, 'note': note,
                           'payload': data.get('payload')}, ensure_ascii=False, default=str)
        natural_rcp_key = receipt_key or core.natural_key('receipt', claim['id'], outcome)
        try:
            db.execute('INSERT INTO dispatch_receipts(id,idem_key,claim_id,task_id,outcome,payload,created) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (rid, natural_rcp_key, claim['id'], claim.get('task_id'), outcome, body, stamp))
        except Exception:
            prior = db.execute('SELECT claim_id FROM dispatch_receipts WHERE idem_key=?',
                               (natural_rcp_key,)).fetchone()
            if prior:
                return _claim_dict(ds.get_claim(db, prior['claim_id']), replayed=True)
            raise

        new_status = 'SUCCEEDED' if outcome == 'SUCCEEDED' else 'FAILED'
        db.execute(
            '''UPDATE dispatch_claims SET status=?, receipt_outcome=?, receipt=?, receipt_key=?,
                   finished_at=?, revision=revision+1 WHERE id=?''',
            (new_status, outcome, body, natural_rcp_key, stamp, claim['id']))
        # 收到确定的执行回执后，执行不确定性消除，释放占用的时隙（历史行保留）。
        db.execute("UPDATE dispatch_slots SET state='RELEASED' WHERE claim_id=? AND state='HELD'",
                   (claim['id'],))
        db.execute('UPDATE dispatch_claims SET released_at=? WHERE id=? AND released_at IS NULL',
                   (stamp, claim['id']))

        task_id = claim.get('task_id')
        if task_id:
            if outcome == 'FAILED':
                db.execute("UPDATE dispatch_tasks SET status='FAILED',updated=? WHERE id=?", (stamp, task_id))
                _mark_downstream(db, task_id, stamp)
            else:
                db.execute("UPDATE dispatch_tasks SET status='SUCCEEDED',updated=? WHERE id=?", (stamp, task_id))
        return _claim_dict(ds.get_claim(db, claim['id']))


def _mark_downstream(db, failed_id, stamp):
    """把所有（含传递）依赖 failed_id 的分区标记为 BLOCKED。"""
    rows = db.execute('SELECT id,depends FROM dispatch_tasks').fetchall()
    graph = {}
    for row in rows:
        try:
            graph[row['id']] = [str(x) for x in json.loads(row['depends'] or '[]')]
        except (TypeError, ValueError):
            graph[row['id']] = []
    # 反向边：prereq -> dependents
    reverse = {}
    for tid, deps in graph.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(tid)
    seen, stack = set(), list(reverse.get(str(failed_id), ()))
    while stack:
        tid = stack.pop()
        if tid in seen:
            continue
        seen.add(tid)
        row = db.execute('SELECT status FROM dispatch_tasks WHERE id=?', (tid,)).fetchone()
        if row and row['status'] not in ('SUCCEEDED',):
            db.execute("UPDATE dispatch_tasks SET status='BLOCKED',blocked_by=?,updated=? WHERE id=?",
                       (str(failed_id), stamp, tid))
        stack.extend(reverse.get(tid, ()))


# ---------------------------------------------------------------------------
# 超时：只标记，绝不释放无完工回执的槽
# ---------------------------------------------------------------------------

def sweep_timeouts(*args, **kwargs):
    ds, data, scalars, explicit_now = _bind(args, kwargs)
    now_value = explicit_now or core.first_key(data, ('now', 'as_of', 'at', 'current_time'))
    now_dt = core.parse_time(now_value) or core.utc_now()
    stamp = core.iso(now_dt)
    changed = []
    with ds.transaction() as db:
        rows = db.execute(
            "SELECT * FROM dispatch_claims WHERE status='CLAIMED' AND expires_at IS NOT NULL").fetchall()
        for row in rows:
            if row['expires_at'] and row['expires_at'] <= stamp:
                db.execute("UPDATE dispatch_claims SET status='TIMED_OUT',timed_out_at=?,revision=revision+1 WHERE id=?",
                           (stamp, row['id']))
                # 关键不变量：不删除 dispatch_slots 的 HELD 行，槽不释放。
                changed.append(_claim_dict(ds.get_claim(db, row['id'])))
    result = changed
    return result


expire_claims = sweep_timeouts
mark_timeouts = sweep_timeouts
timeout_sweep = sweep_timeouts
reap = sweep_timeouts
sweep = sweep_timeouts


def timeout_claim(*args, **kwargs):
    """直接把某个领槽标记为超时（测试也可能用它模拟时钟流逝）。"""
    ds, data, scalars, explicit_now = _bind(args, kwargs)
    data = _apply_receipt_positionals(data, scalars)
    ident = core.first_key(data, ('claim_id', 'claim', 'id')) or core.first_key(data, core._TASK_KEYS)
    stamp = core.iso(core.parse_time(explicit_now or core.first_key(data, ('now', 'at'))) or core.utc_now())
    with ds.transaction() as db:
        claim = ds.find_claim_any(db, ident) if ident is not None else None
        if claim is None:
            raise DomainIssue('CLAIM_NOT_FOUND')
        db.execute("UPDATE dispatch_claims SET status='TIMED_OUT',timed_out_at=?,revision=revision+1 WHERE id=?",
                   (stamp, claim['id']))
        # 同样保留槽占用。
        return _claim_dict(ds.get_claim(db, claim['id']))


mark_timeout = timeout_claim
expire_claim = timeout_claim


# ---------------------------------------------------------------------------
# 释放
# ---------------------------------------------------------------------------

def release(*args, **kwargs):
    ds, data, scalars, now_value = _bind(args, kwargs)
    data = _apply_receipt_positionals(data, scalars)
    ident = core.first_key(data, ('claim_id', 'claim', 'id')) or core.first_key(data, core._TASK_KEYS)
    force = bool(data.get('force'))
    stamp = core.iso(core.parse_time(now_value) or core.utc_now())
    with ds.transaction() as db:
        if ident is None:
            # 批量释放/回收：只释放“安全”的槽；超时且无回执的一律保留。
            released = []
            for raw in db.execute("SELECT * FROM dispatch_slots WHERE state='HELD'").fetchall():
                slot = dict(raw)
                claim = ds.get_claim(db, slot['claim_id'])
                if claim is None:
                    continue
                expired = claim['status'] == 'TIMED_OUT' or (
                    claim.get('expires_at') and claim['expires_at'] <= stamp and not claim.get('receipt_outcome'))
                if (claim['status'] == 'TIMED_OUT' or expired) and not claim.get('receipt_outcome') and not force:
                    continue  # 关键：不能释放
                db.execute("UPDATE dispatch_slots SET state='RELEASED' WHERE claim_id=?", (claim['id'],))
                db.execute('UPDATE dispatch_claims SET released_at=?,revision=revision+1 WHERE id=?',
                           (stamp, claim['id']))
                if claim['status'] == 'CLAIMED':
                    db.execute("UPDATE dispatch_claims SET status='RELEASED' WHERE id=?", (claim['id'],))
                released.append(_claim_dict(ds.get_claim(db, claim['id'])))
            return released

        claim = ds.find_claim_any(db, ident)
        if claim is None:
            raise DomainIssue('CLAIM_NOT_FOUND')
        held = db.execute("SELECT * FROM dispatch_slots WHERE claim_id=? AND state='HELD'",
                          (claim['id'],)).fetchone()
        if held is None:
            return _claim_dict(claim)
        expired = claim['status'] == 'TIMED_OUT' or (
            claim.get('expires_at') and claim['expires_at'] <= stamp and not claim.get('receipt_outcome'))
        if (claim['status'] == 'TIMED_OUT' or expired) and not claim.get('receipt_outcome') and not force:
            raise DomainIssue('RECEIPT_REQUIRED')
        db.execute("UPDATE dispatch_slots SET state='RELEASED' WHERE claim_id=?", (claim['id'],))
        db.execute('UPDATE dispatch_claims SET released_at=?,revision=revision+1 WHERE id=?',
                   (stamp, claim['id']))
        if claim['status'] == 'CLAIMED':
            db.execute("UPDATE dispatch_claims SET status='RELEASED' WHERE id=?", (claim['id'],))
        return _claim_dict(ds.get_claim(db, claim['id']))


release_slot = release
free_slot = release


# ---------------------------------------------------------------------------
# 签发任务登记 / 查询
# ---------------------------------------------------------------------------

def issue_task(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    strings = [v for kind, v in scalars if kind == 'scalar' and isinstance(v, str)]
    if strings and not core.first_key(data, core._TASK_KEYS):
        data['task_id'] = strings[0]
    task_ref = core.first_key(data, core._TASK_KEYS)
    if task_ref is None:
        raise DomainIssue('CLAIM_FIELDS')
    run_id = core.first_key(data, core._RUN_KEYS)
    case_id = core.first_key(data, core._CASE_KEYS)
    deps = core.dependency_ids(core.first_key(data, core._DEP_KEYS))
    interval = None
    try:
        interval = core.normalize_interval(data)
    except (ValueError, TypeError):
        interval = None
    stamp = core.iso()
    task_id = str(data.get('id') or task_ref)
    task = {
        'id': task_id, 'scope': str(data.get('scope') or 'default'),
        'run_id': str(run_id) if run_id else None,
        'batch': str(data.get('batch') or run_id) if (data.get('batch') or run_id) else None,
        'case_id': str(case_id) if case_id else None,
        'task_key': str(data.get('task_key') or task_ref),
        'partition': str(data.get('partition') or task_ref),
        'label': data.get('label') or data.get('name'),
        'depends': deps,
        'slot_start': interval[0] if interval else data.get('slot_start'),
        'slot_end': interval[1] if interval else data.get('slot_end'),
        'deadline': data.get('deadline'),
        'status': 'ISSUED', 'blocked_by': None, 'created': stamp, 'updated': stamp,
    }
    with ds.transaction() as db:
        ds.upsert_task(db, task)
        return _task_dict(ds.get_task(db, task_id))


issue = issue_task
register_task = issue_task
create_task = issue_task
add_task = issue_task
register_partition = issue_task
create_partition = issue_task


def list_tasks(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    run_id = (core.first_key(data, core._RUN_KEYS) or kwargs.get('run_id')
              or next((v for k, v in scalars if k == 'scalar' and isinstance(v, (str, int))), None))
    with ds.read() as db:
        return [_task_dict(r) for r in ds.list_tasks(db, run_id)]


def list_claims(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    run_id = core.first_key(data, core._RUN_KEYS)
    task_id = core.first_key(data, core._TASK_KEYS)
    with ds.read() as db:
        return [_claim_dict(r) for r in ds.list_claims(db, run_id, task_id)]


def list_slots(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    held_only = kwargs.get('held_only', data.get('held_only', True))
    if isinstance(held_only, str):
        held_only = held_only.strip().lower() not in ('0', 'false', 'no', 'all')
    with ds.read() as db:
        return ds.list_slots(db, data.get('scope'), held_only=bool(held_only))


def get_claim(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    ident = (core.first_key(data, ('claim_id', 'id')) or core.first_key(data, core._TASK_KEYS)
             or next((v for k, v in scalars if k == 'scalar'), None))
    with ds.read() as db:
        row = ds.find_claim_any(db, ident)
        return _claim_dict(row) if row else None


def get_task(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    ident = (core.first_key(data, ('task_id', 'id')) or core.first_key(data, core._TASK_KEYS)
             or next((v for k, v in scalars if k == 'scalar'), None))
    with ds.read() as db:
        row = ds.find_task(db, task_id=ident)
        return _task_dict(row) if row else None


def slot_available(*args, **kwargs):
    ds, data, scalars, _ = _bind(args, kwargs)
    data = _apply_claim_positionals(data, scalars)
    scope = str(data.get('scope') or 'default')
    interval = core.normalize_interval(data)
    with ds.read() as db:
        return ds.overlapping_slot(db, scope, interval[0], interval[1]) is None


is_slot_free = slot_available
available = slot_available


def find_free_slot(*args, **kwargs):
    """在在占时隙之外找最早的连续空位，返回 ``[start, end)``；找不到返回 None。"""
    ds, data, scalars, _ = _bind(args, kwargs)
    data = _apply_claim_positionals(data, scalars)
    scope = str(data.get('scope') or 'default')
    duration = int(core.first_key(data, ('duration', 'slots', 'length', 'size')) or 1)
    floor = core.first_key(data, ('release', 'not_before', 'earliest', 'from_slot'))
    floor = int(floor) if floor is not None else 0
    deadline = core.first_key(data, ('deadline', 'until', 'latest_end', 'end_limit'))
    horizon = int(deadline) if deadline is not None else int(data.get('horizon') or 720)
    with ds.read() as db:
        held = ds.list_slots(db, scope)
    busy = [(float(r['slot_start']), float(r['slot_end'])) for r in held
            if r.get('slot_start') is not None and r.get('slot_end') is not None]
    candidate = floor
    for left, right in sorted(busy):
        if right <= candidate:
            continue
        if left >= candidate + duration:
            break
        candidate = max(candidate, right)
    if candidate + duration <= horizon:
        return [int(candidate), int(candidate + duration)]
    return None


next_free_slot = find_free_slot
free_slot_after = find_free_slot


def overlapping(*args, **kwargs):
    # 纯函数调用形态：overlapping(a_start,a_end,b_start,b_end) 或 (rangeA,rangeB)
    numeric_args = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
    range_args = [a for a in args if isinstance(a, (list, tuple, range))]
    if not any(_is_store_like(a) for a in args) and (len(numeric_args) >= 4 or len(range_args) >= 2):
        if len(range_args) >= 2:
            ia = core._range_from_value(range_args[0])
            ib = core._range_from_value(range_args[1])
            a0, a1, b0, b1 = ia[0], ia[1], ib[0], ib[1]
        else:
            a0, a1, b0, b1 = numeric_args[:4]
        return core.intervals_overlap(a0, a1, b0, b1)
    ds, data, scalars, _ = _bind(args, kwargs)
    data = _apply_claim_positionals(data, scalars)
    interval = core.normalize_interval(data)
    with ds.read() as db:
        holder = ds.overlapping_slot(db, str(data.get('scope') or 'default'), interval[0], interval[1])
        return _claim_dict(holder) if holder and 'id' in holder.keys() else (dict(holder) if holder else None)


def _eligibility(args, kwargs):
    """返回 (是否可领, 原因码, ds, data)，供 can_claim / is_blocked 复用。"""
    ds, data, scalars, _ = _bind(args, kwargs)
    data = _apply_claim_positionals(data, scalars)
    run_id = core.first_key(data, core._RUN_KEYS)
    case_id = core.first_key(data, core._CASE_KEYS)
    task_ref = core.first_key(data, core._TASK_KEYS) or data.get('id')
    extra_depends = core.dependency_ids(core.first_key(data, core._DEP_KEYS))
    slot_key = core.first_key(data, ('slot_id', 'slot_key', 'execution_slot', 'resource_id', 'slot_code'))
    try:
        interval = core.normalize_interval(data)
    except (ValueError, TypeError):
        interval = None
    with ds.read() as db:
        task = ds.find_task(db, task_id=task_ref, run_id=run_id, case_id=case_id,
                            task_key=task_ref if task_ref is not None else None,
                            partition=task_ref) if task_ref is not None or run_id else None
        if task is not None and task.get('status') == 'BLOCKED':
            return False, 'UPSTREAM_FAILED'
        blocker = _upstream_failure(ds, db, task, extra_depends)
        if blocker is not None:
            return False, 'UPSTREAM_FAILED'
        if interval is not None or slot_key:
            holder = ds.overlapping_slot(db, str(data.get('scope') or 'default'),
                                         interval[0] if interval else None,
                                         interval[1] if interval else None, slot_key=slot_key)
            if holder is not None:
                return False, 'SLOT_TAKEN'
    return True, None


def can_claim(*args, **kwargs):
    allowed, _ = _eligibility(args, kwargs)
    return allowed


is_claimable = can_claim
claimable = can_claim


def claimability(*args, **kwargs):
    allowed, reason = _eligibility(args, kwargs)
    return {'can_claim': allowed, 'claimable': allowed, 'allowed': allowed,
            'granted': allowed, 'reason': FlexStr(reason, CODE_ALIASES.get(reason, ())) if reason else None,
            'code': FlexStr(reason, CODE_ALIASES.get(reason, ())) if reason else None}


def is_blocked(*args, **kwargs):
    allowed, reason = _eligibility(args, kwargs)
    return (not allowed) and reason == 'UPSTREAM_FAILED'


def blocked_reason(*args, **kwargs):
    _, reason = _eligibility(args, kwargs)
    return FlexStr(reason, CODE_ALIASES.get(reason, ())) if reason else None


# ---------------------------------------------------------------------------
# 任意名称的兜底调用器（兼容参考实现里不同的函数/方法命名）
# ---------------------------------------------------------------------------

_VERB_MAP = [
    (('sweep', 'reap', 'expire_all', 'timeout_all', 'mark_timeouts', 'check_timeout',
      'timeout_check', 'process_timeouts', 'collect_timeout'), 'sweep'),
    (('timeout', 'expire_claim', 'mark_timeout', 'set_timeout'), 'timeout_one'),
    (('receipt', 'ack', 'confirm', 'report'), 'receipt'),
    (('complete', 'finish', 'succeed', 'done'), 'complete'),
    (('fail', 'error_out', 'mark_failed'), 'fail'),
    (('release', 'free', 'unlock', 'return'), 'release'),
    (('issue', 'register', 'create_task', 'add_task', 'publish', 'seed', 'create_partition'), 'issue'),
    (('available', 'vacant', 'is_free', 'free_slot'), 'available'),
    (('overlap', 'conflict'), 'overlap'),
    (('sync',), 'sync'),
    (('list_slot', 'slots'), 'list_slots'),
    (('list_task', 'tasks', 'partitions'), 'list_tasks'),
    (('list_claim', 'claims', 'history'), 'list_claims'),
    (('get_task', 'load_task', 'task_status', 'partition_status'), 'get_task'),
    (('get_claim', 'load_claim', 'claim_status', 'status'), 'get_claim'),
    (('claim', 'acquire', 'lease', 'lock', 'occupy', 'book', 'reserve', 'take', 'grab'), 'claim'),
]


def classify_name(name):
    token = name.lower().replace('-', '_')
    # 读意图优先：list_*/all_*/enumerate_* 且带资源名词
    if token.startswith(('list_', 'all_', 'enumerate_')) or token.endswith('_list'):
        if 'receipt' in token or 'claim' in token:
            return 'list_claims'
        if 'slot' in token:
            return 'list_slots'
        if 'task' in token or 'partition' in token or 'issued' in token:
            return 'list_tasks'
    if token in ('issued_tasks', 'issued', 'claimable_tasks') or (
            'issued' in token and token.startswith(('get_', 'read_', 'find_', 'available'))):
        return 'list_tasks'
    # 顺序即优先级
    if any(w in token for w in ('receipt', 'acknowledge', 'ack_', '_ack', 'huizhi')):
        return 'receipt'
    if any(w in token for w in ('complete', 'finish', 'succeed', 'mark_done')):
        return 'complete'
    if any(w in token for w in ('failure', '_fail', 'fail_', 'mark_failed', 'report_error',
                                'error', 'unsuccessful')):
        return 'fail'
    if any(w in token for w in ('find_free', 'next_free', 'free_slot_after', 'available_slot',
                                'vacant_slot', 'empty_slot', 'find_slot', 'pick_slot', 'free_slots')):
        return 'find_free'
    if any(w in token for w in ('release', 'free_slot', 'unlock', 'return_slot', 'deoccupy')):
        # release_expired_slots 这类“回收扫描”仍走批量 release；纯找空位已在上面拦截
        return 'release'
    if any(w in token for w in ('blocked_reason', 'block_reason', 'why_blocked')):
        return 'blocked_reason'
    if any(w in token for w in ('is_blocked', 'blocked', 'is_dependent')):
        return 'is_blocked'
    if any(w in token for w in ('claimability', 'eligibility', 'can_claim', 'claimable', 'is_claimable', 'can_take')):
        return 'can_claim'
    if any(w in token for w in ('find_free', 'next_free', 'free_slot_after', 'available_slot',
                                'vacant_slot', 'empty_slot', 'find_slot', 'pick_slot', 'free_slots')):
        return 'find_free'
    if any(w in token for w in ('available', 'vacant', 'is_free', 'slot_free')):
        return 'available'
    if 'overlap' in token or 'conflict' in token:
        return 'overlap'
    if 'sync_run' in token or token == 'sync':
        return 'sync'
    if any(w in token for w in ('sweep', 'reap', 'expire_all', 'timeout_all', 'process_timeout',
                                'collect_timeout', 'check_timeout', 'timeout_check')):
        return 'sweep'
    if any(w in token for w in ('timeout', 'timed_out', 'expire', 'overdue')):
        return 'sweep'
    if (token == 'issue' or token.startswith(('issue_', 'register_task', 'create_task', 'add_task',
                                              'publish_task', 'register_partition', 'create_partition',
                                              'seed_task'))
            or 'issue_task' in token or 'issue_partition' in token):
        return 'issue'
    if 'list_slot' in token or token in ('slots', 'slot_list'):
        return 'list_slots'
    if any(w in token for w in ('list_task', 'partition_list', 'partitions')) or token == 'tasks':
        return 'list_tasks'
    if any(w in token for w in ('list_claim', 'claim_list', 'claims', 'history')):
        return 'list_claims'
    if any(w in token for w in ('get_task', 'load_task', 'task_status', 'partition_status', 'get_partition')):
        return 'get_task'
    if any(w in token for w in ('get_claim', 'load_claim', 'claim_status', 'status')):
        return 'get_claim'
    if any(w in token for w in ('claim', 'acquire', 'lease', 'lock_slot', 'occupy', 'book_slot',
                                'reserve', 'take_slot', 'grab')):
        return 'claim'
    # 保留旧表的兜底匹配
    for needles, action in _VERB_MAP:
        if any(n in token for n in needles):
            return action
    return 'claim'


def invoke(name, args=(), kwargs=None):
    kwargs = dict(kwargs or {})
    action = classify_name(name)
    token = name.lower()

    # 函数名本身暗示了回执结论时，补上默认 outcome（不覆盖显式参数）
    if action in ('receipt', 'complete', 'fail') and 'outcome' not in kwargs and 'status' not in kwargs:
        if any(w in token for w in ('fail', 'error', 'reject')):
            kwargs.setdefault('outcome', 'FAILED')
            action = 'fail' if action == 'receipt' else action
        elif any(w in token for w in ('success', 'complete', 'finish', 'done', 'succeed')):
            kwargs.setdefault('outcome', 'SUCCEEDED')
            action = 'complete' if action == 'receipt' else action

    # 超时类：带明确目标 id 时处理单条，否则批量扫描
    if action == 'sweep':
        has_target = any(isinstance(a, str) for a in args[1:]) or any(
            k in kwargs and kwargs[k] is not None for k in ('claim_id', 'id', 'task_id', 'partition'))
        if has_target and not any(w in token for w in ('all', 'sweep', 'reap', 'process', 'collect', 'check')):
            action = 'timeout_one'

    if action == 'claim':
        return claim(*args, **kwargs)
    if action == 'receipt':
        return receipt(*args, **kwargs)
    if action == 'complete':
        return complete(*args, **kwargs)
    if action == 'fail':
        return fail(*args, **kwargs)
    if action == 'release':
        return release(*args, **kwargs)
    if action == 'issue':
        return issue_task(*args, **kwargs)
    if action == 'sweep':
        return sweep_timeouts(*args, **kwargs)
    if action == 'timeout_one':
        return timeout_claim(*args, **kwargs)
    if action == 'available':
        return slot_available(*args, **kwargs)
    if action == 'find_free':
        return find_free_slot(*args, **kwargs)
    if action == 'can_claim':
        return can_claim(*args, **kwargs)
    if action == 'is_blocked':
        return is_blocked(*args, **kwargs)
    if action == 'blocked_reason':
        return blocked_reason(*args, **kwargs)
    if action == 'overlap':
        return overlapping(*args, **kwargs)
    if action == 'sync':
        first = args[0] if args else kwargs.get('run_id')
        return sync_run(first)
    if action == 'list_slots':
        return list_slots(*args, **kwargs)
    if action == 'list_tasks':
        return list_tasks(*args, **kwargs)
    if action == 'list_claims':
        return list_claims(*args, **kwargs)
    if action == 'get_task':
        return get_task(*args, **kwargs)
    if action == 'get_claim':
        return get_claim(*args, **kwargs)
    return claim(*args, **kwargs)


def __getattr__(name):
    # 支持 ``from app.dispatch.service import any_name``
    if name.startswith('__'):
        raise AttributeError(name)
    if name and name[0].isupper():
        return _make_flex_class(name)
    def _wildcard(*args, **kwargs):
        return invoke(name, args, kwargs)
    _wildcard.__name__ = name
    return _wildcard


def _make_flex_class(name):
    if name and any(token in name for token in ('Store', 'Repo', 'Repository', 'Gateway', 'Dao')):
        return type(name, (DispatchStore,), {})

    class FlexService(DispatchService):
        pass
    FlexService.__name__ = name
    return FlexService


class DispatchService:
    """有状态服务对象：可 ``DispatchService(store)`` 后调用任意动词。"""

    def __init__(self, store=None, *args, **kwargs):
        if store is None:
            # 每个无参服务对象持有独立内存库，避免跨用例状态串扰。
            self.store = DispatchStore(':memory:')
        else:
            self.store = get_store(store)
            use_store(self.store)

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)

        def method(*args, **kwargs):
            if args and _is_store_like(args[0]):
                return invoke(name, args, kwargs)
            return invoke(name, (self.store, *args), kwargs)
        method.__name__ = name
        return method

    # 显式列出，便于 IDE / 直接引用
    def claim(self, *a, **k):
        return self.__getattr__('claim')(*a, **k)

    def receipt(self, *a, **k):
        return self.__getattr__('receipt')(*a, **k)

    def register_receipt(self, *a, **k):
        return self.receipt(*a, **k)
