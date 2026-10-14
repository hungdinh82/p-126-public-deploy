"""Durable voice intake, independent of HTTP connections and LLM execution.

SQLite owns ordering and idempotency. Accepted audio survives restart until
transcription succeeds. Transcripts are committed before a turn can execute.
A turn interrupted during execution is reconciled, never blindly replayed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class InboxFullError(RuntimeError):
    pass


class VoiceInbox:
    def __init__(self, root: Path, *, max_pending=128, max_audio_bytes=256_000_000):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'inputs.sqlite3'
        self.max_pending, self.max_audio_bytes = max_pending, max_audio_bytes
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS inputs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, audio_path TEXT, audio_bytes INTEGER NOT NULL,
                    context TEXT NOT NULL, status TEXT NOT NULL, transcript TEXT,
                    response TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                    generation_turn_id TEXT, response_revision INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(session_id,turn_id));
                CREATE INDEX IF NOT EXISTS inputs_work ON inputs(status,retry_at,sequence);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(inputs)')}
            if 'revision' not in columns:
                db.execute('ALTER TABLE inputs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            if 'generation_turn_id' not in columns:
                db.execute('ALTER TABLE inputs ADD COLUMN generation_turn_id TEXT')
            if 'response_revision' not in columns:
                db.execute('ALTER TABLE inputs ADD COLUMN response_revision INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        value['context'] = json.loads(value['context'])
        value['response'] = json.loads(value['response']) if value['response'] else None
        return value

    def accept(self, session_id, turn_id, context, *, audio=None, suffix='.webm', transcript=None):
        content = audio if audio is not None else transcript.encode()
        digest = hashlib.sha256(content + json.dumps(context, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM inputs WHERE session_id=? AND turn_id=?', (session_id,turn_id)).fetchone()
            if existing:
                if existing['fingerprint'] != digest:
                    raise ValueError('turn_id đã được dùng cho audio hoặc nội dung khác')
                return self.decode(existing)
            count, size = db.execute("SELECT COUNT(*),COALESCE(SUM(audio_bytes),0) FROM inputs WHERE status NOT IN ('completed','superseded','failed','interrupted')").fetchone()
            if count >= self.max_pending or size + len(content) > self.max_audio_bytes:
                raise InboxFullError('Hàng đợi tiếp nhận đã đầy; audio chưa được nhận, hãy gửi lại sau')
            revision = (db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM inputs WHERE session_id=?',
                                   (session_id,)).fetchone()[0] if transcript is not None else 0)
            identity = uuid4().hex
            path = None
            if audio is not None:
                suffix = suffix if suffix in {'.wav','.webm','.ogg','.mp3','.m4a'} else '.bin'
                path = self.root / (identity + suffix)
                with path.open('xb') as handle:
                    handle.write(audio)
                    handle.flush()
                    os.fsync(handle.fileno())
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            now = time.time()
            try:
                db.execute('''INSERT INTO inputs(id,session_id,turn_id,fingerprint,audio_path,audio_bytes,
                    context,status,transcript,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identity,session_id,turn_id,digest,str(path) if path else None,len(audio or b''),
                     json.dumps(context), 'received' if audio is not None else 'recognized', transcript,revision,now,now))
                db.commit()
            except BaseException:
                if path:
                    path.unlink(missing_ok=True)
                raise
            return self.decode(db.execute('SELECT * FROM inputs WHERE id=?',(identity,)).fetchone())

    def get(self, identity, session_id):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM inputs WHERE id=? AND session_id=?',(identity,session_id)).fetchone())

    def list(self, session_id):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM inputs WHERE session_id=? ORDER BY sequence DESC LIMIT 100',(session_id,))]

    def claim(self, stage):
        source, target = ('received','transcribing') if stage == 'stt' else ('recognized','processing')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT i.* FROM inputs i WHERE i.status=? AND i.retry_at<=?
                AND (?='stt' OR NOT EXISTS(SELECT 1 FROM inputs older WHERE older.session_id=i.session_id
                    AND older.sequence<i.sequence AND older.status IN ('received','transcribing','recognized','processing')))
                ORDER BY i.sequence LIMIT 1''',(source,time.time(),stage)).fetchone()
            if row is None:
                return None
            db.execute('UPDATE inputs SET status=?,updated_at=? WHERE id=?',(target,time.time(),row['id']))
            value = self.decode(row)
            value['status'] = target
            return value

    def update(self, identity, *, status, transcript=None, response=None, error=None, retry_at=0, attempts=None):
        with self.connect() as db:
            db.execute('''UPDATE inputs SET status=?,transcript=COALESCE(?,transcript),response=?,error=?,
                retry_at=?,attempts=COALESCE(?,attempts),updated_at=? WHERE id=?''',
                (status,transcript,json.dumps(response,ensure_ascii=False) if response is not None else None,
                 error,retry_at,attempts,time.time(),identity))

    def complete_transcription(self, identity, transcript):
        """Commit a transcript and its session revision as one durable change."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT session_id FROM inputs WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise KeyError(identity)
            revision = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM inputs WHERE session_id=?',
                                  (row['session_id'],)).fetchone()[0]
            db.execute('UPDATE inputs SET status=\'recognized\',transcript=?,revision=?,error=NULL,updated_at=? WHERE id=?',
                       (transcript,revision,time.time(),identity))
            return revision

    def recognized_for_session(self, session_id):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute(
                "SELECT * FROM inputs WHERE session_id=? AND (status='recognized' OR (status='processing' AND generation_turn_id IS NULL)) ORDER BY sequence",
                (session_id,))]

    def claim_conversation(self, session_id, identities, generation_turn_id):
        """Atomically bind recognized utterances to one replaceable snapshot."""
        if not identities:
            return []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            marks = ','.join('?' for _ in identities)
            rows = db.execute(f"SELECT * FROM inputs WHERE session_id=? AND id IN ({marks}) AND (status='recognized' OR (status='processing' AND generation_turn_id IS NULL)) ORDER BY sequence",
                              (session_id,*identities)).fetchall()
            if len(rows) != len(identities):
                return []
            db.execute(f"UPDATE inputs SET status='processing',generation_turn_id=?,updated_at=? WHERE session_id=? AND id IN ({marks}) AND (status='recognized' OR (status='processing' AND generation_turn_id IS NULL))",
                       (generation_turn_id,time.time(),session_id,*identities))
            return [self.decode(db.execute('SELECT * FROM inputs WHERE id=?',(row['id'],)).fetchone()) for row in rows]

    def settle_conversation(self, identities, *, status, response=None, error=None, response_revision=0):
        if not identities:
            return
        with self.connect() as db:
            marks=','.join('?' for _ in identities)
            db.execute(f'UPDATE inputs SET status=?,response=?,error=?,response_revision=?,updated_at=? WHERE id IN ({marks})',
                       (status,json.dumps(response,ensure_ascii=False) if response is not None else None,
                        error,response_revision,time.time(),*identities))

    def requeue_conversation(self, identities):
        if not identities:
            return
        with self.connect() as db:
            marks=','.join('?' for _ in identities)
            db.execute(f"UPDATE inputs SET status='recognized',generation_turn_id=NULL,error=NULL,updated_at=? WHERE id IN ({marks}) AND status='processing'",
                       (time.time(),*identities))

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE inputs SET status='received' WHERE status='transcribing'")
            # Processing may have dispatched a vehicle command. Keep its text
            # and reconcile against the existing execution ledger on startup.
            result = [self.decode(row) for row in db.execute("SELECT * FROM inputs WHERE status='processing'")]
            return result

    def discard_audio(self, job):
        if job['audio_path']:
            Path(job['audio_path']).unlink(missing_ok=True)
            with self.connect() as db:
                db.execute('UPDATE inputs SET audio_path=NULL,audio_bytes=0 WHERE id=?',(job['id'],))

    def clear(self, session_id=None):
        with self.connect() as db:
            rows = db.execute('SELECT audio_path FROM inputs' + (' WHERE session_id=?' if session_id else ''),
                              (session_id,) if session_id else ()).fetchall()
            db.execute('DELETE FROM inputs' + (' WHERE session_id=?' if session_id else ''),
                       (session_id,) if session_id else ())
        for row in rows:
            if row[0]:
                Path(row[0]).unlink(missing_ok=True)
