import json, sqlite3, time, threading
from contextlib import contextmanager
from .config import DB_PATH

STATUSES = ["NOT_STARTED", "IN_PROGRESS", "APPLIED", "ALREADY_APPLIED", "NEEDS_USER_INPUT",
            "BLOCKED", "FAILED", "CLOSED", "INELIGIBLE", "UNCERTAIN"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, url TEXT, url_key TEXT,
  dedupe_key TEXT, source_url TEXT, category TEXT, priority INTEGER, platform TEXT,
  status TEXT DEFAULT 'NOT_STARTED', status_reason TEXT, date_posted INTEGER,
  discovered_at REAL, attempted_at REAL, submitted_at REAL, resume_used TEXT,
  retries INTEGER DEFAULT 0, evidence TEXT, last_error TEXT, next_try_at REAL DEFAULT 0,
  questions_json TEXT DEFAULT '[]');
CREATE INDEX IF NOT EXISTS ix_status ON jobs(status, priority);
CREATE INDEX IF NOT EXISTS ix_urlkey ON jobs(url_key);
CREATE TABLE IF NOT EXISTS human_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, qkey TEXT, question TEXT, kind TEXT,
  options TEXT, answer TEXT, created_at REAL, resolved_at REAL);
CREATE INDEX IF NOT EXISTS ix_hq ON human_queue(qkey, resolved_at);
CREATE TABLE IF NOT EXISTS answers(qkey TEXT PRIMARY KEY, question TEXT, answer TEXT, source TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, job_id TEXT, msg TEXT);
"""
_lock = threading.RLock()


def connect(path=None):
    c = sqlite3.connect(str(path or DB_PATH), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


class DB:
    def __init__(self, path=None):
        self.c = connect(path)

    def q(self, sql, args=()):
        with _lock:
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with _lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur

    def update(self, job_id, **f):
        if not f:
            return
        cols = ", ".join(f"{k}=?" for k in f)
        self.x(f"UPDATE jobs SET {cols} WHERE id=?", (*f.values(), job_id))

    def log(self, job_id, msg):
        self.x("INSERT INTO events(ts,job_id,msg) VALUES(?,?,?)", (time.time(), job_id, msg[:500]))

    def kv_get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # --- answers & human queue ---
    def get_answer(self, qkey):
        r = self.q("SELECT answer FROM answers WHERE qkey=?", (qkey,))
        return r[0]["answer"] if r else None

    def save_answer(self, qkey, question, answer, source="user"):
        self.x("""INSERT INTO answers(qkey,question,answer,source,updated_at) VALUES(?,?,?,?,?)
                  ON CONFLICT(qkey) DO UPDATE SET answer=excluded.answer, source=excluded.source,
                  updated_at=excluded.updated_at""", (qkey, question, answer, source, time.time()))

    def add_need(self, job_id, qkey, question, kind, options):
        if self.q("SELECT 1 FROM human_queue WHERE job_id=? AND qkey=? AND resolved_at IS NULL", (job_id, qkey)):
            return
        self.x("INSERT INTO human_queue(job_id,qkey,question,kind,options,created_at) VALUES(?,?,?,?,?,?)",
               (job_id, qkey, question, kind, json.dumps(options or []), time.time()))

    def resolve_need(self, qkey, answer, save=True):
        now = time.time()
        rows = self.q("SELECT DISTINCT job_id, question FROM human_queue WHERE qkey=? AND resolved_at IS NULL", (qkey,))
        self.x("UPDATE human_queue SET answer=?, resolved_at=? WHERE qkey=? AND resolved_at IS NULL", (answer, now, qkey))
        if save and rows:
            self.save_answer(qkey, rows[0]["question"], answer, "user")
        for r in rows:  # re-queue jobs that were waiting only on this
            left = self.q("SELECT 1 FROM human_queue WHERE job_id=? AND resolved_at IS NULL", (r["job_id"],))
            if not left:
                self.x("UPDATE jobs SET status='NOT_STARTED', retries=0, next_try_at=0 WHERE id=? AND status='NEEDS_USER_INPUT'", (r["job_id"],))

    def counts(self):
        c = {s: 0 for s in STATUSES}
        for r in self.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status"):
            c[r["status"]] = r["n"]
        return c
