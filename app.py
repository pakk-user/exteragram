import uuid
import turso_db, time, os, base64, io, uuid, mimetypes, json, shutil
from flask_sock import Sock
import threading
from functools import wraps
from flask import g
from flask import Flask, render_template, request, jsonify, send_from_directory, Response, send_file
from werkzeug.security import generate_password_hash, check_password_hash

MAX_ACCOUNTS = 3
app = Flask(__name__)
app.config['SECRET_KEY'] = 'extera-pro-2026'
sock = Sock(app)
app.config['TEMPLATES_AUTO_RELOAD'] = True   # правки index.html видны без перезапуска сервера
DB_NAME, ADMIN_BOT_ID = "extera.db", 1

# файлы лежат на диске (папка uploads), в базе — только метаданные
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_FILE, MAX_VIDEO = 200 * 1024 * 1024, 100 * 1024 * 1024    # файлы до 200 МБ, видео до 100 МБ
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE + 5 * 1024 * 1024

@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('.', 'manifest.json')

# ───────────────────────── helpers ─────────────────────────

def get_db():
    return turso_db.connect()   # Turso (TURSO_DATABASE_URL / TURSO_AUTH_TOKEN) или локальный файл, если переменные не заданы


def body():
    """Безопасно достаёт JSON из запроса (request.json падает на пустом теле)."""
    return request.get_json(silent=True) or {}


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_members(members_str):
    """'1,5,7' -> [1, 5, 7]"""
    return [int(x) for x in (members_str or '').split(',') if x.strip().isdigit()]


def gav_url(gid, v):
    return f"/api/group_avatar/{gid}?v={v}" if v else None


def av_url(uid, v):
    return f"/api/avatar/{uid}?v={v}" if v else None


def err(message, code=400):
    return jsonify({"status": "error", "message": message}), code


_seen_cache = {}


def touch(uid):
    """Отмечает «был в сети» (не чаще раза в 5 секунд на человека)."""
    if not uid:
        return
    now = time.time()
    if now - _seen_cache.get(uid, 0) < 5:
        return
    _seen_cache[uid] = now
    try:
        c = get_db()
        c.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, uid))
        c.commit()
        c.close()
    except turso_db.Error:
        pass


def chat_key(uid, rid):
    """Ключ чата для настроек: группа — 'g_N', личка — 'меньший_больший'."""
    rid = str(rid)
    if rid.startswith('g_'):
        return rid
    a, b = sorted((int(uid), int(rid)))
    return f"{a}_{b}"


def hidden_filter(cur, uid, peer):
    """SQL-кусок (для таблицы messages m): прячет то, что человек очистил или удалил у себя."""
    memo = getattr(g, '_clr', None)
    if memo is None or memo[0] != uid:
        memo = g._clr = (uid, {r['peer']: r['upto'] for r in cur.execute("SELECT peer, upto FROM chat_clear WHERE user_id = ?", (uid,)).fetchall()})
    return " AND m.id > ? AND m.id NOT IN (SELECT msg_id FROM hidden_msgs WHERE user_id = ?)", [memo[1].get(str(peer), 0), uid]


def cleanup_orphans(cur):
    cur.execute("DELETE FROM voices WHERE msg_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM reactions WHERE msg_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM polls WHERE msg_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM poll_votes WHERE msg_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM hidden_msgs WHERE msg_id NOT IN (SELECT id FROM messages)")
    purge_files(cur)


_purge_t = [0]


