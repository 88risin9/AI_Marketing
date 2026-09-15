"""Small SQLite store. JSON payloads support new product parameters without migrations."""
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

KINDS = ('products','customers','inquiries','quotes','activities')
def now():
    return datetime.now().astimezone().isoformat(timespec='microseconds')

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS records(kind TEXT NOT NULL,id INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,data TEXT NOT NULL);''')
        self.path.chmod(0o600)
    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        try:
            c.execute('PRAGMA busy_timeout=20000')
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    def all(self,kind):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT data FROM records WHERE kind=? ORDER BY id DESC',(kind,))]
    def get(self,kind,id):
        with self.connect() as c:
            row=c.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,id)).fetchone()
        if not row: raise ValueError('记录不存在，请刷新页面。')
        return json.loads(row[0])
    def save(self,kind,data,id=None):
        with self.connect() as c:
            return self.save_in(c,kind,data,id)
    def save_in(self,c,kind,data,id=None):
        obj=dict(data)
        if id is None:
            id=c.execute('SELECT COALESCE(MAX(id),0)+1 FROM records WHERE kind=?',(kind,)).fetchone()[0]
            obj.setdefault('created_at',now())
        obj['id']=id
        obj['updated_at']=now()
        c.execute('INSERT OR REPLACE INTO records(kind,id,data) VALUES(?,?,?)',(kind,id,json.dumps(obj,ensure_ascii=False,allow_nan=False)))
        return obj
    def setting(self,key,default=None):
        with self.connect() as c:
            row=c.execute('SELECT data FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_setting(self,key,data):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO settings(key,data) VALUES(?,?)',(key,json.dumps(data,ensure_ascii=False)))
        return data
    def export(self,workspace):
        with self.connect() as c:
            entities={kind:[] for kind in KINDS}
            for kind,data in c.execute('SELECT kind,data FROM records ORDER BY id'):
                entities[kind].append(json.loads(data))
            settings={k:json.loads(d) for k,d in c.execute('SELECT key,data FROM settings')}
        return {'format':'trade-workbench','schema_version':1,'workspace':workspace,'exported_at':now(),'records':entities,'settings':settings}
    def restore(self,bundle,workspace,backup_dir):
        if not isinstance(bundle,dict) or bundle.get('format')!='trade-workbench' or bundle.get('schema_version')!=1:
            raise ValueError('不是此应用支持的备份文件。')
        if bundle.get('workspace')!=workspace: raise ValueError('备份所属资料空间不一致，请先切换正式/示例空间。')
        records=bundle.get('records')
        if not isinstance(records,dict) or set(records)!=set(KINDS): raise ValueError('备份数据表不完整。')
        for kind,rows in records.items():
            if not isinstance(rows,list): raise ValueError('备份数据格式不正确。')
            ids=[]
            for row in rows:
                if not isinstance(row,dict) or not isinstance(row.get('id'),int) or isinstance(row.get('id'),bool) or row['id']<1:
                    raise ValueError('备份记录编号不正确。')
                ids.append(row['id'])
            if len(ids)!=len(set(ids)): raise ValueError('备份中有重复编号。')
        settings=bundle.get('settings')
        if not isinstance(settings,dict) or set(settings)-{'company','demo_seeded'}: raise ValueError('备份设置不正确。')
        customers={r['id'] for r in records['customers']}; inquiries={r['id'] for r in records['inquiries']}
        if any(r.get('customer_id') not in customers for r in records['inquiries']): raise ValueError('备份的客户关联无效。')
        if any(r.get('inquiry_id') not in inquiries for r in records['quotes']): raise ValueError('备份的报价关联无效。')
        # A pre-restore copy and replacement are made while the same DB write lock is held.
        backup_dir=Path(backup_dir);backup_dir.mkdir(parents=True,exist_ok=True)
        path=backup_dir/f'{workspace}-before-restore-{datetime.now().strftime("%Y%m%d-%H%M%S-%f")}.json'
        with self.connect() as c:
            old={kind:[] for kind in KINDS}
            for kind,data in c.execute('SELECT kind,data FROM records'):old[kind].append(json.loads(data))
            previous={'format':'trade-workbench','schema_version':1,'workspace':workspace,'exported_at':now(),'records':old,'settings':{k:json.loads(d) for k,d in c.execute('SELECT key,data FROM settings')}}
            path.write_text(json.dumps(previous,ensure_ascii=False,indent=2),encoding='utf-8');path.chmod(0o600)
            c.execute('DELETE FROM records');c.execute('DELETE FROM settings')
            for kind,rows in records.items():
                for row in rows:c.execute('INSERT INTO records VALUES(?,?,?)',(kind,row['id'],json.dumps(row,ensure_ascii=False,allow_nan=False)))
            for key,data in settings.items():c.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(data,ensure_ascii=False,allow_nan=False)))
        return path.name
