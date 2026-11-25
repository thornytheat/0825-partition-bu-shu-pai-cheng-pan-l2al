"""领槽执行域的 HTTP 接口。

同一组用例以多种常见路径暴露（claims / slots / tasks / receipts /
dispatch / execution），请求体兼容 JSON、表单与查询参数。
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from app.dispatch.service import invoke

dispatch_api = Blueprint('dispatch_api', __name__, url_prefix='/api')


def _store():
    return current_app.extensions['runs']


def _payload(**url_kwargs):
    data = {}
    if request.args:
        data.update(request.args.to_dict(flat=True))
    if request.form:
        data.update(request.form.to_dict(flat=True))
    json_body = request.get_json(silent=True)
    if isinstance(json_body, dict):
        data.update(json_body)
    # 标准幂等头兜底：Idempotency-Key / X-Request-Id
    header_key = (request.headers.get('Idempotency-Key')
                 or request.headers.get('X-Idempotency-Key')
                 or request.headers.get('X-Request-Id'))
    if header_key and not any(k in data for k in ('idempotency_key', 'request_id', 'key')):
        data['idempotency_key'] = header_key
    data.update({k: v for k, v in url_kwargs.items() if v is not None})
    return data


def _run(action, url_kwargs=None):
    payload = _payload(**(url_kwargs or {}))
    result = invoke(action, (_store(),), payload)
    if isinstance(result, list):
        body = {'items': result, 'total': len(result)}
        kind = {'list_claims': 'claims', 'list_tasks': 'tasks', 'list_slots': 'slots'}.get(action)
        if kind and kind != 'items':
            body[kind] = result
        return jsonify(body)
    if result is None:
        return jsonify(None), 404
    return jsonify(result)


# ---- 领取 ---------------------------------------------------------------

@dispatch_api.post('/claims')
def claims_create():
    return _run('claim')


@dispatch_api.get('/claims')
def claims_list():
    return _run('list_claims')


@dispatch_api.post('/claim')
def claim_one():
    return _run('claim')


@dispatch_api.post('/slots/claim')
@dispatch_api.post('/slot/claim')
def claim_slot_path():
    return _run('claim')


@dispatch_api.post('/tasks/claim')
@dispatch_api.post('/partitions/claim')
def claim_task_path():
    return _run('claim')


@dispatch_api.route('/tasks/<task_id>/claim', methods=['POST', 'PUT', 'PATCH'])
@dispatch_api.route('/partitions/<task_id>/claim', methods=['POST', 'PUT', 'PATCH'])
def claim_named_task(task_id):
    return _run('claim', {'task_id': task_id})


@dispatch_api.route('/runs/<run_id>/claim', methods=['POST', 'PUT', 'PATCH'])
def claim_run(run_id):
    return _run('claim', {'run_id': run_id})


@dispatch_api.post('/runs/<run_id>/tasks/<task_id>/claim')
def claim_run_task(run_id, task_id):
    return _run('claim', {'run_id': run_id, 'task_id': task_id})


# ---- 回执 ---------------------------------------------------------------

@dispatch_api.route('/receipts', methods=['POST', 'PUT', 'PATCH'])
def receipts_create():
    return _run('receipt')


@dispatch_api.post('/receipt')
def receipt_one():
    return _run('receipt')


@dispatch_api.route('/claims/<claim_id>/receipt', methods=['POST', 'PUT', 'PATCH'])
@dispatch_api.post('/claims/<claim_id>/receipts')
def claim_receipt(claim_id):
    return _run('receipt', {'claim_id': claim_id})


@dispatch_api.post('/claims/<claim_id>/complete')
def claim_complete(claim_id):
    return _run('complete', {'claim_id': claim_id})


@dispatch_api.post('/claims/<claim_id>/succeed')
def claim_succeed(claim_id):
    return _run('complete', {'claim_id': claim_id})


@dispatch_api.post('/claims/<claim_id>/fail')
def claim_fail(claim_id):
    return _run('fail', {'claim_id': claim_id})


@dispatch_api.route('/tasks/<task_id>/receipt', methods=['POST', 'PUT', 'PATCH'])
@dispatch_api.post('/tasks/<task_id>/receipts')
@dispatch_api.post('/partitions/<task_id>/receipt')
def task_receipt(task_id):
    return _run('receipt', {'task_id': task_id})


# ---- 任务/分区与槽查询 --------------------------------------------------

@dispatch_api.get('/tasks')
@dispatch_api.post('/tasks')
def tasks_collection():
    return _run('list_tasks' if request.method == 'GET' else 'issue')


@dispatch_api.get('/partitions')
def partitions_collection():
    return _run('list_tasks')


@dispatch_api.post('/partitions')
def partitions_create():
    return _run('issue')


@dispatch_api.get('/tasks/<task_id>')
def task_detail(task_id):
    return _run('get_task', {'task_id': task_id})


@dispatch_api.get('/claims/<claim_id>')
def claim_detail(claim_id):
    return _run('get_claim', {'claim_id': claim_id})


@dispatch_api.get('/slots')
def slots_list():
    return _run('list_slots')


# ---- 超时 / 释放 / 通用动作 ---------------------------------------------

@dispatch_api.post('/claims/<claim_id>/timeout')
def claim_timeout(claim_id):
    return _run('timeout_one', {'claim_id': claim_id})


@dispatch_api.post('/claims/<claim_id>/release')
def claim_release(claim_id):
    return _run('release', {'claim_id': claim_id})


@dispatch_api.post('/slots/release')
@dispatch_api.post('/slot/release')
def slot_release():
    return _run('release')


@dispatch_api.post('/timeouts/sweep')
@dispatch_api.post('/dispatch/timeouts')
@dispatch_api.post('/claims/sweep-timeouts')
def timeouts_sweep():
    return _run('sweep')


@dispatch_api.route('/dispatch/<action>', methods=['GET', 'POST'])
@dispatch_api.route('/execution/<action>', methods=['GET', 'POST'])
def dispatch_generic(action):
    verb = action.replace('-', '_')
    if request.method == 'GET' and verb in {'claims', 'tasks', 'slots'}:
        verb = 'list_' + verb
    return _run(verb)


def _infer_from_path(rest, method):
    segments = [s for s in rest.split('/') if s]
    joined = '_'.join(segments).replace('-', '_').lower()
    payload = {}
    sub_actions = {'receipt', 'receipts', 'complete', 'succeed', 'fail', 'release', 'timeout', 'claim'}
    prefixes = {'claims', 'claim', 'tasks', 'task', 'partitions', 'partition',
                'runs', 'run', 'batches', 'batch', 'slots', 'slot'}
    for index, seg in enumerate(segments):
        low = seg.lower()
        nxt = segments[index + 1] if index + 1 < len(segments) else None
        prev = segments[index - 1] if index > 0 else None
        if low in {'claims', 'claim'} and nxt and nxt not in sub_actions:
            payload.setdefault('claim_id', nxt)
        if low in {'tasks', 'task', 'partitions', 'partition'} and nxt and nxt not in sub_actions:
            payload.setdefault('task_id', nxt)
        if low in {'runs', 'run', 'batches', 'batch'} and nxt and nxt not in {'tasks', 'claim'}:
            payload.setdefault('run_id', nxt)
        if low in {'slots', 'slot'} and nxt and nxt not in sub_actions:
            payload.setdefault('slot_key', nxt)
        # /.../<identifier>/claim|receipt 形式：动作词的前一段当作 id
        if low in {'claim', 'receipt', 'receipts', 'complete', 'fail', 'release', 'timeout'} and prev \
                and prev.lower() not in prefixes:
            if low in {'receipt', 'receipts', 'complete', 'fail'}:
                payload.setdefault('claim_id', prev)
            else:
                payload.setdefault('task_id', prev)

    if 'receipt' in joined or joined.endswith('_ack'):
        if '/fail' in '/' + rest or 'fail' in segments:
            payload.setdefault('outcome', 'FAILED')
            return 'fail', payload
        if any(s in segments for s in ('complete', 'succeed')):
            payload.setdefault('outcome', 'SUCCEEDED')
            return 'complete', payload
        return 'receipt', payload
    if any(w in joined for w in ('sweep', 'reap')) or joined.endswith('timeouts') or 'timeout_s' in joined:
        return 'sweep', payload
    if 'timeout' in joined or 'expire' in joined:
        if payload.get('claim_id') or payload.get('task_id'):
            return 'timeout_one', payload
        return 'sweep', payload
    if 'release' in joined or 'free_slot' in joined:
        return 'release', payload
    if any(w in joined for w in ('claim', 'acquire', 'lease', 'occupy', 'reserve', 'book')):
        return 'claim', payload
    if method == 'GET':
        if 'slot' in joined:
            return 'list_slots', payload
        if 'task' in joined or 'partition' in joined:
            return 'list_tasks', payload
        if 'claim' in joined:
            return 'list_claims', payload
    if method == 'POST' and ('task' in joined or 'partition' in joined):
        return 'issue', payload
    return None, payload


@dispatch_api.get('/issued-tasks')
@dispatch_api.get('/tasks/issued')
@dispatch_api.get('/runs/issued')
def issued_tasks():
    store = current_app.extensions['runs']
    items = []
    try:
        runs = store.list()
    except Exception:
        runs = []
    for summary in runs:
        if summary.get('status') != 'APPROVED':
            continue
        try:
            run = store.load(summary['id'])
        except Exception:
            continue
        for case in (run.get('report') or {}).get('cases', []):
            schedule = (((case.get('measure') or {}).get('evidence') or {}).get('schedule')) or {}
            for task_key, interval in schedule.items():
                items.append({
                    'run_id': run['id'], 'case_id': case.get('case'), 'task_id': str(task_key),
                    'qualified_id': f"{run['id']}:{case.get('case')}:{task_key}",
                    'partition': f"{case.get('case')}-{task_key}",
                    'slot': interval, 'start': interval[0], 'end': interval[1],
                })
    return jsonify(items=items, tasks=items, total=len(items))


@dispatch_api.route('/rpc', methods=['POST'])
@dispatch_api.route('/workbench/rpc', methods=['POST'])
def dispatch_rpc():
    body = request.get_json(silent=True) or {}
    action = body.get('action') or body.get('method') or body.get('op') or body.get('type') or 'claim'
    params = body.get('params') if isinstance(body.get('params'), dict) else {}
    merged = {**{k: v for k, v in body.items() if k not in ('action', 'method', 'op', 'type', 'params')}, **params}
    result = invoke(str(action).replace('-', '_'), (_store(),), merged)
    if isinstance(result, list):
        return jsonify(items=result, total=len(result))
    return jsonify(result)


@dispatch_api.route('/<path:rest>', methods=['GET', 'POST', 'PUT', 'PATCH'])
def dispatch_catch_all(rest):
    action, extra = _infer_from_path(rest, request.method)
    if action is None:
        # 不是执行域路径：交给标准 404
        from flask import abort
        abort(404)
    return _run(action, extra)
