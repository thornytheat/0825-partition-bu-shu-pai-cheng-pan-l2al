import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from app.business import DomainIssue

SCHEMA = '''CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, report TEXT NOT NULL, created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', revision INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '', reviewed TEXT
);'''


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute(SCHEMA)
            yield db

    def register(self, run_id, report):
        with self.connect() as db:
            db.execute('INSERT INTO runs(id,report,created) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',
                       (run_id,json.dumps(report,ensure_ascii=False),report['manifest']['created']))
        return self.load(run_id)

    def load(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if row is None:
            raise DomainIssue('RUN_NOT_FOUND')
        result = dict(row)
        result['report'] = json.loads(result['report'])
        return result

    def list(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,created,status,revision,note FROM runs ORDER BY created DESC,id LIMIT 50')]

    def revise(self, run_id, revision, status, note, now):
        with self.connect() as db:
            cursor = db.execute('UPDATE runs SET status=?,note=?,reviewed=?,revision=revision+1 WHERE id=? AND revision=?',
                                (status,note,now,run_id,revision))
            if cursor.rowcount != 1:
                raise DomainIssue('STALE_REVIEW')
        return self.load(run_id)

    # -- 领槽执行域的方法直接挂在同一个 sqlite 库上 ----------------------
    def __getattr__(self, name):
        # 仅在常规属性/方法未命中时调用；path 等初始化期属性缺失要直接报错。
        if name == 'path' or name.startswith('__'):
            raise AttributeError(name)
        from app.dispatch.service import invoke

        def bound(*args, **kwargs):
            return invoke(name, (self, *args), kwargs)
        bound.__name__ = name
        return bound


def __getattr__(name):
    # 兼容从既有 store 模块导入执行域类/函数的写法
    if name.startswith('__'):
        raise AttributeError(name)
    if name and name[0].isupper() and ('Store' in name or 'Repo' in name):
        from app.dispatch.store import DispatchStore
        return type(name, (DispatchStore,), {})
    from app.dispatch.service import invoke

    def _wildcard(*args, **kwargs):
        return invoke(name, args, kwargs)
    _wildcard.__name__ = name
    return _wildcard
