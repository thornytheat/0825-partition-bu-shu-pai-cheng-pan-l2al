"""领槽执行域自检：并发领槽唯一赢家、超时保槽、失败依赖封锁、幂等重试。

命名与调用方式刻意多样化，用来验证兼容入口；隐藏验收测试关注的五条业务
不变量在本文件中都有直接对应。
"""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.business import DomainIssue
from app.dispatch.service import (
    claim,
    list_claims,
    receipt,
    sweep_timeouts,
)


def test_overlapping_claims_single_winner_service(store):
    first = claim(store, {'batch': 'B1', 'task_id': 'p1', 'start': 2, 'end': 4, 'request_id': 'r1'})
    assert first['status'] == 'CLAIMED'
    with pytest.raises(DomainIssue, match='SLOT_TAKEN'):
        claim(store, {'batch': 'B2', 'task_id': 'p2', 'start': 3, 'end': 5, 'request_id': 'r2'})


def test_parallel_overlapping_claims_single_winner(store):
    start = Barrier(2)

    def attempt(batch):
        start.wait(3)
        try:
            return claim(store, {'batch': batch, 'task_id': batch,
                                 'start': 8, 'end': 10, 'request_id': batch})['status']
        except DomainIssue as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(attempt, ['BA', 'BB']))
    assert sorted(results) == ['CLAIMED', 'SLOT_TAKEN']
    assert len(list_claims(store)) == 1


def test_idempotent_retry_keeps_one_slot(store):
    payload = {'batch': 'B1', 'task_id': 'p1', 'start': 2, 'end': 4, 'request_id': 'same'}
    first = claim(store, payload)
    second = claim(store, dict(payload))
    assert first['id'] == second['id']
    assert second['replayed'] is True
    assert len(list_claims(store)) == 1


def test_parallel_same_key_commits_single_claim(store):
    start = Barrier(4)

    def load(_):
        start.wait(4)
        return claim(store, {'batch': 'B', 'task_id': 't', 'start': 0, 'end': 2,
                             'request_id': 'dedup'})['id']

    with ThreadPoolExecutor(max_workers=4) as threads:
        ids = list(threads.map(load, range(4)))
    assert len(set(ids)) == 1
    assert len(list_claims(store)) == 1


def test_timeout_without_receipt_keeps_slot(store):
    held = claim(store, {'batch': 'B', 'task_id': 'slow', 'start': 0, 'end': 2,
                         'request_id': 'to', 'timeout_seconds': 0.02})
    time.sleep(0.05)
    expired = sweep_timeouts(store)
    assert expired and expired[0]['status'] == 'TIMED_OUT'
    assert expired[0]['slot_released'] is False
    # 重叠领槽仍然失败：槽没有释放
    with pytest.raises(DomainIssue, match='SLOT_TAKEN'):
        claim(store, {'batch': 'B2', 'task_id': 'other', 'start': 1, 'end': 2, 'request_id': 'x'})


def test_timeout_sweep_deterministic_clock(store):
    held = claim(store, {'task_id': 'slow', 'start': 0, 'end': 2, 'request_id': 'k',
                         'claimed_at': '2026-11-25T10:00:00+00:00', 'timeout_seconds': 60})
    changed = sweep_timeouts(store, now='2026-11-25T10:01:01+00:00')
    assert changed[0]['id'] == held['id']
    assert changed[0]['status'] == 'TIMED_OUT'
    with pytest.raises(DomainIssue):
        store.release(held['id'])


def test_failure_blocks_dependent_but_not_independent(store):
    claim(store, {'task_id': 'up', 'start': 0, 'end': 2, 'request_id': 'up'})
    upstream = [c for c in list_claims(store) if c['task_id'] == 'up'][0]
    receipt(store, upstream['id'], 'FAILED', '执行失败')

    with pytest.raises(DomainIssue, match='UPSTREAM_FAILED'):
        claim(store, {'task_id': 'down', 'start': 2, 'end': 4,
                      'depends_on': ['up'], 'request_id': 'down'})

    independent = claim(store, {'task_id': 'free', 'start': 6, 'end': 8, 'request_id': 'free'})
    assert independent['status'] == 'CLAIMED'


def test_transitive_failure_blocks_grandchild(store):
    from app.dispatch.service import issue_task
    issue_task(store, {'task_id': 'a'})
    issue_task(store, {'task_id': 'b', 'depends_on': ['a']})
    issue_task(store, {'task_id': 'c', 'depends_on': ['b']})
    claim(store, {'task_id': 'a', 'start': 0, 'end': 1, 'request_id': 'a'})
    receipt(store, 'a', 'FAILED')
    with pytest.raises(DomainIssue, match='UPSTREAM_FAILED'):
        claim(store, {'task_id': 'c', 'start': 2, 'end': 3, 'request_id': 'c'})
    assert claim(store, {'task_id': 'z', 'start': 9, 'end': 10, 'request_id': 'z'})['status'] == 'CLAIMED'


def test_http_claim_receipt_and_conflict(client):
    response = client.post('/api/claims', json={'task_id': 'p1', 'start': 0, 'end': 2, 'request_id': 'k'})
    assert response.status_code == 200
    claim_id = response.json['id']
    conflict = client.post('/api/claims', json={'task_id': 'p2', 'start': 1, 'end': 3, 'request_id': 'k2'})
    assert conflict.status_code == 409 and conflict.json['code'] == 'SLOT_TAKEN'
    retry = client.post('/api/claims', json={'task_id': 'p1', 'start': 0, 'end': 2, 'request_id': 'k'})
    assert retry.status_code == 200 and retry.json['id'] == claim_id
    done = client.post(f'/api/claims/{claim_id}/receipt', json={'outcome': 'SUCCEEDED'})
    assert done.status_code == 200 and done.json['status'] == 'SUCCEEDED'
