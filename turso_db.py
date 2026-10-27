"""Тонкая обёртка над Turso (libSQL по HTTP, протокол Hrana) с интерфейсом, похожим на sqlite3.
Нужны только `requests` и переменные окружения TURSO_DATABASE_URL / TURSO_AUTH_TOKEN.
Если TURSO_DATABASE_URL не задан — работает обычный локальный sqlite-файл (для разработки)."""
import os, base64, sqlite3, requests
from requests.adapters import HTTPAdapter

URL = (os.environ.get('TURSO_DATABASE_URL') or '').strip()
TOKEN = (os.environ.get('TURSO_AUTH_TOKEN') or '').strip()
LOCAL_FILE = os.environ.get('LOCAL_DB', 'extera.db')
USE_TURSO = bool(URL)

if URL.startswith('libsql://'):
    URL = 'https://' + URL[len('libsql://'):]
URL = URL.rstrip('/')


class Error(Exception):
    pass


class IntegrityError(Error):
    pass


if not USE_TURSO:   # локальный режим: те же исключения, что и у sqlite3
    Error, IntegrityError = sqlite3.Error, sqlite3.IntegrityError

_session = requests.Session()
_session.mount('http://', HTTPAdapter(pool_connections=4, pool_maxsize=32))
_session.mount('https://', HTTPAdapter(pool_connections=4, pool_maxsize=32))
_WRITES = {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}


class Row(tuple):
    """Строка результата: row[0], row['name'], dict(row), row.keys()."""
    def __new__(cls, cols, values):
        r = super().__new__(cls, values)
        r._cols = cols
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._cols.index(k))
            except ValueError:
                raise IndexError('No item with that key')
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._cols)


def _enc(v):
    if v is None:
        return {'type': 'null'}
    if isinstance(v, bool):
        return {'type': 'integer', 'value': str(int(v))}
    if isinstance(v, int):
        return {'type': 'integer', 'value': str(v)}
    if isinstance(v, float):
        return {'type': 'float', 'value': v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {'type': 'blob', 'base64': base64.b64encode(bytes(v)).decode().rstrip('=')}
    return {'type': 'text', 'value': str(v)}


def _dec(c):
    t = c.get('type')
    if t == 'null':
        return None
    if t == 'integer':
        return int(c['value'])
    if t == 'float':
        return float(c['value'])
    if t == 'blob':
        b = c.get('base64', '')
        return base64.b64decode(b + '=' * (-len(b) % 4))
    return c.get('value')


class Cursor:
    def __init__(self, conn):
        self.conn, self.rows, self.rowcount, self.lastrowid, self.description, self._i = conn, [], -1, None, None, 0

    def execute(self, sql, params=()):
        self.conn._run(self, sql, params)
        return self

    def fetchone(self):
        if self._i < len(self.rows):
            self._i += 1
            return self.rows[self._i - 1]
        return None

    def fetchall(self):
        out = self.rows[self._i:]
        self._i = len(self.rows)
        return out

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        pass


class Connection:
    def __init__(self):
        self.baton, self.base, self.in_tx = None, URL, False
        self.row_factory = None

    def _post(self, reqs):
        body = {'baton': self.baton, 'requests': reqs}
        headers = {'Authorization': 'Bearer ' + TOKEN} if TOKEN else {}
        last = None
        for attempt in range(2 if self.baton is None else 1):   # ретрай только вне транзакции
            try:
                r = _session.post(self.base + '/v2/pipeline', json=body, headers=headers, timeout=25)
                break
            except requests.RequestException as e:
                last = e
        else:
            raise Error('Turso недоступен: %s' % last)
        if r.status_code != 200:
            raise Error('Turso HTTP %s: %s' % (r.status_code, r.text[:300]))
        data = r.json()
        self.baton = data.get('baton')
        if data.get('base_url'):
            self.base = data['base_url'].rstrip('/')
        return data['results']

    @staticmethod
    def _check(res):
        if res.get('type') == 'error':
            e = res.get('error') or {}
            msg, code = e.get('message', 'error'), str(e.get('code', ''))
            if 'CONSTRAINT' in code.upper() or 'constraint failed' in msg.lower() or 'UNIQUE' in msg:
                raise IntegrityError(msg)
            raise Error(msg)
        return res['response']

    def _run(self, cur, sql, params):
        first = (sql.lstrip().split(None, 1) or [''])[0].upper()
        stmt = {'sql': sql, 'args': [_enc(p) for p in (params or ())], 'want_rows': True}
        reqs = []
        begin = first in _WRITES and not self.in_tx
        if begin:
            reqs.append({'type': 'execute', 'stmt': {'sql': 'BEGIN', 'args': []}})
        reqs.append({'type': 'execute', 'stmt': stmt})
        if not (self.in_tx or begin):
            reqs.append({'type': 'close'})   # вне транзакции поток не держим: один запрос на один SQL
        res = self._post(reqs)
        if begin:
            self._check(res[0])
            self.in_tx = True
            res = res[1:]
        try:
            out = self._check(res[0])['result']
        except Error:
            if begin and self.in_tx:
                self.in_tx = False
            raise
        cols = [c.get('name') for c in out.get('cols', [])]
        cur.description = [(c,) + (None,) * 6 for c in cols]
        cur.rows = [Row(cols, [_dec(c) for c in row]) for row in out.get('rows', [])]
        cur._i = 0
        cur.rowcount = out.get('affected_row_count', -1)
        lid = out.get('last_insert_rowid')
        cur.lastrowid = int(lid) if lid is not None else None

    def cursor(self):
        return Cursor(self)

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def commit(self):
        if self.in_tx:
            res = self._post([{'type': 'execute', 'stmt': {'sql': 'COMMIT', 'args': []}}, {'type': 'close'}])
            self.in_tx, self.baton = False, None
            self._check(res[0])

    def rollback(self):
        if self.in_tx or self.baton:
            try:
                self._post([{'type': 'execute', 'stmt': {'sql': 'ROLLBACK', 'args': []}}, {'type': 'close'}])
            except Error:
                pass
        self.in_tx, self.baton = False, None

    def close(self):
        if self.baton:   # незавершённая транзакция откатывается, как в sqlite3
            self.rollback()


def connect():
    if USE_TURSO:
        return Connection()
    conn = sqlite3.connect(LOCAL_FILE)
    conn.row_factory = sqlite3.Row
    return conn
