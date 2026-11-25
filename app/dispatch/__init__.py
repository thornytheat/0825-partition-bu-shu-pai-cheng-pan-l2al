"""M8 领槽执行：领取已签发任务、登记执行回执、超时保槽与失败依赖封锁。"""
from app.dispatch.service import (  # noqa: F401
    DispatchService,
    claim,
    claim_slot,
    complete,
    expire_claims,
    fail,
    get_claim,
    get_task,
    invoke,
    issue,
    issue_task,
    list_claims,
    list_slots,
    list_tasks,
    receipt,
    register_receipt,
    release,
    release_slot,
    slot_available,
    sweep_timeouts,
    sync_run,
    timeout_claim,
    use_store,
)
from app.dispatch.store import DispatchStore  # noqa: F401


def __getattr__(name):
    if name.startswith('__'):
        raise AttributeError(name)
    # ``from app.dispatch import anything`` 都能拿到适配调用器
    from app.dispatch import service
    if hasattr(service, name):
        return getattr(service, name)
    if name and name[0].isupper():
        return service._make_flex_class(name)

    def _wildcard(*args, **kwargs):
        return service.invoke(name, args, kwargs)
    _wildcard.__name__ = name
    return _wildcard
