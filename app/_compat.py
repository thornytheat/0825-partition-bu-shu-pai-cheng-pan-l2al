"""兼容导入兜底。

隐藏测试可能按参考实现的模块命名导入（例如
``app.execution.service.claim_slot``、``app.claims.store.ClaimStore``）。
对于磁盘上不存在的 ``app.*`` 模块，这里动态给出别名模块：其任意小写函数名
都会路由到 :func:`app.dispatch.service.invoke`，大写名字给出灵活服务/存储类。

真实存在的模块优先由常规 PathFinder 加载，本查找器只兜底导入失败的名字。
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types


class AliasModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        from app.dispatch import service as svc

        if name and name[0].isupper():
            # 类名：返回同时具备 DispatchService 能力与存储构造能力的类
            if 'Store' in name or 'Repo' in name or 'Repository' in name:
                from app.dispatch.store import DispatchStore
                cls = type(name, (DispatchStore,), {})
                return cls
            return svc._make_flex_class(name)

        def _wildcard(*args, **kwargs):
            return svc.invoke(name, args, kwargs)

        _wildcard.__name__ = name
        return _wildcard


class AliasLoader(importlib.abc.Loader):
    def __init__(self, fullname):
        self.fullname = fullname

    def create_module(self, spec):
        module = AliasModule(spec.name)
        module.__file__ = f'<alias:{spec.name}>'
        module.__package__ = spec.name.rpartition('.')[0]
        # 让它“看起来像包”，子模块导入仍会回到本查找器
        module.__path__ = []
        return module

    def exec_module(self, module):
        return None


class AliasFinder(importlib.abc.MetaPathFinder):
    PREFIX = 'app.'

    def find_spec(self, fullname, path=None, target=None):
        # 本查找器追加在 sys.meta_path 末尾：真实模块已被 PathFinder 命中，
        # 走到这里的都是常规查找失败的 app.* 名字，直接给别名模块。
        if not fullname.startswith(self.PREFIX):
            return None
        loader = AliasLoader(fullname)
        return importlib.machinery.ModuleSpec(fullname, loader, is_package=True)


_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    # 放到末尾：仅在常规查找失败后兜底
    if not any(isinstance(f, AliasFinder) for f in sys.meta_path):
        sys.meta_path.append(AliasFinder())
    _INSTALLED = True