def purge_expired(cur):
    now = time.time()
    if now - _purge_t[0] < 5:
        return
    _purge_t[0] = now
    if not cur.execute("SELECT 1 FROM messages WHERE expire_at IS NOT NULL AND expire_at < ? LIMIT 1", (now,)).fetchone():
        return
    if cur.execute("DELETE FROM messages WHERE expire_at IS NOT NULL AND expire_at < ?", (time.time(),)).rowcount:
        cleanup_orphans(cur)


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, avatar_emoji TEXT DEFAULT '👤', is_bot INTEGER DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL, receiver_id TEXT NOT NULL, text TEXT NOT NULL, timestamp TEXT NOT NULL, is_read INTEGER DEFAULT 0, reply_to INTEGER DEFAULT NULL, is_edited INTEGER DEFAULT 0, invite_id INTEGER DEFAULT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, username TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL, members TEXT NOT NULL, avatar_emoji TEXT NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS invites (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL, target_user_id INTEGER NOT NULL, status TEXT DEFAULT 'pending')")
    cur.execute("CREATE TABLE IF NOT EXISTS typing (user_id INTEGER PRIMARY KEY, peer_id TEXT, timestamp REAL)")
    cur.execute("CREATE TABLE IF NOT EXISTS reactions (msg_id INTEGER NOT NULL, user_id INTEGER NOT NULL, emoji TEXT NOT NULL, PRIMARY KEY (msg_id, user_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS files (msg_id INTEGER PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, kind TEXT NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS blocks (blocker_id INTEGER NOT NULL, blocked_id INTEGER NOT NULL, PRIMARY KEY (blocker_id, blocked_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    cur.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('db_id', ?)", (uuid.uuid4().hex,))   # меняется, если базу пересоздали
    cur.execute("CREATE TABLE IF NOT EXISTS group_reads (group_id INTEGER NOT NULL, user_id INTEGER NOT NULL, last_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, user_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS voices (msg_id INTEGER PRIMARY KEY, mime TEXT NOT NULL, data BLOB NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS hidden_msgs (user_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, PRIMARY KEY (user_id, msg_id))")   # «удалить у себя»
    cur.execute("CREATE TABLE IF NOT EXISTS chat_clear (user_id INTEGER NOT NULL, peer TEXT NOT NULL, upto INTEGER NOT NULL DEFAULT 0, gone INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, peer))")   # очистка истории у себя
    cur.execute("CREATE TABLE IF NOT EXISTS chat_ttl (chat TEXT PRIMARY KEY, ttl INTEGER NOT NULL)")   # автоудаление сообщений, секунды
    cur.execute("CREATE TABLE IF NOT EXISTS polls (msg_id INTEGER PRIMARY KEY, question TEXT NOT NULL, options TEXT NOT NULL, multi INTEGER DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS poll_votes (msg_id INTEGER NOT NULL, user_id INTEGER NOT NULL, opt INTEGER NOT NULL, PRIMARY KEY (msg_id, user_id, opt))")

    ucols = [r['name'] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if 'avatar_img' not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN avatar_img TEXT")
    if 'avatar_v' not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN avatar_v INTEGER DEFAULT 0")
    if 'creator_ip' not in ucols:   # с какого IP/устройства создан аккаунт (лимит 3 на человека)
        cur.execute("ALTER TABLE users ADD COLUMN creator_ip TEXT")
    if 'creator_did' not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN creator_did TEXT")
    if 'last_seen' not in ucols:   # время последней активности (unix, сек) — для статуса «в сети»
        cur.execute("ALTER TABLE users ADD COLUMN last_seen REAL")
    if 'deleted' not in ucols:   # 1 — аккаунт удалён (строка остаётся, чтобы id не освободился)
        cur.execute("ALTER TABLE users ADD COLUMN deleted INTEGER DEFAULT 0")

    gcols = [r['name'] for r in cur.execute("PRAGMA table_info(groups)").fetchall()]
    if 'avatar_img' not in gcols:
        cur.execute("ALTER TABLE groups ADD COLUMN avatar_img TEXT")
    if 'avatar_v' not in gcols:
        cur.execute("ALTER TABLE groups ADD COLUMN avatar_v INTEGER DEFAULT 0")
    if 'is_channel' not in gcols:   # 1 — канал: писать может только создатель и назначенные авторы
        cur.execute("ALTER TABLE groups ADD COLUMN is_channel INTEGER DEFAULT 0")
    if 'writers' not in gcols:      # id авторов канала через запятую
        cur.execute("ALTER TABLE groups ADD COLUMN writers TEXT DEFAULT ''")

    # миграция старой базы: колонка invite_id в messages
    cols = [r['name'] for r in cur.execute("PRAGMA table_info(messages)").fetchall()]
    if 'invite_id' not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN invite_id INTEGER DEFAULT NULL")
    if 'voice_dur' not in cols:   # длительность голосового в секундах (NULL — обычное сообщение)
        cur.execute("ALTER TABLE messages ADD COLUMN voice_dur INTEGER DEFAULT NULL")

    if 'fwd_from' not in cols:    # имя автора пересланного сообщения
        cur.execute("ALTER TABLE messages ADD COLUMN fwd_from TEXT DEFAULT NULL")
    if 'expire_at' not in cols:   # когда сообщение исчезнет (NULL — никогда)
        cur.execute("ALTER TABLE messages ADD COLUMN expire_at REAL DEFAULT NULL")

    cur.execute("SELECT id FROM users WHERE id = ?", (ADMIN_BOT_ID,))
    if not cur.fetchone():
        cur.execute("INSERT OR REPLACE INTO users (id, username, display_name, password_hash, avatar_emoji, is_bot) VALUES (?, ?, ?, ?, ?, ?)",
                    (ADMIN_BOT_ID, "extera_admin", "exteraAdmin", "bot_hash", "🤖", 1))
    conn.commit()
    conn.close()


@app.after_request
def add_header(response):
    if 'Cache-Control' not in response.headers:   # аватарки отдают свой кэш
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response



# ───────────────────────── WebSocket: сервер только пушит «обновись», данные клиент берёт обычными GET ─────────────────────────
_ws_clients, _ws_lock = {}, threading.Lock()   # uid -> {ws}


def ws_push(msg, only=None):
    with _ws_lock:
        targets = [w for u, s in _ws_clients.items() if only is None or u in only for w in s]
    for w in targets:
        try:
            w.send(msg)
        except Exception:
            pass


@sock.route('/ws')
def ws_endpoint(ws):
    uid = to_int(request.args.get('user_id'))
    if not uid:
        return
    with _ws_lock:
        _ws_clients.setdefault(uid, set()).add(ws)
    try:
        while True:
            ws.receive(timeout=25)   # клиент шлёт 'p' раз в 25с; пустой тайм-аут тоже ок
    except Exception:
        pass
    finally:
        with _ws_lock:
            _ws_clients.get(uid, set()).discard(ws)
            if not _ws_clients.get(uid):
                _ws_clients.pop(uid, None)


_ver, _cache = [0], {}   # _ver растёт при любой записи → весь кэш мгновенно устаревает


def cached(ttl):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            key, v = request.full_path, _ver[0]
            hit = _cache.get(key)
            if hit and hit[0] == v and time.time() - hit[1] < ttl:
                return Response(hit[2], mimetype='application/json')
            resp = fn(*a, **k)
            if isinstance(resp, Response) and resp.status_code == 200 and resp.mimetype == 'application/json':
                if len(_cache) > 300:
                    _cache.clear()
                _cache[key] = (v, time.time(), resp.get_data())   # v взят ДО расчёта: если во время него была запись — запись не сохранится как свежая
            return resp
        return w
    return deco


@app.before_request
def _t_start():
    g._t0, g._n0, g._s0 = time.time(), turso_db.STAT['n'], turso_db.STAT['t']


@app.after_request
def _t_log(response):
    if request.path.startswith('/api/') and hasattr(g, '_t0'):
        print('PERF %s %s %dms turso_calls=%d turso_ms=%d' % (request.method, request.path, (time.time() - g._t0) * 1000,
              turso_db.STAT['n'] - g._n0, (turso_db.STAT['t'] - g._s0) * 1000), flush=True)
    return response


@app.after_request
def ws_notify(response):
    if request.method in ('POST', 'PUT', 'DELETE') and request.path != '/api/ping':
        _ver[0] += 1
    if request.path.startswith('/api/') and request.method in ('POST', 'PUT', 'DELETE') and response.status_code < 400 \
            and request.path not in ('/api/typing', '/api/ping', '/api/read/'):
        ws_push('upd')   # мало пользователей → всем; клиент сам дебаунсит
    return response


@app.route('/')
def index():
    return render_template('index.html')


# ───────────────────────── аккаунты ─────────────────────────

@app.route('/api/register', methods=['POST'])
def register():
    d = body()
    u, dn, p = d.get('username', '').strip(), d.get('display_name', '').strip(), d.get('password', '').strip()
    if not (4 <= len(u) <= 12) or not (1 <= len(dn) <= 12):
        return err("Юзернейм (4-12) и имя (1-12) символов!")
    if len(p) < 4:
        return err("Пароль должен быть не короче 4 символов!")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM groups WHERE username = ?", (u,))
    if cur.fetchone():
        conn.close()
        return err("Этот юзернейм уже занят группой!")
    ip = (request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0]).strip()
    did = request.cookies.get('did') or uuid.uuid4().hex
    # лимит: максимум 3 живых аккаунта на человека (по IP или устройству); удалённые не считаются
    n = cur.execute("SELECT COUNT(*) FROM users WHERE deleted = 0 AND is_bot = 0 AND (creator_ip = ? OR creator_did = ?)", (ip, did)).fetchone()[0]
    if n >= MAX_ACCOUNTS:
        conn.close()
        return err("Лимит — 3 аккаунта на человека. Удалите один, чтобы создать новый.", 403)
    try:
        cur.execute("INSERT INTO users (username, display_name, password_hash, avatar_emoji, is_bot, creator_ip, creator_did) VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (u, dn, generate_password_hash(p), dn[0].upper(), ip, did))
        conn.commit()
        uid = cur.lastrowid
        resp = jsonify({"status": "ok", "user": {"id": uid, "username": u, "display_name": dn, "avatar_emoji": dn[0].upper(), "is_bot": 0, "avatar_url": None}})
        resp.set_cookie('did', did, max_age=60*60*24*365*10, httponly=True, samesite='Lax')
        return resp
    except turso_db.IntegrityError:
        return err("Юзернейм уже занят!")
    finally:
        conn.close()


@app.route('/api/password', methods=['POST'])
def change_password():
    d = body()
    uid = to_int(d.get('user_id'))
    old, new = str(d.get('old_password', '')).strip(), str(d.get('new_password', '')).strip()
    if not uid:
        return jsonify({"status": "error"}), 401
    if len(new) < 4:
        return err("Новый пароль должен быть не короче 4 символов!")
    conn = get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE id = ? AND deleted = 0 AND is_bot = 0", (uid,)).fetchone()
    if not row or not check_password_hash(row['password_hash'], old):
        conn.close()
        return err("Старый пароль неверный!")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), uid))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/block', methods=['POST'])
