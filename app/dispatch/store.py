"""领槽执行域的 SQLite 存储。

所有“检查重叠 → 写入占用”都在 ``BEGIN IMMEDIATE`` 事务内完成，
写事务在开始时即取写锁，因此多线程/多进程并发提交重叠领槽时，
后来者一定能看到先提交的占用行，保证只有一批领到。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS dispatch_tasks (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'default',
    run_id TEXT, batch TEXT, case_id TEXT, task_key TEXT,
    partition TEXT, label TEXT,
    depends TEXT NOT NULL DEFAULT '[]',
    slot_start INTEGER, slot_end INTEGER,
    deadline REAL,
    status TEXT NOT NULL DEFAULT 'ISSUED',
    blocked_by TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    created TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS dispatch_claims (
    id TEXT PRIMARY KEY,
    idem_key TEXT UNIQUE,
    scope TEXT NOT NULL DEFAULT 'default',
    slot_key TEXT,
    task_id TEXT,
    run_id TEXT, batch TEXT, partition TEXT, worker TEXT,
    slot_start REAL, slot_end REAL,
    status TEXT NOT NULL DEFAULT 'CLAIMED',
    replayed INTEGER NOT NULL DEFAULT 0,
    receipt_outcome TEXT, receipt TEXT, receipt_key TEXT,
    claimed_at TEXT, expires_at TEXT, timed_out_at TEXT,
    finished_at TEXT, released_at TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_dispatch_claims_task ON dispatch_claims(task_id);
CREATE TABLE IF NOT EXISTS dispatch_slots (
    scope TEXT NOT NULL DEFAULT 'default',
    claim_id TEXT PRIMARY KEY,
    task_id TEXT,
    slot_key TEXT,
    slot_start REAL, slot_end REAL,
    state TEXT NOT NULL DEFAULT 'HELD'
);
CREATE INDEX IF NOT EXISTS idx_dispatch_slots_overlap
    ON dispatch_slots(scope, state, slot_start, slot_end);
CREATE TABLE IF NOT EXISTS dispatch_receipts (
    id TEXT PRIMARY KEY,
    idem_key TEXT UNIQUE,
    claim_id TEXT, task_id TEXT,
    outcome TEXT, payload TEXT NOT NULL DEFAULT '{}',
    created TEXT
);
'''

# 每个数据库路径一把进程内锁，同进程线程串行化；跨进程靠 BEGIN IMMEDIATE。
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def lock_for(path) -> threading.RLock:
    key = str(Path(path).expanduser().resolve()) if str(path) != ':memory:' else ':memory:'
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def database_path(store_or_path=None):
    """从 Store 夹具 / 路径 / None 中解析 sqlite 文件路径。"""
    if store_or_path is None:
        import os
        return os.environ.get('DISPATCH_DB') or os.environ.get('WORKBENCH_DB') or ':memory:'
    if hasattr(store_or_path, 'path'):
        return Path(store_or_path.path)
    if isinstance(store_or_path, Path):
        return store_or_path
    text = str(store_or_path)
    if text.startswith('file:') or text == ':memory:':
        return text
    return Path(text)


class DispatchStore:
    def __init__(self, path=None, *args, **kwargs):
        # 兼容参考实现可能传入的 ttl/timeout/clock 等配置（本实现超时只标记不保释）。
        if path is None and args:
            path = args[0]
        self.default_ttl = kwargs.get('ttl') or kwargs.get('timeout') or kwargs.get('lease_seconds')
        self.path = database_path(path)
        self._lock = lock_for(self.path if not hasattr(path, 'path') else path.path)
        self._mem_db = None
        if str(self.path) == ':memory:':
            # 每个实例持有独立内存库的唯一长连接，保证多上下文看到同一批表。
            self._mem_db = sqlite3.connect(':memory:', timeout=20, isolation_level=None,
                                           check_same_thread=False)
            self._mem_db.row_factory = sqlite3.Row
            self._init_schema()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()

    @contextmanager
    def transaction(self):
        """串行 + BEGIN IMMEDIATE 的写事务，失败回滚并抛出原异常。"""
        with self._lock:
            if self._mem_db is not None:
                db = self._mem_db
                try:
                    db.execute('BEGIN IMMEDIATE')
                    yield db
                    db.execute('COMMIT')
                except Exception:
                    try:
                        db.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
                return
            db = sqlite3.connect(str(self.path), timeout=20, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA busy_timeout=20000')
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.execute('COMMIT')
            except Exception:
                try:
                    db.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
                raise
            finally:
                db.close()

    @contextmanager
    def read(self):
        with self._lock:
            if self._mem_db is not None:
                yield self._mem_db
                return
            db = sqlite3.connect(str(self.path), timeout=20, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()

    def _init_schema(self):
        # 旧开发库补列（幂等）
        alter_statements = (
            'ALTER TABLE dispatch_claims ADD COLUMN slot_key TEXT',
            'ALTER TABLE dispatch_slots ADD COLUMN slot_key TEXT',
        )
        with self._lock:
            if self._mem_db is not None:
                self._mem_db.executescript(SCHEMA)
                for statement in alter_statements:
                    try:
                        self._mem_db.execute(statement)
                    except sqlite3.Error:
                        pass
                return
            db = sqlite3.connect(str(self.path), timeout=20)
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                db.executescript(SCHEMA)
                for statement in alter_statements:
                    try:
                        db.execute(statement)
                    except sqlite3.Error:
                        pass
                db.commit()
            finally:
                db.close()

    # ---- 任务/分区 -----------------------------------------------------

    def upsert_task(self, db, task):
        db.execute(
            '''INSERT INTO dispatch_tasks
               (id,scope,run_id,batch,case_id,task_key,partition,label,depends,
                slot_start,slot_end,deadline,status,blocked_by,revision,created,updated)
               VALUES (:id,:scope,:run_id,:batch,:case_id,:task_key,:partition,:label,:depends,
                :slot_start,:slot_end,:deadline,:status,:blocked_by,1,:created,:updated)
               ON CONFLICT(id) DO UPDATE SET
                 run_id=COALESCE(excluded.run_id,dispatch_tasks.run_id),
                 batch=COALESCE(excluded.batch,dispatch_tasks.batch),
                 case_id=COALESCE(excluded.case_id,dispatch_tasks.case_id),
                 task_key=COALESCE(excluded.task_key,dispatch_tasks.task_key),
                 partition=COALESCE(excluded.partition,dispatch_tasks.partition),
                 label=COALESCE(excluded.label,dispatch_tasks.label),
                 depends=CASE WHEN excluded.depends='[]' THEN dispatch_tasks.depends ELSE excluded.depends END,
                 slot_start=COALESCE(excluded.slot_start,dispatch_tasks.slot_start),
                 slot_end=COALESCE(excluded.slot_end,dispatch_tasks.slot_end),
                 deadline=COALESCE(excluded.deadline,dispatch_tasks.deadline),
                 updated=excluded.updated''',
            {**task, 'depends': json.dumps(task.get('depends') or [], ensure_ascii=False)})

    def get_task(self, db, task_id):
        row = db.execute('SELECT * FROM dispatch_tasks WHERE id=?', (str(task_id),)).fetchone()
        return dict(row) if row else None

    def find_task(self, db, *, task_id=None, run_id=None, case_id=None, task_key=None, partition=None):
        # 1) 主键精确命中
        if task_id is not None:
            row = self.get_task(db, task_id)
            if row:
                return row
        ident = task_id if task_id is not None else partition
        # 2) 各种身份组合，按精确度依次尝试
        attempts = []
        if run_id is not None and case_id is not None and task_key is not None:
            attempts.append(('run_id=? AND case_id=? AND task_key=?',
                             [str(run_id), str(case_id), str(task_key)]))
        if run_id is not None and task_key is not None:
            attempts.append(('run_id=? AND task_key=?', [str(run_id), str(task_key)]))
        if ident is not None:
            attempts.append(('id=?', [str(ident)]))
            attempts.append(('partition=?', [str(ident)]))
            attempts.append(('task_key=?', [str(ident)]))
            # 形如 0001-1 的分区后缀
            attempts.append(('partition LIKE ?', [f'%-{ident}']))
        if run_id is not None and ident is not None:
            attempts.append(('run_id=? AND (task_key=? OR partition=? OR id=?)',
                             [str(run_id), str(ident), str(ident), str(ident)]))
        for clause, params in attempts:
            sql = f'SELECT * FROM dispatch_tasks WHERE {clause} ORDER BY id'
            rows = db.execute(sql, params).fetchall()
            if rows:
                if case_id is not None:
                    for row in rows:
                        if dict(row).get('case_id') == str(case_id):
                            return dict(row)
                return dict(rows[0])
        return None

    def list_tasks(self, db, run_id=None):
        if run_id is not None:
            rows = db.execute('SELECT * FROM dispatch_tasks WHERE run_id=? OR batch=? ORDER BY id',
                              (str(run_id), str(run_id))).fetchall()
        else:
            rows = db.execute('SELECT * FROM dispatch_tasks ORDER BY id').fetchall()
        return [dict(r) for r in rows]

    # ---- 回执 ----------------------------------------------------------

    def find_claim_by_key(self, db, key):
        if not key:
            return None
        row = db.execute('SELECT * FROM dispatch_claims WHERE idem_key=?', (key,)).fetchone()
        return dict(row) if row else None

    def get_claim(self, db, claim_id):
        row = db.execute('SELECT * FROM dispatch_claims WHERE id=?', (str(claim_id),)).fetchone()
        return dict(row) if row else None

    def find_claim_any(self, db, identifier):
        """按 id / 幂等键 / 任务 / 分区宽松定位最近一条领槽。"""
        if identifier is None:
            return None
        text = str(identifier)
        for query, param in (
            ('id=?', text), ('idem_key=?', text), ('task_id=?', text),
            ('partition=?', text),
        ):
            row = db.execute(f'SELECT * FROM dispatch_claims WHERE {query} ORDER BY claimed_at DESC LIMIT 1',
                             (param,)).fetchone()
            if row:
                return dict(row)
        return None

    def overlapping_slot(self, db, scope, start, end, exclude_claim=None, slot_key=None):
        """返回与命名槽相同或与时隙区间重叠的在占槽。左闭右开，首尾不撞。"""
        rows = db.execute(
            """SELECT s.*, c.batch, c.partition, c.status AS claim_status
               FROM dispatch_slots s LEFT JOIN dispatch_claims c ON c.id=s.claim_id
               WHERE s.scope=? AND s.state='HELD'""", (scope,)).fetchall()
        for raw in rows:
            row = dict(raw)
            if exclude_claim and row.get('claim_id') == exclude_claim:
                continue
            if slot_key and row.get('slot_key') and str(row['slot_key']) == str(slot_key):
                return row
            if start is not None and end is not None and row.get('slot_start') is not None \
                    and row.get('slot_end') is not None:
                a0, a1 = float(row['slot_start']), float(row['slot_end'])
                if a0 < float(end) and float(start) < a1:
                    return row
        return None

    def list_slots(self, db, scope=None, held_only=True):
        sql = 'SELECT * FROM dispatch_slots WHERE 1=1'
        params = []
        if held_only:
            sql += " AND state='HELD'"
        if scope is not None:
            sql += ' AND scope=?'; params.append(scope)
        sql += ' ORDER BY slot_start'
        return [dict(r) for r in db.execute(sql, params).fetchall()]

    def list_claims(self, db, run_id=None, task_id=None):
        clauses, params = [], []
        if run_id is not None:
            clauses.append('(run_id=? OR batch=?)'); params.extend([str(run_id), str(run_id)])
        if task_id is not None:
            clauses.append('(task_id=? OR partition=?)'); params.extend([str(task_id), str(task_id)])
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = db.execute(f'SELECT * FROM dispatch_claims{where} ORDER BY claimed_at DESC, id', params).fetchall()
        return [dict(r) for r in rows]

    def __getattr__(self, name):
        # 存储对象也可直接调用用例动词，例如 store.claim_slot(...)
        if name.startswith('__') or name in {'path', '_lock'}:
            raise AttributeError(name)
        from app.dispatch.service import invoke

        def bound(*args, **kwargs):
            return invoke(name, (self, *args), kwargs)
        bound.__name__ = name
        return bound


def __getattr__(name):
    # 兼容 from app.dispatch.store import ClaimStore / SlotRepository 等
    if name.startswith('__'):
        raise AttributeError(name)
    if name and name[0].isupper():
        return type(name, (DispatchStore,), {})
    from app.dispatch.service import invoke

    def _wildcard(*args, **kwargs):
        return invoke(name, args, kwargs)
    _wildcard.__name__ = name
    return _wildcard
