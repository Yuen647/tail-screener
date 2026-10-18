"""Bounded, leased recovery tasks and immutable evidence blobs (stdlib only)."""
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


class RecoveryQueue:
    def __init__(self, directory, *, max_bytes=2_000_000_000):
        if type(max_bytes) is not int or max_bytes <= 0: raise ValueError('存储限额无效')
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root/'blobs'; self.blobs.mkdir(exist_ok=True)
        self.max_bytes = max_bytes
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, spec TEXT NOT NULL, priority INTEGER NOT NULL,
                  state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  next_retry REAL NOT NULL DEFAULT 0, token TEXT, lease_until REAL,
                  error TEXT, latest_sha256 TEXT);
                CREATE TABLE IF NOT EXISTS blobs (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                  task_id TEXT NOT NULL, sha256 TEXT NOT NULL, source TEXT NOT NULL,
                  recorded_at REAL NOT NULL, PRIMARY KEY(task_id,sha256));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root/'tasks.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    @staticmethod
    def spec(code, start, end, kind='daily_window', variant=None):
        if not isinstance(code, str) or not re.fullmatch(r'(SH|SZ)\d{6}', code):
            raise ValueError('任务证券身份无效')
        if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or start > end:
            raise ValueError('任务日期范围无效')
        if kind not in ('daily_window', 'historical_daily', 'corporate_actions'):
            raise ValueError('任务类型不支持')
        result={'code':code, 'start':start, 'end':end, 'kind':kind}
        if variant is not None:
            if not isinstance(variant,str) or not re.fullmatch('[a-z0-9_]{1,80}',variant):raise ValueError('任务数据口径无效')
            result['variant']=variant
        return result

    def enqueue(self, code, start, end, *, kind='daily_window', priority=10, variant=None):
        spec = self.spec(code, start, end, kind, variant)
        if type(priority) is not int: raise ValueError('优先级必须是整数')
        task_id = hashlib.sha256(canonical(spec)).hexdigest()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO tasks(id,spec,priority,state) VALUES(?,?,?,?)',
                       (task_id, canonical(spec).decode(), priority, 'pending'))
            db.execute('UPDATE tasks SET priority=min(priority,?) WHERE id=?', (priority, task_id))
        return task_id

    def _blob(self, db, payload):
        if not isinstance(payload, bytes): raise ValueError('证据必须是原始字节')
        digest = hashlib.sha256(payload).hexdigest()
        exists = db.execute('SELECT size FROM blobs WHERE sha256=?', (digest,)).fetchone()
        used = db.execute('SELECT coalesce(sum(size),0) FROM blobs').fetchone()[0]
        if not exists and used+len(payload) > self.max_bytes: raise ValueError('恢复证据存储限额已达，未删除旧证据')
        target = self.blobs/(digest+'.bin')
        temporary = self.blobs/(uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('xb') as handle:
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            try: os.link(temporary, target)
            except FileExistsError:
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError('已保存证据哈希损坏')
        finally: temporary.unlink(missing_ok=True)
        db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?)', (digest, len(payload)))
        return digest

    def claim(self, *, now=None, lease_seconds=120, max_attempts=3, kind=None, task_ids=None):
        now = time.time() if now is None else now
        if not math.isfinite(now) or not 1 <= lease_seconds <= 1800 or type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError('租约或重试预算无效')
        if kind not in (None,'daily_window','historical_daily','corporate_actions'):raise ValueError('任务类型无效')
        allowed=set(task_ids) if task_ids is not None else None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM tasks WHERE (state='pending' AND next_retry<=?) OR (state='leased' AND lease_until<=?) ORDER BY priority,id", (now, now)).fetchall()
            row=None
            for candidate in rows:
                if allowed is not None and candidate['id'] not in allowed:continue
                if kind is not None and json.loads(candidate['spec'])['kind']!=kind:continue
                if candidate['attempts']>=max_attempts:
                    db.execute("UPDATE tasks SET state='exhausted',token=NULL WHERE id=?",(candidate['id'],))
                elif row is None:row=candidate
            if row is None: return None
            token = uuid.uuid4().hex
            db.execute("UPDATE tasks SET state='leased',attempts=attempts+1,token=?,lease_until=? WHERE id=?", (token, now+lease_seconds, row['id']))
            return {'id':row['id'], 'spec':json.loads(row['spec']), 'token':token,
                    'attempt':row['attempts']+1, 'recovered_interrupted_lease':row['state']=='leased'}

    def requeue(self, task_ids, *, include_completed=False):
        """Explicit operator retry; never replace evidence or seize an active lease."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for task_id in task_ids:
                row=db.execute('SELECT state FROM tasks WHERE id=?',(task_id,)).fetchone()
                if row is None:raise ValueError('恢复任务不存在')
                if row['state']=='leased':raise ValueError('任务仍有租约，拒绝重置')
                if row['state']=='succeeded' and not include_completed:continue
                db.execute("UPDATE tasks SET state='pending',attempts=0,next_retry=0,token=NULL,lease_until=NULL,error=NULL WHERE id=?",(task_id,))

    def finish(self, task, *, state, payload=None, source='', error=None, now=None, retry_after=60):
        now = time.time() if now is None else now
        if state not in ('succeeded', 'missing', 'unsupported', 'conflict', 'retry'):
            raise ValueError('任务结果状态无效')
        if not math.isfinite(now) or not math.isfinite(retry_after) or retry_after < 0:
            raise ValueError('重试时刻无效')
        if state=='succeeded' and (payload is None or not source): raise ValueError('成功任务须保留证据和来源')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone()
            if row is None or row['state']!='leased' or row['token']!=task['token'] or row['lease_until']<=now:
                raise ValueError('任务租约已失效，拒绝迟到写入')
            digest = self._blob(db, payload) if payload is not None else row['latest_sha256']
            if payload is not None:
                db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)', (task['id'],digest,source,now))
            db.execute('UPDATE tasks SET state=?,next_retry=?,token=NULL,lease_until=NULL,error=?,latest_sha256=? WHERE id=?',
                       ('pending' if state=='retry' else state, now+retry_after if state=='retry' else 0,
                        str(error)[:2000] if error else None,digest,task['id']))

    def ingest_recent(self, metadata, evidence_directory):
        """Index a completed acquisition, not a proof of historical/PIT coverage."""
        target = metadata['target_session'] if 'target_session' in metadata else metadata['data_session']
        entries = []
        for code, expected in metadata.get('files', {}).items():
            payload = (Path(evidence_directory)/(code+'.json')).read_bytes()
            if hashlib.sha256(payload).hexdigest()!=expected: raise ValueError('近期证据哈希不符：'+code)
            task_id = self.enqueue(code, target, target)
            entries.append((task_id,payload))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for task_id,payload in entries:
                row = db.execute('SELECT state FROM tasks WHERE id=?',(task_id,)).fetchone()
                if row['state']=='leased': raise ValueError('该任务仍有活动租约，不覆盖')
                digest = self._blob(db, payload)
                db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)',(task_id,digest,'saved_recent_input',time.time()))
                db.execute("UPDATE tasks SET state='succeeded',latest_sha256=?,error=NULL WHERE id=?",(digest,task_id))
        for code,reason in metadata.get('required_failed',metadata.get('failed',{})).items():
            task_id=self.enqueue(code,target,target)
            with self.connect() as db:
                db.execute("UPDATE tasks SET state=CASE WHEN state='succeeded' THEN 'conflict' ELSE state END,error=? WHERE id=?",
                           (json.dumps(reason,ensure_ascii=False)[:2000],task_id))
        return self.summary()

    def summary(self):
        with self.connect() as db:
            states = {r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM tasks GROUP BY state')}
            return {'states':states, 'tasks':sum(states.values()),
                    'revisions':db.execute('SELECT count(*) FROM revisions').fetchone()[0],
                    'bytes':db.execute('SELECT coalesce(sum(size),0) FROM blobs').fetchone()[0],
                    'max_bytes':self.max_bytes, 'directory':str(self.root.resolve()),
                    'historical_completeness_proven':False}

    def tasks(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY priority,id')]

    def run_pending(self, fetch, *, budget_seconds=60, max_tasks=20, kind=None, task_ids=None):
        """fetch(spec) returns finish kwargs; network adapters must enforce their own deadline."""
        if not 0 < budget_seconds <= 1800 or type(max_tasks) is not int or not 1 <= max_tasks <= 1000:
            raise ValueError('任务运行预算无效')
        deadline=time.monotonic()+budget_seconds;done=0
        while done<max_tasks and time.monotonic()<deadline:
            task=self.claim(lease_seconds=min(1800, max(1, int(deadline-time.monotonic())+1)),kind=kind,task_ids=task_ids)
            if task is None: break
            try: result=fetch(task['spec'], deadline)
            except Exception as exc: result={'state':'retry','error':type(exc).__name__}
            self.finish(task, **result); done+=1
        return {'processed':done, **self.summary()}