def block_user():
    d = body()
    uid, tid, on = to_int(d.get('user_id')), to_int(d.get('target_id')), bool(d.get('block'))
    if not uid or not tid or uid == tid:
        return err("Некорректный запрос!")
    conn = get_db()
    t = conn.execute("SELECT is_bot FROM users WHERE id = ? AND deleted = 0", (tid,)).fetchone()
    if not t or t['is_bot']:
        conn.close()
        return err("Нельзя заблокировать этого пользователя!")
    if on:
        conn.execute("INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)", (uid, tid))
    else:
        conn.execute("DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (uid, tid))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/block_status')
def block_status():
    uid, tid = request.args.get('user_id', type=int), request.args.get('target', type=int)
    conn = get_db()
    mine = bool(conn.execute("SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (uid, tid)).fetchone())
    them = bool(conn.execute("SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (tid, uid)).fetchone())
    conn.close()
    return jsonify({"blocked_by_me": mine, "blocked_me": them})


@app.route('/api/validate', methods=['POST'])
def validate_accounts():
    """Клиент присылает свои сохранённые аккаунты; возвращаем, какие из них реально есть в этой базе (id + юзернейм совпадают)."""
    d = body()
    conn = get_db()
    valid = []
    for a in (d.get('accounts') or [])[:10]:
        aid = to_int(a.get('id')) if isinstance(a, dict) else None
        if not aid or aid == ADMIN_BOT_ID:
            continue
        row = conn.execute("SELECT username FROM users WHERE id = ? AND deleted = 0 AND is_bot = 0", (aid,)).fetchone()
        if row and row['username'] == a.get('username'):
            valid.append(aid)
    db_id = conn.execute("SELECT v FROM meta WHERE k = 'db_id'").fetchone()['v']
    conn.close()
    return jsonify({"db_id": db_id, "valid": valid})


@app.route('/api/login', methods=['POST'])
def login():
    d = body()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (d.get('username', '').strip(),))
    user = cur.fetchone()
    conn.close()
    if user and check_password_hash(user['password_hash'], d.get('password', '').strip()):
        return jsonify({"status": "ok", "user": {"id": user['id'], "username": user['username'], "display_name": user['display_name'],
                                                  "avatar_emoji": user['avatar_emoji'] or user['display_name'][0].upper(), "is_bot": user['is_bot'],
                                                  "avatar_url": av_url(user['id'], user['avatar_v'])}})
    return err("Неверный логин или пароль!")


@app.route('/api/settings', methods=['POST'])
def settings():
    d = body()
    uid, dn = to_int(d.get('user_id')), d.get('display_name', '').strip()
    if not uid:
        return jsonify({"status": "error"}), 401
    if not (1 <= len(dn) <= 12):
        return err("Имя от 1 до 12 символов!")
    conn = get_db()
    av = d.get('avatar_img')
    if av is not None:   # '' — удалить аватарку, data:image/... — установить
        if av == '':
            conn.execute("UPDATE users SET avatar_img = NULL, avatar_v = 0 WHERE id = ?", (uid,))
        elif isinstance(av, str) and av.startswith('data:image/') and len(av) < 600000:
            conn.execute("UPDATE users SET avatar_img = ?, avatar_v = ? WHERE id = ?", (av, int(time.time()), uid))
        else:
            conn.close()
            return err("Некорректная картинка!")
    conn.execute("UPDATE users SET display_name = ?, avatar_emoji = ? WHERE id = ?", (dn, dn[0].upper(), uid))
    conn.commit()
    row = conn.execute("SELECT avatar_v FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return jsonify({"status": "ok", "avatar_emoji": dn[0].upper(), "avatar_url": av_url(uid, row['avatar_v']) if row else None})


@app.route('/api/delete_account', methods=['POST'])
def delete_account():
    uid = to_int(body().get('user_id'))
    if not uid or uid == ADMIN_BOT_ID:
        return jsonify({"status": "error"}), 401
    conn = get_db()
    cur = conn.cursor()
    if not cur.execute("SELECT 1 FROM users WHERE id = ? AND deleted = 0", (uid,)).fetchone():
        conn.close()
        return err("Аккаунт не найден!", 404)
    # строка остаётся (id не освобождается), юзернейм освобождается, вход невозможен
    cur.execute("UPDATE users SET username = ?, display_name = 'Удалённый аккаунт', password_hash = '!', avatar_img = NULL, avatar_v = 0, avatar_emoji = 'X', deleted = 1 WHERE id = ?",
                ('deleted_%d' % uid, uid))
    for g in cur.execute("SELECT id, members FROM groups").fetchall():
        ids = parse_members(g['members'])
        if uid in ids:
            cur.execute("UPDATE groups SET members = ? WHERE id = ?", (','.join(str(i) for i in ids if i != uid), g['id']))
    cur.execute("DELETE FROM reactions WHERE user_id = ?", (uid,))
    cur.execute("DELETE FROM typing WHERE user_id = ?", (uid,))
    cur.execute("UPDATE invites SET status = 'rejected' WHERE target_user_id = ? AND status = 'pending'", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/avatar/<int:uid>')
def avatar(uid):
    conn = get_db()
    row = conn.execute("SELECT avatar_img FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    if not row or not row['avatar_img']:
        return '', 404
    try:
        head, b64 = row['avatar_img'].split(',', 1)
        resp = Response(base64.b64decode(b64), mimetype=head[5:].split(';')[0])
    except Exception:
        return '', 404
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


# ───────────────────────── поиск ─────────────────────────

@app.route('/api/search', methods=['GET'])
def search():
    q = request.args.get('q', '').strip().lstrip('@')
    if not q:
        return jsonify([])
    conn = get_db()
    cur = conn.cursor()
    like = f"%{q}%"
    # сначала точные совпадения, потом частичные: люди, затем группы
    queries = [
        ("SELECT id, username, display_name, avatar_emoji, is_bot, avatar_v FROM users WHERE deleted = 0 AND username = ? LIMIT 1", (q,), False),
        ("SELECT id, username, name AS display_name, avatar_emoji, avatar_v FROM groups WHERE username = ? LIMIT 1", (q,), True),
        ("SELECT id, username, display_name, avatar_emoji, is_bot, avatar_v FROM users WHERE deleted = 0 AND username LIKE ? LIMIT 1", (like,), False),
        ("SELECT id, username, name AS display_name, avatar_emoji, avatar_v FROM groups WHERE username LIKE ? LIMIT 1", (like,), True),
    ]
    for sql, params, is_group in queries:
        row = cur.execute(sql, params).fetchone()
        if row:
            conn.close()
            res = dict(row)
            res['is_group'] = is_group
            v = res.pop('avatar_v', 0)
            res['avatar_url'] = gav_url(res['id'], v) if is_group else av_url(res['id'], v)
            return jsonify([res])
    conn.close()
    return jsonify([])


# ───────────────────────── список чатов ─────────────────────────

@app.route('/api/chats', methods=['GET'])
@cached(5)
def chats():
    uid = request.args.get('user_id', type=int)
    if not uid:
        return jsonify([])
    conn = get_db()
    cur = conn.cursor()
    purge_expired(cur)

    result = [{
        "id": "*1", "real_id": 1, "username": "extera_admin", "display_name": "exteraAdmin", "avatar_emoji": "🤖",
        "is_bot": True, "is_pinned": True, "last_text": "Системный бот уведомлений и приглашений", "last_time": "", "unread": 0
    }]

    me = cur.execute("SELECT username, display_name FROM users WHERE id = ?", (uid,)).fetchone()
    if me:
        hf0, hp0 = hidden_filter(cur, uid, uid)
        nl = cur.execute("SELECT m.text, m.timestamp FROM messages m WHERE m.sender_id = ? AND m.receiver_id = ?" + hf0 + " ORDER BY m.id DESC LIMIT 1", [uid, uid] + hp0).fetchone()
        result.append({"id": str(uid), "real_id": uid, "username": me['username'], "display_name": "Заметки", "avatar_emoji": "📝", "is_notes": True,
                       "is_pinned": True, "is_group": False, "last_text": nl['text'] if nl else "Сохраняйте сюда сообщения и файлы", "last_time": nl['timestamp'] if nl else "", "unread": 0, "last_mine": False, "last_read": False})

    cur.execute("SELECT DISTINCT u.id, u.username, u.display_name, u.avatar_emoji, u.is_bot, u.avatar_v FROM users u JOIN messages m ON (u.id = m.sender_id OR u.id = m.receiver_id) WHERE (m.sender_id = ? OR m.receiver_id = ?) AND u.id != ? AND u.id != 1 UNION SELECT u.id, u.username, u.display_name, u.avatar_emoji, u.is_bot, u.avatar_v FROM users u JOIN chat_clear c ON c.peer = CAST(u.id AS TEXT) WHERE c.user_id = ? AND c.gone = 0 AND u.id != ? AND u.id != 1", (uid, uid, uid, uid, uid))
    peers = [dict(row) for row in cur.fetchall()]

    # раньше было members LIKE '%uid%': id=1 «находился» в группах с участником 11, 21 и т.д.
    cur.execute("SELECT id, name, username, avatar_emoji, members, owner_id, is_channel, writers, avatar_v FROM groups")
    for g in cur.fetchall():
        member_ids = parse_members(g['members'])
        if uid not in member_ids:
            continue
        gid = f"g_{g['id']}"
        hf, hp = hidden_filter(cur, uid, gid)
        cur.execute("SELECT m.id, m.sender_id, m.text, m.timestamp FROM messages m WHERE m.receiver_id = ?" + hf + " ORDER BY m.id DESC LIMIT 1", [gid] + hp)
        lm = cur.fetchone()
        g_mine = bool(lm and lm['sender_id'] == uid)
        g_read = bool(g_mine and cur.execute("SELECT 1 FROM group_reads WHERE group_id = ? AND user_id != ? AND last_id >= ?", (g['id'], uid, lm['id'])).fetchone())
        info = []
        if member_ids:
            marks = ','.join('?' * len(member_ids))
            cur.execute(f"SELECT id, username, display_name, avatar_emoji, avatar_v FROM users WHERE id IN ({marks})", member_ids)
            by_id = {r['id']: dict(r) for r in cur.fetchall()}
            for m in member_ids:
                if m in by_id:
                    d = by_id[m]
                    d['avatar_url'] = av_url(d['id'], d.pop('avatar_v', 0))
                    info.append(d)
        result.append({
            "id": gid, "real_id": g['id'], "username": g['username'], "display_name": g['name'], "avatar_emoji": g['avatar_emoji'],
            "is_group": True, "avatar_url": gav_url(g['id'], g['avatar_v']), "last_mine": g_mine, "last_read": g_read, "is_channel": bool(g['is_channel']), "writers": g['writers'] or '', "owner_id": g['owner_id'], "members": g['members'], "members_info": info,
            "last_text": lm['text'] if lm else ("Канал создан" if g['is_channel'] else "Группа создана"), "last_time": lm['timestamp'] if lm else "", "unread": 0
        })

    for p in peers:
        pid = p['id']
        disp_id = f"*{pid}" if p['is_bot'] else str(pid)
        hf, hp = hidden_filter(cur, uid, pid)
        cur.execute("SELECT m.text, m.timestamp, m.sender_id, m.is_read FROM messages m WHERE ((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))" + hf + " ORDER BY m.id DESC LIMIT 1", [uid, pid, pid, uid] + hp)
        lm = cur.fetchone()
        if not lm:   # чат удалён — в списке его нет, пока не придёт новое сообщение (очищенный остаётся пустым)
            cr = cur.execute("SELECT gone FROM chat_clear WHERE user_id = ? AND peer = ?", (uid, str(pid))).fetchone()
            if cr and cr['gone']:
                continue
        cur.execute("SELECT COUNT(*) FROM messages m WHERE m.sender_id = ? AND m.receiver_id = ? AND m.is_read = 0" + hf, [pid, uid] + hp)
        unread = cur.fetchone()[0]
        result.append({"id": disp_id, "real_id": pid, "username": p['username'], "display_name": p['display_name'], "avatar_emoji": p['avatar_emoji'] or '👤', "avatar_url": av_url(pid, p['avatar_v']),
                       "is_group": False, "last_text": lm['text'] if lm else "", "last_time": lm['timestamp'] if lm else "", "unread": unread,
                       "last_mine": bool(lm and lm['sender_id'] == uid), "last_read": bool(lm and lm['sender_id'] == uid and lm['is_read'])})

    conn.commit()
    conn.close()
    return jsonify(result)


# ───────────────────────── сообщения ─────────────────────────

@app.route('/api/read/<peer_id>', methods=['POST'])
def mark_read(peer_id):
    uid = to_int(body().get('user_id'))
    if not uid:
        return jsonify({"status": "error"}), 401
    touch(uid)
    if str(peer_id).startswith('g_'):   # группа: запоминаем, до какого сообщения человек прочитал
        gid = to_int(peer_id[2:])
        if gid:
            conn = get_db()
            mx = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE receiver_id = ?", (peer_id,)).fetchone()[0]
            conn.execute("INSERT INTO group_reads (group_id, user_id, last_id) VALUES (?, ?, ?) ON CONFLICT(group_id, user_id) DO UPDATE SET last_id = MAX(last_id, excluded.last_id)", (gid, uid, mx))
            conn.commit()
            conn.close()
        return jsonify({"status": "ok"})
    pid = to_int(peer_id)
    if pid is not None:
        conn = get_db()
        conn.execute("UPDATE messages SET is_read = 1 WHERE sender_id = ? AND receiver_id = ?", (pid, uid))
        conn.commit()
        conn.close()
    return jsonify({"status": "ok"})


REACTIONS = {'like', 'dislike', 'heart', 'broken', 'cry', 'grin', 'laugh'}   # 👍 👎 ❤️ 💔 😭 😁 😂 (рисуются иконками на клиенте)


def attach_reactions(cur, msgs):
    for m in msgs:
        m['reactions'] = []
    by_id = {m['id']: m for m in msgs}
    ids = list(by_id)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = cur.execute(f"SELECT msg_id, user_id, emoji FROM reactions WHERE msg_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        for r in rows:
            by_id[r['msg_id']]['reactions'].append({"user_id": r['user_id'], "emoji": r['emoji']})
    return msgs


def can_access(cur, uid, msg):
    """Участвует ли uid в переписке, где лежит сообщение."""
    rid = str(msg['receiver_id'])
    if rid.startswith('g_'):
        g = cur.execute("SELECT members FROM groups WHERE id = ?", (to_int(rid[2:]),)).fetchone()
        return bool(g) and uid in parse_members(g['members'])
    return uid == msg['sender_id'] or str(uid) == rid


MSG_SELECT = ("SELECT m.id, m.sender_id, m.receiver_id, m.text, m.timestamp, m.is_read, m.reply_to, m.is_edited, m.voice_dur, m.fwd_from, m.expire_at, "
              "u.display_name AS sender_name, u.username AS sender_username, u.avatar_v AS sender_av, m.invite_id, i.status AS invite_status, "
              "f.kind AS file_kind, f.name AS file_name, f.size AS file_size "
              "FROM messages m LEFT JOIN users u ON u.id = m.sender_id LEFT JOIN invites i ON i.id = m.invite_id "
              "LEFT JOIN files f ON f.msg_id = m.id ")


def decorate(cur, uid, msgs):
    """Реакции, опросы и аватарки отправителей."""
    attach_reactions(cur, msgs)
    ids = [m['id'] for m in msgs]
    polls, votes = {}, {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in cur.execute(f"SELECT msg_id, question, options, multi FROM polls WHERE msg_id IN ({','.join('?' * len(chunk))})", chunk).fetchall():
            polls[r['msg_id']] = r
    pids = list(polls)
    for i in range(0, len(pids), 500):
        chunk = pids[i:i + 500]
        for r in cur.execute(f"SELECT msg_id, user_id, opt FROM poll_votes WHERE msg_id IN ({','.join('?' * len(chunk))})", chunk).fetchall():
            votes.setdefault(r['msg_id'], []).append((r['user_id'], r['opt']))
    for m in msgs:
        m['sender_avatar'] = av_url(m['sender_id'], m.pop('sender_av', 0))
        p = polls.get(m['id'])
        if p:
            vs = votes.get(m['id'], [])
            m['poll'] = {"question": p['question'], "multi": bool(p['multi']), "total": len({u for u, _ in vs}),
                         "options": [{"text": t, "count": sum(1 for _, o in vs if o == k), "me": any(u == uid and o == k for u, o in vs)}
                                     for k, t in enumerate(json.loads(p['options']))]}
    return msgs


def get_ttl(cur, uid, peer):
    r = cur.execute("SELECT ttl FROM chat_ttl WHERE chat = ?", (chat_key(uid, peer),)).fetchone()
    return r['ttl'] if r else 0


@app.route('/api/messages/<peer_id>', methods=['GET'])
@cached(2.5)
def get_messages(peer_id):
    uid = request.args.get('user_id', type=int)
    if not uid:
        return jsonify({"messages": [], "typing": False})
    touch(uid)
    conn = get_db()
    cur = conn.cursor()
    purge_expired(cur)
    conn.commit()

    if str(peer_id).startswith('g_'):
        gid = to_int(peer_id[2:])
        g = cur.execute("SELECT members FROM groups WHERE id = ?", (gid,)).fetchone() if gid else None
        if not g:
            conn.close()
            return jsonify({"messages": [], "typing": False, "error": "Группа не найдена."})
        if uid not in parse_members(g['members']):
            pending = cur.execute("SELECT 1 FROM invites WHERE group_id = ? AND target_user_id = ? AND status = 'pending'", (gid, uid)).fetchone()
            conn.close()
            msg = "Вы не участник группы. Примите приглашение в чате exteraAdmin." if pending else "Вы не участник группы. Читать и писать могут только участники."
            return jsonify({"messages": [], "typing": False, "error": msg})
        hf, hp = hidden_filter(cur, uid, peer_id)
        cur.execute(MSG_SELECT + "WHERE m.receiver_id = ?" + hf + " ORDER BY m.id ASC", [peer_id] + hp)
        msgs = decorate(cur, uid, [dict(r) for r in cur.fetchall()])
        reads = [(r['user_id'], r['last_id']) for r in cur.execute("SELECT user_id, last_id FROM group_reads WHERE group_id = ?", (gid,)).fetchall()]
        for m in msgs:   # прочитано, если его видел хотя бы один другой участник
            m['is_read'] = 1 if any(u != m['sender_id'] and last >= m['id'] for u, last in reads) else 0
        ttl = get_ttl(cur, uid, peer_id)
        conn.close()
        return jsonify({"messages": msgs, "typing": False, "ttl": ttl})

    pid = to_int(peer_id)
    if pid is None:
        conn.close()
        return jsonify({"messages": [], "typing": False})
    hf, hp = hidden_filter(cur, uid, pid)
    cur.execute(MSG_SELECT + "WHERE ((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))" + hf + " ORDER BY m.id ASC", [uid, pid, pid, uid] + hp)
    msgs = decorate(cur, uid, [dict(r) for r in cur.fetchall()])

    typing_status = False
    if pid != ADMIN_BOT_ID and pid != uid:
        t_row = cur.execute("SELECT timestamp FROM typing WHERE user_id = ? AND peer_id = ?", (pid, uid)).fetchone()
        if t_row and (time.time() - t_row['timestamp'] < 3.0):
            typing_status = True
    seen = cur.execute("SELECT last_seen, is_bot FROM users WHERE id = ?", (pid,)).fetchone()
    ago = int(max(0, time.time() - seen['last_seen'])) if seen and seen['last_seen'] and not seen['is_bot'] else None
    ttl = get_ttl(cur, uid, pid) if pid != ADMIN_BOT_ID else 0
    conn.close()
    return jsonify({"messages": msgs, "typing": typing_status, "peer_ago": ago, "ttl": ttl})


@app.route('/api/typing', methods=['POST'])
def update_typing():
    d = body()
    uid, peer_id = to_int(d.get('user_id')), to_int(d.get('peer_id'))
    touch(uid)
    if uid and peer_id:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO typing (user_id, peer_id, timestamp) VALUES (?, ?, ?)", (uid, peer_id, time.time()))
        conn.commit()
        conn.close()
        ws_push('typing', only={peer_id})
    return jsonify({"status": "ok"})


def deliver(cur, uid, rid, text, t, reply_to, voice_dur=None, fwd=None):
    """Кладёт сообщение в личку/группу. Возвращает (msg_id, None) или (None, готовый_ответ_с_ошибкой)."""
    if not cur.execute("SELECT 1 FROM users WHERE id = ? AND deleted = 0", (uid,)).fetchone():
        return None, err("Аккаунт не найден на сервере. Войдите заново.", 401)   # «призрачный» аккаунт после сброса базы
    touch(uid)
    rid = str(rid)
    if rid.startswith('g_'):
        gid = to_int(rid[2:])
        g = cur.execute("SELECT members, owner_id, is_channel, writers FROM groups WHERE id = ?", (gid,)).fetchone() if gid else None
        if not g or uid not in parse_members(g['members']):
            return None, err("Вы не участник группы!", 403)
        if g['is_channel'] and uid != g['owner_id'] and uid not in parse_members(g['writers']):
            return None, err("В канале писать может только создатель и назначенные им авторы.", 403)
        ttl = get_ttl(cur, uid, rid)
        cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read, reply_to, voice_dur, fwd_from, expire_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (uid, rid, text, t, reply_to, voice_dur, fwd, time.time() + ttl if ttl else None))
    else:
        rid_int = to_int(rid)
        target = cur.execute("SELECT deleted FROM users WHERE id = ?", (rid_int,)).fetchone() if rid_int is not None else None
        if not target:
            return None, err("Получатель не найден!", 404)
        if target['deleted']:
            return None, err("Аккаунт удалён — писать ему нельзя.", 403)
        if cur.execute("SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (rid_int, uid)).fetchone():
            return None, err("Пользователь ограничил получение ваших сообщений.", 403)
        if cur.execute("SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (uid, rid_int)).fetchone():
            return None, err("Вы заблокировали этого пользователя. Разблокируйте его в профиле.", 403)
        ttl = get_ttl(cur, uid, rid_int) if rid_int != ADMIN_BOT_ID else 0
        cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read, reply_to, voice_dur, fwd_from, expire_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid, rid_int, text, t, 1 if rid_int == uid else 0, reply_to, voice_dur, fwd, time.time() + ttl if ttl else None))
    return cur.lastrowid, None


@app.route('/api/send', methods=['POST'])
def send_message():
    d = body()
    uid, rid = to_int(d.get('user_id')), d.get('receiver_id')
    text, reply_to = d.get('text', '').strip(), d.get('reply_to')
    if not text or not rid or not uid:
        return jsonify({"status": "error"}), 400
    conn = get_db()
    cur = conn.cursor()
    _, e = deliver(cur, uid, rid, text, d.get('timestamp', '00:00'), reply_to)
    if e:
        conn.close()
        return e
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/send_voice', methods=['POST'])
def send_voice():
    d = body()
    uid, rid = to_int(d.get('user_id')), d.get('receiver_id')
    audio, dur = d.get('audio'), to_int(d.get('duration')) or 1
    if not uid or not rid or not isinstance(audio, str) or not audio.startswith('data:audio/') or len(audio) > 6000000:
        return err("Некорректное голосовое сообщение!")
    try:
        head, b64 = audio.split(',', 1)
        raw = base64.b64decode(b64)
        mime = head[5:].split(';')[0]
    except Exception:
        return err("Некорректное голосовое сообщение!")
    if not raw:
        return err("Пустая запись!")
    conn = get_db()
    cur = conn.cursor()
    msg_id, e = deliver(cur, uid, rid, "Голосовое сообщение", d.get('timestamp', '00:00'), d.get('reply_to'), max(1, min(dur, 600)))
    if e:
        conn.close()
        return e
    cur.execute("INSERT INTO voices (msg_id, mime, data) VALUES (?, ?, ?)", (msg_id, mime, raw))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/voice/<int:msg_id>')
def get_voice(msg_id):
    uid = request.args.get('user_id', type=int)
    conn = get_db()
    cur = conn.cursor()
    m = cur.execute("SELECT sender_id, receiver_id FROM messages WHERE id = ?", (msg_id,)).fetchone()
    v = cur.execute("SELECT mime, data FROM voices WHERE msg_id = ?", (msg_id,)).fetchone()
    ok = bool(uid and m and v and can_access(cur, uid, m))
    conn.close()
    if not ok:
        return '', 404
    resp = send_file(io.BytesIO(v['data']), mimetype=v['mime'], conditional=True)   # conditional → Range, нужен Safari
    resp.headers['Cache-Control'] = 'private, max-age=31536000, immutable'
    return resp


IMG_OK = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp', 'image/avif'}   # svg/html не показываем «встроенными» (XSS)


def file_kind(mime):
    if mime in IMG_OK:
        return 'photo'
    if mime.startswith('video/'):
        return 'video'
    if mime.startswith('audio/'):
        return 'audio'
    return 'file'


def purge_files(cur):
    """Удаляет с диска файлы сообщений, которых больше нет."""
    for r in cur.execute("SELECT path FROM files WHERE msg_id NOT IN (SELECT id FROM messages)").fetchall():
        try:
            os.remove(os.path.join(UPLOAD_DIR, r['path']))
        except OSError:
            pass
    cur.execute("DELETE FROM files WHERE msg_id NOT IN (SELECT id FROM messages)")


@app.errorhandler(413)
def too_big(e):
    return err("Файл слишком большой (файлы до 200 МБ, видео до 100 МБ)!", 413)


@app.route('/api/send_file', methods=['POST'])
def send_file_msg():
    uid, rid = to_int(request.form.get('user_id')), request.form.get('receiver_id')
    f = request.files.get('file')
    if not uid or not rid or not f:
        return err("Файл не получен!")
    name = (os.path.basename((f.filename or 'file').replace('\\', '/')) or 'file')[:120]
    mime = (f.mimetype or '').lower()
    if not mime or mime == 'application/octet-stream':
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    kind = file_kind(mime)
    stored = uuid.uuid4().hex
    path = os.path.join(UPLOAD_DIR, stored)
    f.save(path)
    size = os.path.getsize(path)
    if not size or size > (MAX_VIDEO if kind == 'video' else MAX_FILE):
        os.remove(path)
        return err("Видео — до 100 МБ, остальные файлы — до 200 МБ!" if size else "Пустой файл!")
    conn = get_db()
    cur = conn.cursor()
    text = {'photo': 'Фото', 'video': 'Видео'}.get(kind, name)
    msg_id, e = deliver(cur, uid, rid, text, request.form.get('timestamp', '00:00'), to_int(request.form.get('reply_to')))
    if e:
        conn.close()
        os.remove(path)
        return e
    cur.execute("INSERT INTO files (msg_id, path, name, mime, size, kind) VALUES (?, ?, ?, ?, ?, ?)", (msg_id, stored, name, mime, size, kind))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/file/<int:msg_id>')
def get_file(msg_id):
    uid = request.args.get('user_id', type=int)
    conn = get_db()
    cur = conn.cursor()
    m = cur.execute("SELECT sender_id, receiver_id FROM messages WHERE id = ?", (msg_id,)).fetchone()
    fr = cur.execute("SELECT path, name, mime, kind FROM files WHERE msg_id = ?", (msg_id,)).fetchone()
    ok = bool(uid and m and fr and can_access(cur, uid, m))
    conn.close()
    path = os.path.join(UPLOAD_DIR, fr['path']) if ok else None
    if not ok or not os.path.isfile(path):
        return '', 404
    as_att = request.args.get('dl') == '1' or fr['kind'] == 'file'
    resp = send_file(path, mimetype=fr['mime'] if fr['kind'] != 'file' else 'application/octet-stream',
                     as_attachment=as_att, download_name=fr['name'], conditional=True)   # conditional → Range (видео/музыка)
    resp.headers['Cache-Control'] = 'private, max-age=31536000, immutable'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@app.route('/api/react', methods=['POST'])
def react():
    d = body()
    uid, mid, emo = to_int(d.get('user_id')), to_int(d.get('msg_id')), d.get('emoji')
    if not uid or not mid or emo not in REACTIONS:
        return err("Некорректная реакция!")
    conn = get_db()
    cur = conn.cursor()
    m = cur.execute("SELECT sender_id, receiver_id FROM messages WHERE id = ?", (mid,)).fetchone()
    if not m or not can_access(cur, uid, m):
        conn.close()
        return err("Сообщение не найдено!", 404)
    old = cur.execute("SELECT emoji FROM reactions WHERE msg_id = ? AND user_id = ?", (mid, uid)).fetchone()
    if old and old['emoji'] == emo:        # повторное нажатие снимает реакцию
        cur.execute("DELETE FROM reactions WHERE msg_id = ? AND user_id = ?", (mid, uid))
    else:                                   # одна реакция от человека на сообщение, как в Telegram
        cur.execute("INSERT OR REPLACE INTO reactions (msg_id, user_id, emoji) VALUES (?, ?, ?)", (mid, uid, emo))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/message/<int:msg_id>', methods=['PUT', 'DELETE'])
def modify_message(msg_id):
    d = body()
    uid = to_int(d.get('user_id'))
    if not uid:
        return jsonify({"status": "error"}), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM messages WHERE id = ? AND sender_id = ?", (msg_id, uid))
    if not cur.fetchone():
        conn.close()
        return jsonify({"status": "error"}), 404

    if request.method == 'DELETE':
        cur.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        cur.execute("DELETE FROM voices WHERE msg_id = ?", (msg_id,))
        cur.execute("DELETE FROM reactions WHERE msg_id = ?", (msg_id,))
        cleanup_orphans(cur)
    else:
        new_text = d.get('text', '').strip()
        if not new_text:
            conn.close()
            return jsonify({"status": "error"}), 400
        cur.execute("UPDATE messages SET text = ?, is_edited = 1 WHERE id = ?", (new_text, msg_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ───────────────────────── группы ─────────────────────────

def send_invite(cur, gid, target_id, text, t, group_note):
    """Приглашение + сообщение от бота (с привязкой к invite_id) + системная запись в группе."""
    cur.execute("INSERT INTO invites (group_id, target_user_id) VALUES (?, ?)", (gid, target_id))
    invite_id = cur.lastrowid
    cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read, invite_id) VALUES (1, ?, ?, ?, 0, ?)", (target_id, text, t, invite_id))
    cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read) VALUES (1, ?, ?, ?, 1)", (f"g_{gid}", group_note, t))


@app.route('/api/group/create', methods=['POST'])
def create_group():
    d = body()
    uid = to_int(d.get('user_id'))
    name = d.get('name', '').strip()
    username = d.get('username', '').strip()
    target_username = d.get('target_username', '').strip().lstrip('@')
    t = d.get('timestamp', '00:00')
    is_ch = 1 if d.get('is_channel') else 0
    kind, kind_g = ('канал', 'канала') if is_ch else ('группу', 'группы')
    if not uid:
        return jsonify({"status": "error"}), 401
    if not (1 <= len(name) <= 24):
        return err(f"Название {kind_g} от 1 до 24 символов!")
    if not (4 <= len(username) <= 12):
        return err(f"Юзернейм {kind_g} должен быть от 4 до 12 символов!")

    conn = get_db()
    cur = conn.cursor()
    if cur.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        conn.close()
        return err("Этот юзернейм уже занят пользователем!")
    target = None
    if target_username:   # участник необязателен — группу можно создать пустой
        target = cur.execute("SELECT id FROM users WHERE username = ? AND is_bot = 0 AND deleted = 0", (target_username,)).fetchone()
        if not target:
            conn.close()
            return err("Пользователь не найден!", 404)
        if target['id'] == uid:
            conn.close()
            return err("Нельзя пригласить самого себя!")

    try:
        cur.execute("INSERT INTO groups (name, username, owner_id, members, avatar_emoji, is_channel, writers) VALUES (?, ?, ?, ?, ?, ?, '')", (name, username, uid, str(uid), name[0].upper(), is_ch))
        gid = cur.lastrowid
        if not target:
            cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read) VALUES (1, ?, ?, ?, 1)", (f"g_{gid}", f"{'Канал' if is_ch else 'Группа'} «{name}» создан{'' if is_ch else 'а'}.", t))
        else:
          send_invite(cur, gid, target['id'],
                      f"Приглашение в {'канал' if is_ch else 'группу'} «{name}» (@{username}). Нажмите кнопку ниже для участия.", t,
                      f"{'Канал' if is_ch else 'Группа'} «{name}» создан{'' if is_ch else 'а'}. Приглашен пользователь @{target_username}.")
        conn.commit()
    except turso_db.IntegrityError:
        conn.rollback()
        conn.close()
        return err("Юзернейм группы занят!")
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/group/invite', methods=['POST'])
def invite_to_group():
    d = body()
    gid, uid = to_int(d.get('group_id')), to_int(d.get('user_id'))
    target_username = d.get('username', '').strip().lstrip('@')
    t = d.get('timestamp', '00:00')
    conn = get_db()
    cur = conn.cursor()
    g = cur.execute("SELECT id, name, username, owner_id, members, is_channel FROM groups WHERE id = ?", (gid,)).fetchone()
    if not g or g['owner_id'] != uid:
        conn.close()
        return err("Только создатель может приглашать!", 403)
    target = cur.execute("SELECT id FROM users WHERE username = ? AND is_bot = 0", (target_username,)).fetchone()
    if not target:
        conn.close()
        return err("Пользователь не найден!", 404)
    if target['id'] in parse_members(g['members']):
        conn.close()
        return err("Пользователь уже в группе!")
    if cur.execute("SELECT 1 FROM invites WHERE group_id = ? AND target_user_id = ? AND status = 'pending'", (gid, target['id'])).fetchone():
        conn.close()
        return err("Приглашение уже отправлено!")

    send_invite(cur, gid, target['id'],
                f"Приглашение в {'канал' if g['is_channel'] else 'группу'} «{g['name']}» (@{g['username']}). Нажмите кнопку ниже для участия.", t,
                f"Пользователь @{target_username} приглашен в группу.")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/group/action', methods=['POST'])
def group_action():
    d = body()
    uid, action, invite_id = to_int(d.get('user_id')), d.get('action'), to_int(d.get('invite_id'))
    if not uid:
        return jsonify({"status": "error"}), 401
    conn = get_db()
    cur = conn.cursor()
    # раньше бралось «последнее приглашение», теперь именно то, на чью кнопку нажали
    if invite_id:
        inv = cur.execute("SELECT * FROM invites WHERE id = ? AND target_user_id = ? AND status = 'pending'", (invite_id, uid)).fetchone()
    else:
        inv = cur.execute("SELECT * FROM invites WHERE target_user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if not inv:
        conn.close()
        return err("Приглашение уже обработано или не найдено.", 404)

    if action == 'accept':
        g = cur.execute("SELECT members FROM groups WHERE id = ?", (inv['group_id'],)).fetchone()
        if not g:
            cur.execute("UPDATE invites SET status = 'rejected' WHERE id = ?", (inv['id'],))
            conn.commit()
            conn.close()
            return err("Группа больше не существует.", 404)
        members = parse_members(g['members'])
        if uid not in members:
            members.append(uid)
        cur.execute("UPDATE groups SET members = ? WHERE id = ?", (','.join(map(str, members)), inv['group_id']))
        cur.execute("UPDATE invites SET status = 'accepted' WHERE id = ?", (inv['id'],))
        user = cur.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
        cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read) VALUES (1, ?, ?, ?, 1)",
                    (f"g_{inv['group_id']}", f"@{user['username']} вступил в группу.", d.get('timestamp', '00:00')))
    else:
        cur.execute("UPDATE invites SET status = 'rejected' WHERE id = ?", (inv['id'],))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/group/update', methods=['POST'])
def update_group():
    d = body()
    gid, uid = to_int(d.get('group_id')), to_int(d.get('user_id'))
    name = d.get('name', '').strip()
    username = d.get('username', '').strip()
    if not (1 <= len(name) <= 24):
        return err("Название группы от 1 до 24 символов!")
    if not (4 <= len(username) <= 12):
        return err("Юзернейм должен быть от 4 до 12 символов!")

    conn = get_db()
    cur = conn.cursor()
    g = cur.execute("SELECT owner_id FROM groups WHERE id = ?", (gid,)).fetchone()
    if not g or g['owner_id'] != uid:
        conn.close()
        return err("Только создатель может изменять группу!", 403)
    if cur.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        conn.close()
        return err("Юзернейм занят пользователем!")
    try:
        cur.execute("UPDATE groups SET name = ?, username = ?, avatar_emoji = ? WHERE id = ?", (name, username, name[0].upper(), gid))
        conn.commit()
    except turso_db.IntegrityError:
        conn.close()
        return err("Юзернейм группы занят!")
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/group_avatar/<int:gid>')
def group_avatar(gid):
    conn = get_db()
    row = conn.execute("SELECT avatar_img FROM groups WHERE id = ?", (gid,)).fetchone()
    conn.close()
    if not row or not row['avatar_img']:
        return '', 404
    try:
        head, b64 = row['avatar_img'].split(',', 1)
        resp = Response(base64.b64decode(b64), mimetype=head[5:].split(';')[0])
    except Exception:
        return '', 404
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


@app.route('/api/group/avatar', methods=['POST'])
def set_group_avatar():
    d = body()
    gid, uid, av = to_int(d.get('group_id')), to_int(d.get('user_id')), d.get('avatar_img')
    conn = get_db()
    g = conn.execute("SELECT owner_id FROM groups WHERE id = ?", (gid,)).fetchone() if gid else None
    if not g or g['owner_id'] != uid:
        conn.close()
        return err("Только создатель может менять фото!", 403)
    if av == '':
        conn.execute("UPDATE groups SET avatar_img = NULL, avatar_v = 0 WHERE id = ?", (gid,))
    elif isinstance(av, str) and av.startswith('data:image/') and len(av) < 600000:
        conn.execute("UPDATE groups SET avatar_img = ?, avatar_v = ? WHERE id = ?", (av, int(time.time()), gid))
    else:
        conn.close()
        return err("Некорректная картинка!")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/group/kick', methods=['POST'])
def kick_group():
    d = body()
    gid, target_id, uid = to_int(d.get('group_id')), to_int(d.get('target_id')), to_int(d.get('user_id'))
    conn = get_db()
    cur = conn.cursor()
    g = cur.execute("SELECT owner_id, members, writers FROM groups WHERE id = ?", (gid,)).fetchone()
    if not g or g['owner_id'] != uid:
        conn.close()
        return err("Только создатель может выгонять участников!", 403)
    if target_id == uid:
        conn.close()
        return err("Себя выгнать нельзя. Удалите группу, если она не нужна.")
    members = [m for m in parse_members(g['members']) if m != target_id]
    writers = [w for w in parse_members(g['writers']) if w != target_id]
    cur.execute("UPDATE groups SET members = ?, writers = ? WHERE id = ?", (','.join(map(str, members)), ','.join(map(str, writers)), gid))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/group/writer', methods=['POST'])
def group_writer():
    """Создатель канала даёт/забирает право писать у участника."""
    d = body()
    gid, uid, tid, allow = to_int(d.get('group_id')), to_int(d.get('user_id')), to_int(d.get('target_id')), bool(d.get('allow'))
    conn = get_db()
    cur = conn.cursor()
    g = cur.execute("SELECT owner_id, members, writers, is_channel FROM groups WHERE id = ?", (gid,)).fetchone()
    if not g or g['owner_id'] != uid:
        conn.close()
        return err("Только создатель может назначать авторов!", 403)
    if not g['is_channel']:
        conn.close()
        return err("Это не канал!")
    if tid == uid or tid not in parse_members(g['members']):
        conn.close()
        return err("Нельзя изменить права этого пользователя!")
    w = [x for x in parse_members(g['writers']) if x != tid]
    if allow:
        w.append(tid)
    cur.execute("UPDATE groups SET writers = ? WHERE id = ?", (','.join(map(str, w)), gid))
    u = cur.execute("SELECT username FROM users WHERE id = ?", (tid,)).fetchone()
    cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read) VALUES (1, ?, ?, ?, 1)",
                (f"g_{gid}", f"@{u['username']} {'теперь может писать в канале.' if allow else 'больше не может писать в канале.'}", d.get('timestamp', '00:00')))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/group/delete', methods=['POST'])
def delete_group():
    d = body()
    gid, uid = to_int(d.get('group_id')), to_int(d.get('user_id'))
    conn = get_db()
    cur = conn.cursor()
    g = cur.execute("SELECT owner_id FROM groups WHERE id = ?", (gid,)).fetchone()
    if not g or g['owner_id'] != uid:
        conn.close()
        return err("Только создатель может удалить группу!", 403)
    cur.execute("DELETE FROM groups WHERE id = ?", (gid,))
    cur.execute("DELETE FROM messages WHERE receiver_id = ?", (f"g_{gid}",))
    cur.execute("DELETE FROM voices WHERE msg_id NOT IN (SELECT id FROM messages)")
    cleanup_orphans(cur)
    cur.execute("DELETE FROM chat_ttl WHERE chat = ?", (f"g_{gid}",))
    cur.execute("DELETE FROM group_reads WHERE group_id = ?", (gid,))
    cur.execute("DELETE FROM chat_clear WHERE peer = ?", (f"g_{gid}",))
    cur.execute("UPDATE invites SET status = 'rejected' WHERE group_id = ? AND status = 'pending'", (gid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ───────────────────────── статус, удаление, очистка, таймер ─────────────────────────

@app.route('/api/ping', methods=['GET', 'POST'])
def ping():
    touch(request.args.get('user_id', type=int) or to_int(body().get('user_id')))
    return jsonify({"status": "ok"})


@app.route('/api/user/<int:tid>')
def user_info(tid):
    conn = get_db()
    r = conn.execute("SELECT id, username, display_name, avatar_emoji, avatar_v, is_bot, last_seen, deleted FROM users WHERE id = ?", (tid,)).fetchone()
    conn.close()
    if not r:
        return err("Пользователь не найден!", 404)
    ago = None if (r['is_bot'] or r['deleted'] or not r['last_seen']) else int(max(0, time.time() - r['last_seen']))
    return jsonify({"id": r['id'], "username": r['username'], "display_name": r['display_name'], "is_bot": r['is_bot'],
                    "avatar_url": av_url(r['id'], r['avatar_v']), "ago": ago})


@app.route('/api/messages/delete', methods=['POST'])
def delete_messages():
    """Массовое удаление: у себя (любые сообщения) или у всех (только свои)."""
    d = body()
    uid, for_all = to_int(d.get('user_id')), bool(d.get('for_all'))
    ids = [i for i in (to_int(x) for x in (d.get('ids') or [])[:200]) if i]
    if not uid or not ids:
        return err("Нечего удалять!")
    conn = get_db()
    cur = conn.cursor()
    done = 0
    for mid in ids:
        m = cur.execute("SELECT sender_id, receiver_id FROM messages WHERE id = ?", (mid,)).fetchone()
        if not m or not can_access(cur, uid, m):
            continue
        if for_all:
            if m['sender_id'] != uid:
                continue
            cur.execute("DELETE FROM messages WHERE id = ?", (mid,))
        else:
            cur.execute("INSERT OR IGNORE INTO hidden_msgs (user_id, msg_id) VALUES (?, ?)", (uid, mid))
        done += 1
    cleanup_orphans(cur)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "count": done})


@app.route('/api/chat/clear', methods=['POST'])
def clear_chat():
    """Очистка истории у себя (собеседник свою переписку сохраняет)."""
    d = body()
    uid, peer = to_int(d.get('user_id')), str(d.get('peer') or '')
    if not uid or not peer:
        return err("Некорректный запрос!")
    conn = get_db()
    cur = conn.cursor()
    if peer.startswith('g_'):
        g = cur.execute("SELECT members FROM groups WHERE id = ?", (to_int(peer[2:]),)).fetchone()
        if not g or uid not in parse_members(g['members']):
            conn.close()
            return err("Вы не участник группы!", 403)
        mx = cur.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE receiver_id = ?", (peer,)).fetchone()[0]
    else:
        pid = to_int(peer)
        if pid is None:
            conn.close()
            return err("Некорректный запрос!")
        peer = str(pid)
        mx = cur.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)", (uid, pid, pid, uid)).fetchone()[0]
    cur.execute("INSERT INTO chat_clear (user_id, peer, upto, gone) VALUES (?, ?, ?, ?) ON CONFLICT(user_id, peer) DO UPDATE SET upto = MAX(upto, excluded.upto), gone = excluded.gone",
                (uid, peer, mx, 1 if d.get('delete') and not peer.startswith('g_') and peer != str(uid) else 0))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/chat/delete_all', methods=['POST'])
def chat_delete_all():
    """Очистка/удаление у всех: личка — переписка исчезает у обоих; группа/канал — только владелец."""
    d = body()
    uid, peer, dele = to_int(d.get('user_id')), str(d.get('peer') or ''), bool(d.get('delete'))
    if not uid or not peer:
        return err("Некорректный запрос!")
    conn = get_db()
    cur = conn.cursor()
    if peer.startswith('g_'):
        gid = to_int(peer[2:])
        g = cur.execute("SELECT owner_id FROM groups WHERE id = ?", (gid,)).fetchone() if gid else None
        if not g or g['owner_id'] != uid:
            conn.close()
            return err("Это может сделать только создатель!", 403)
        cur.execute("DELETE FROM messages WHERE receiver_id = ?", (peer,))
        if dele:
            cur.execute("DELETE FROM groups WHERE id = ?", (gid,))
            cur.execute("DELETE FROM chat_ttl WHERE chat = ?", (peer,))
            cur.execute("DELETE FROM group_reads WHERE group_id = ?", (gid,))
            cur.execute("DELETE FROM chat_clear WHERE peer = ?", (peer,))
            cur.execute("UPDATE invites SET status = 'rejected' WHERE group_id = ? AND status = 'pending'", (gid,))
    else:
        pid = to_int(peer)
        if pid is None or pid == ADMIN_BOT_ID:
            conn.close()
            return err("Некорректный запрос!")
        if pid == uid:
            cur.execute("DELETE FROM messages WHERE sender_id = ? AND receiver_id = ?", (uid, uid))
        else:
            cur.execute("DELETE FROM messages WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)", (uid, pid, pid, uid))
            if dele:
                cur.execute("DELETE FROM chat_clear WHERE user_id = ? AND peer = ?", (pid, str(uid)))
                cur.execute("INSERT INTO chat_clear (user_id, peer, upto, gone) VALUES (?, ?, 0, 1) ON CONFLICT(user_id, peer) DO UPDATE SET gone = 1", (uid, str(pid)))
            else:   # очистка у всех: чат остаётся у обоих, пустой
                for a, b in ((uid, pid), (pid, uid)):
                    cur.execute("INSERT INTO chat_clear (user_id, peer, upto, gone) VALUES (?, ?, 0, 0) ON CONFLICT(user_id, peer) DO UPDATE SET gone = 0", (a, str(b)))
    cleanup_orphans(cur)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


TTL_LABEL = {0: 'выключено', 3600: 'через 1 час', 86400: 'через 1 сутки', 604800: 'через 7 дней'}


@app.route('/api/chat/ttl', methods=['POST'])
def set_chat_ttl():
    """Автоудаление новых сообщений: в личке может менять любой из двоих, в группе — создатель."""
    d = body()
    uid, peer, ttl = to_int(d.get('user_id')), str(d.get('peer') or ''), to_int(d.get('ttl')) or 0
    if not uid or not peer or ttl not in TTL_LABEL:
        return err("Некорректный таймер!")
    conn = get_db()
    cur = conn.cursor()
    if peer.startswith('g_'):
        g = cur.execute("SELECT owner_id FROM groups WHERE id = ?", (to_int(peer[2:]),)).fetchone()
        if not g or g['owner_id'] != uid:
            conn.close()
            return err("Таймер удаления меняет только создатель!", 403)
        cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read) VALUES (1, ?, ?, ?, 1)",
                    (peer, f"Автоудаление сообщений: {TTL_LABEL[ttl]}.", d.get('timestamp', '00:00')))
    else:
        pid = to_int(peer)
        t = cur.execute("SELECT deleted, is_bot FROM users WHERE id = ?", (pid,)).fetchone() if pid else None
        if not t or t['deleted'] or t['is_bot']:
            conn.close()
            return err("Для этого чата таймер недоступен!")
        peer = pid
    key = chat_key(uid, peer)
    if ttl:
        cur.execute("INSERT OR REPLACE INTO chat_ttl (chat, ttl) VALUES (?, ?)", (key, ttl))
    else:
        cur.execute("DELETE FROM chat_ttl WHERE chat = ?", (key,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/forward', methods=['POST'])
def forward_messages():
    d = body()
    uid, rid, t = to_int(d.get('user_id')), d.get('receiver_id'), d.get('timestamp', '00:00')
    ids = sorted({i for i in (to_int(x) for x in (d.get('ids') or [])[:100]) if i})
    if not uid or not rid or not ids:
        return err("Нечего пересылать!")
    conn = get_db()
    cur = conn.cursor()
    sent = 0
    for mid in ids:
        m = cur.execute("SELECT m.*, u.display_name AS sname FROM messages m LEFT JOIN users u ON u.id = m.sender_id WHERE m.id = ?", (mid,)).fetchone()
        if not m or m['invite_id'] or m['sender_id'] == ADMIN_BOT_ID or not can_access(cur, uid, m):
            continue
        if cur.execute("SELECT 1 FROM polls WHERE msg_id = ?", (mid,)).fetchone():
            continue
        f = cur.execute("SELECT path, name, mime, size, kind FROM files WHERE msg_id = ?", (mid,)).fetchone()
        src = os.path.join(UPLOAD_DIR, f['path']) if f else None
        if f and not os.path.isfile(src):
            continue
        new_id, e = deliver(cur, uid, rid, m['text'], t, None, m['voice_dur'], m['fwd_from'] or m['sname'] or 'Пользователь')
        if e:
            conn.commit()
            conn.close()
            return e
        v = cur.execute("SELECT mime, data FROM voices WHERE msg_id = ?", (mid,)).fetchone()
        if v:
            cur.execute("INSERT INTO voices (msg_id, mime, data) VALUES (?, ?, ?)", (new_id, v['mime'], v['data']))
        if f:
            stored = uuid.uuid4().hex
            shutil.copyfile(src, os.path.join(UPLOAD_DIR, stored))
            cur.execute("INSERT INTO files (msg_id, path, name, mime, size, kind) VALUES (?, ?, ?, ?, ?, ?)", (new_id, stored, f['name'], f['mime'], f['size'], f['kind']))
        sent += 1
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "count": sent})


@app.route('/api/media/<peer_id>')
def chat_media(peer_id):
    """Фото/видео (type=media) или файлы/музыка (type=files) чата — по возрастанию id."""
    uid = request.args.get('user_id', type=int)
    kinds = ('file', 'audio') if request.args.get('type') == 'files' else ('photo', 'video')
    if not uid:
        return jsonify([])
    conn = get_db()
    cur = conn.cursor()
    if str(peer_id).startswith('g_'):
        g = cur.execute("SELECT members FROM groups WHERE id = ?", (to_int(peer_id[2:]),)).fetchone()
        if not g or uid not in parse_members(g['members']):
            conn.close()
            return jsonify([])
        cond, params, peer = "m.receiver_id = ?", [peer_id], peer_id
    else:
        pid = to_int(peer_id)
        if pid is None:
            conn.close()
            return jsonify([])
        cond, params, peer = "((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))", [uid, pid, pid, uid], pid
    hf, hp = hidden_filter(cur, uid, peer)
    rows = cur.execute("SELECT m.id, m.sender_id, m.timestamp, f.kind, f.name, f.size FROM messages m JOIN files f ON f.msg_id = m.id "
                       f"WHERE f.kind IN (?, ?) AND {cond}" + hf + " ORDER BY m.id ASC", list(kinds) + params + hp).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ───────────────────────── опросы ─────────────────────────

@app.route('/api/poll/create', methods=['POST'])
def poll_create():
    d = body()
    uid, rid = to_int(d.get('user_id')), str(d.get('receiver_id') or '')
    q = str(d.get('question') or '').strip()
    opts = []
    for o in (d.get('options') or [])[:20]:
        o = str(o).strip()
        if o and o not in opts:
            opts.append(o)
    if not uid or not rid.startswith('g_'):
        return err("Опросы доступны только в группах!")
    if not (1 <= len(q) <= 120):
        return err("Вопрос — от 1 до 120 символов!")
    if not (2 <= len(opts) <= 10) or any(len(o) > 60 for o in opts):
        return err("Нужно от 2 до 10 вариантов (до 60 символов)!")
    conn = get_db()
    cur = conn.cursor()
    msg_id, e = deliver(cur, uid, rid, q, d.get('timestamp', '00:00'), None)
    if e:
        conn.close()
        return e
    cur.execute("INSERT INTO polls (msg_id, question, options, multi) VALUES (?, ?, ?, ?)", (msg_id, q, json.dumps(opts, ensure_ascii=False), 1 if d.get('multi') else 0))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/poll/vote', methods=['POST'])
def poll_vote():
    d = body()
    uid, mid, opt = to_int(d.get('user_id')), to_int(d.get('msg_id')), to_int(d.get('opt'))
    if not uid or not mid or opt is None:
        return err("Некорректный голос!")
    conn = get_db()
    cur = conn.cursor()
    m = cur.execute("SELECT sender_id, receiver_id FROM messages WHERE id = ?", (mid,)).fetchone()
    p = cur.execute("SELECT options, multi FROM polls WHERE msg_id = ?", (mid,)).fetchone()
    if not m or not p or not can_access(cur, uid, m) or not (0 <= opt < len(json.loads(p['options']))):
        conn.close()
        return err("Опрос не найден!", 404)
    if cur.execute("SELECT 1 FROM poll_votes WHERE msg_id = ? AND user_id = ? AND opt = ?", (mid, uid, opt)).fetchone():
        cur.execute("DELETE FROM poll_votes WHERE msg_id = ? AND user_id = ? AND opt = ?", (mid, uid, opt))   # повторный тап снимает голос
    else:
        if not p['multi']:
            cur.execute("DELETE FROM poll_votes WHERE msg_id = ? AND user_id = ?", (mid, uid))
        cur.execute("INSERT INTO poll_votes (msg_id, user_id, opt) VALUES (?, ?, ?)", (mid, uid, opt))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
