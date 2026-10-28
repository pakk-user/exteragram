import uuid, re, hmac, hashlib, threading, tempfile, gzip, sqlite3, datetime, requests
import turso_db, time, os, base64, io, uuid, mimetypes, json, shutil
from flask import Flask, render_template, request, jsonify, send_from_directory, Response, send_file
from werkzeug.security import generate_password_hash, check_password_hash

MAX_ACCOUNTS = 3
MAX_WARNS = 3       # предупреждений на аккаунт до автоматического удаления
DEV_MAX_WARNS = 3   # блокировок аккаунтов с одного устройства до бана устройства
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY') or uuid.uuid4().hex   # задайте FLASK_SECRET_KEY на Render
if not os.environ.get('FLASK_SECRET_KEY'):
    print('WARNING: FLASK_SECRET_KEY не задан — используется временный ключ')
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


HIDDEN_AGO = 10 ** 8   # «был(а) недавно» — когда человек скрыл статус «в сети»
_WIPE = ("premium = 0, premium_until = NULL, nick_color = NULL, status_emoji = NULL, hide_online = 0, avatar_anim = NULL, "
         "avatar_v = CASE WHEN avatar_anim IS NOT NULL THEN (CASE WHEN avatar_img IS NULL THEN 0 ELSE ? END) ELSE avatar_v END")
_prem_sweep = [0]


def prem_active(r):
    return bool(r['premium']) and (not r['premium_until'] or r['premium_until'] > time.time())


def prem_info(cur, ids):
    """{id: {premium, nick_color, status_emoji}} только для тех, у кого премиум сейчас действует."""
    ids = list({i for i in ids if isinstance(i, int)})
    out = {}
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        rows = cur.execute("SELECT id, nick_color, status_emoji, premium, premium_until, public_id FROM users WHERE ((premium = 1 AND (premium_until IS NULL OR premium_until > ?)) OR public_id IS NOT NULL) AND id IN (%s)" % ','.join('?' * len(ch)),
                           [time.time()] + ch).fetchall()
        for r in rows:
            a = prem_active(r)
            out[r['id']] = {"premium": a, "nick_color": (r['nick_color'] or None) if a else None, "status_emoji": (r['status_emoji'] or None) if a else None, "public_id": r['public_id']}
    return out


def me_prem(r):
    """Свои премиум-данные (для настроек профиля)."""
    return {"premium": prem_active(r), "premium_until": r['premium_until'], "nick_color": r['nick_color'], "status_emoji": r['status_emoji'],
            "hide_online": bool(r['hide_online']), "public_id": r['public_id'], "bio": r['bio'], "role": r['role'], "avatar_url": av_url(r['id'], r['avatar_v'])}


def expire_premium(cur):
    """Истёкший премиум снимается: всё, что он давал, пропадает, обычная аватарка возвращается."""
    now = time.time()
    if now - _prem_sweep[0] < 30:
        return
    _prem_sweep[0] = now
    cur.execute("UPDATE users SET " + _WIPE + " WHERE premium = 1 AND premium_until IS NOT NULL AND premium_until < ?", (int(now), now))


def set_premium(cur, uid, on, days=None):
    if on:
        cur.execute("UPDATE users SET premium = 1, premium_until = ? WHERE id = ?", (time.time() + days * 86400 if days else None, uid))
    else:
        cur.execute("UPDATE users SET " + _WIPE + " WHERE id = ?", (int(time.time()), uid))


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
    row = cur.execute("SELECT upto FROM chat_clear WHERE user_id = ? AND peer = ?", (uid, str(peer))).fetchone()
    return " AND m.id > ? AND m.id NOT IN (SELECT msg_id FROM hidden_msgs WHERE user_id = ?)", [row['upto'] if row else 0, uid]


def cleanup_orphans(cur):
    cur.execute("DELETE FROM voices WHERE msg_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM reactions WHERE msg_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM polls WHERE msg_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM poll_votes WHERE msg_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM hidden_msgs WHERE msg_id NOT IN (SELECT id FROM messages)")
    purge_files(cur)


def purge_expired(cur):
    if cur.execute("DELETE FROM messages WHERE expire_at IS NOT NULL AND expire_at < ?", (time.time(),)).rowcount:
        cleanup_orphans(cur)
        check_reports(cur)


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

    cur.execute("CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL, target_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, reason TEXT NOT NULL, details TEXT, status TEXT NOT NULL DEFAULT 'sent', created REAL NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS report_ctx (report_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, text TEXT, is_edited INTEGER DEFAULT 0, PRIMARY KEY (report_id, msg_id))")   # свои сообщения жалобщика в контексте
    cur.execute("CREATE TABLE IF NOT EXISTS report_tg (report_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, tg_msg INTEGER NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS chat_archive (user_id INTEGER NOT NULL, peer TEXT NOT NULL, PRIMARY KEY (user_id, peer))")   # архив чатов (режим тишины)
    cur.execute("CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, did TEXT NOT NULL, name TEXT, created REAL, last_seen REAL, revoked INTEGER DEFAULT 0, UNIQUE (user_id, did))")   # устройства (сеансы)
    cur.execute("CREATE TABLE IF NOT EXISTS report_admins (tg_id INTEGER PRIMARY KEY)")   # кто, кроме главного админа, получает жалобы
    cur.execute("CREATE TABLE IF NOT EXISTS warns (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, reason TEXT NOT NULL, report_id INTEGER, created REAL NOT NULL, by_mod TEXT)")   # предупреждения аккаунтов
    cur.execute("CREATE TABLE IF NOT EXISTS device_warns (dkey TEXT PRIMARY KEY, ip TEXT, n INTEGER NOT NULL DEFAULT 0, updated REAL)")   # «предупреждения» устройства (пользователь их не видит)
    cur.execute("CREATE TABLE IF NOT EXISTS banned_devices (dkey TEXT PRIMARY KEY, ip TEXT, created REAL)")   # устройства, которым нельзя создавать аккаунты
    fcols = [r['name'] for r in cur.execute("PRAGMA table_info(files)").fetchall()]
    if 'has_cap' not in fcols:   # 1 — у файла есть подпись (она лежит в messages.text)
        cur.execute("ALTER TABLE files ADD COLUMN has_cap INTEGER DEFAULT 0")

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

    for col, ddl in (('premium', 'INTEGER DEFAULT 0'), ('premium_until', 'REAL'), ('nick_color', 'TEXT'),
                     ('status_emoji', 'TEXT'), ('hide_online', 'INTEGER DEFAULT 0'), ('avatar_anim', 'TEXT'), ('public_id', 'INTEGER'), ('bio', 'TEXT'), ('role', 'TEXT')):
        if col not in ucols:   # премиум: цвет ника, эмодзи-статус, скрытие «в сети», анимированная аватарка
            cur.execute("ALTER TABLE users ADD COLUMN %s %s" % (col, ddl))
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_id ON users(public_id)")   # красивый «уникальный ID» для показа в профиле

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
    if 'btn' not in cols:         # кнопка под сообщением бота ('appeal' — «Обжаловать»)
        cur.execute("ALTER TABLE messages ADD COLUMN btn TEXT DEFAULT NULL")

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
    if device_banned(cur, request.cookies.get('did'), ip):   # устройство заблокировано за нарушения
        conn.close()
        return err("С этого устройства создание аккаунтов заблокировано.", 403)
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
        dev_register(uid, d.get('device_id'))
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
    valid, names = [], {}
    for a in (d.get('accounts') or [])[:10]:
        aid = to_int(a.get('id')) if isinstance(a, dict) else None
        if not aid or aid == ADMIN_BOT_ID:
            continue
        row = conn.execute("SELECT username FROM users WHERE id = ? AND deleted = 0 AND is_bot = 0", (aid,)).fetchone()
        if row:   # от сброса базы защищает db_id; юзернейм мог поменяться (его выдаёт админ)
            valid.append(aid)
            names[aid] = row['username']
    db_id = conn.execute("SELECT v FROM meta WHERE k = 'db_id'").fetchone()['v']
    conn.close()
    return jsonify({"db_id": db_id, "valid": valid, "names": names})


@app.route('/api/login', methods=['POST'])
def login():
    d = body()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (d.get('username', '').strip(),))
    user = cur.fetchone()
    conn.close()
    if user and check_password_hash(user['password_hash'], d.get('password', '').strip()):
        dev_register(user['id'], d.get('device_id'))
        return jsonify({"status": "ok", "user": {"id": user['id'], "username": user['username'], "display_name": user['display_name'],
                                                  "avatar_emoji": user['avatar_emoji'] or user['display_name'][0].upper(), "is_bot": user['is_bot'],
                                                  "avatar_url": av_url(user['id'], user['avatar_v']), **me_prem(user)}})
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
    me = conn.execute("SELECT premium, premium_until FROM users WHERE id = ?", (uid,)).fetchone()
    if not me:
        conn.close()
        return err("Аккаунт не найден!", 404)
    prem = prem_active(me)
    NEED = "Это доступно только с премиумом"
    sets, av_changed = [], False
    av = d.get('avatar_img')
    if av is not None:   # '' — удалить аватарку, data:image/... — установить
        if av == '':
            sets.append(("avatar_img", None))
        elif isinstance(av, str) and av.startswith('data:image/') and len(av) < 600000:
            sets.append(("avatar_img", av))
        else:
            conn.close()
            return err("Некорректная картинка!")
        av_changed = True
    an = d.get('avatar_anim')
    if an is not None:   # анимированная аватарка (премиум); убрать можно всегда
        if an == '':
            sets.append(("avatar_anim", None))
        elif isinstance(an, str) and re.match(r'data:image/(gif|webp);base64,', an) and len(an) < 1300000:
            if not prem:
                conn.close()
                return err(NEED, 403)
            sets.append(("avatar_anim", an))
        else:
            conn.close()
            return err("Некорректная картинка!")
        av_changed = True
    if 'nick_color' in d:
        c = d.get('nick_color') or ''
        if c and not re.fullmatch(r'(theme|g:theme|#[0-9a-fA-F]{6}|g:#[0-9a-fA-F]{6}(,#[0-9a-fA-F]{6}){1,3})', str(c)):
            conn.close()
            return err("Некорректный цвет!")
        if c and not prem:
            conn.close()
            return err(NEED, 403)
        sets.append(("nick_color", c or None))
    if 'bio' in d:   # «пара слов о себе» — всем, до 80 символов
        b = re.sub(r'\s+', ' ', str(d.get('bio') or '')).strip()
        if len(b) > 80:
            conn.close()
            return err("Максимум 80 символов!")
        sets.append(("bio", b or None))
    if 'status_emoji' in d:
        e = (d.get('status_emoji') or '').strip()
        if len(e) > 8 or any(ch in e for ch in '<>&"\''):
            conn.close()
            return err("Некорректный эмодзи-статус!")
        if e and not prem:
            conn.close()
            return err(NEED, 403)
        sets.append(("status_emoji", e or None))
    if 'hide_online' in d:
        h = 1 if d.get('hide_online') else 0
        if h and not prem:
            conn.close()
            return err(NEED, 403)
        sets.append(("hide_online", h))
    conn.execute("UPDATE users SET display_name = ?, avatar_emoji = ? WHERE id = ?", (dn, dn[0].upper(), uid))
    for col, val in sets:   # имена колонок — из фиксированного списка выше
        conn.execute("UPDATE users SET %s = ? WHERE id = ?" % col, (val, uid))
    if av_changed:
        conn.execute("UPDATE users SET avatar_v = CASE WHEN avatar_img IS NOT NULL OR avatar_anim IS NOT NULL THEN ? ELSE 0 END WHERE id = ?", (int(time.time()), uid))
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return jsonify({"status": "ok", "avatar_emoji": dn[0].upper(), "avatar_url": av_url(uid, row['avatar_v']) if row else None,
                    "me": me_prem(row) if row else None})


def do_delete_account(cur, uid):
    # строка остаётся (id не освобождается), юзернейм освобождается, вход невозможен
    cur.execute("UPDATE users SET username = ?, display_name = 'Удалённый аккаунт', password_hash = '!', avatar_img = NULL, avatar_v = 0, avatar_emoji = 'X', deleted = 1, premium = 0, premium_until = NULL, nick_color = NULL, status_emoji = NULL, hide_online = 0, avatar_anim = NULL, public_id = NULL WHERE id = ?",
                ('deleted_%d' % uid, uid))
    for g in cur.execute("SELECT id, members FROM groups").fetchall():
        ids = parse_members(g['members'])
        if uid in ids:
            cur.execute("UPDATE groups SET members = ? WHERE id = ?", (','.join(str(i) for i in ids if i != uid), g['id']))
    cur.execute("DELETE FROM reactions WHERE user_id = ?", (uid,))
    cur.execute("DELETE FROM typing WHERE user_id = ?", (uid,))
    cur.execute("UPDATE invites SET status = 'rejected' WHERE target_user_id = ? AND status = 'pending'", (uid,))


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
    do_delete_account(cur, uid)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/avatar/<int:uid>')
def avatar(uid):
    conn = get_db()
    row = conn.execute("SELECT avatar_img, avatar_anim, premium, premium_until FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    img = row and ((row['avatar_anim'] if prem_active(row) and row['avatar_anim'] else None) or row['avatar_img'])
    if not img:
        return '', 404
    try:
        head, b64 = img.split(',', 1)
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
            res = dict(row)
            res['is_group'] = is_group
            if not is_group:
                res.update(prem_info(cur, [res['id']]).get(res['id'], {"premium": False}))
            conn.close()
            v = res.pop('avatar_v', 0)
            res['avatar_url'] = gav_url(res['id'], v) if is_group else av_url(res['id'], v)
            return jsonify([res])
    conn.close()
    return jsonify([])


# ───────────────────────── список чатов ─────────────────────────

@app.route('/api/chats', methods=['GET'])
def chats():
    uid = request.args.get('user_id', type=int)
    if not uid:
        return jsonify([])
    conn = get_db()
    cur = conn.cursor()
    purge_expired(cur)
    expire_premium(cur)

    result = [{
        "id": "*1", "real_id": 1, "username": "extera_admin", "display_name": "exteraAdmin", "avatar_emoji": "🤖",
        "is_bot": True, "is_pinned": True, "last_text": "Системный бот уведомлений и приглашений", "last_time": "", "unread": 0
    }]

    bl = cur.execute("SELECT text, timestamp FROM messages WHERE sender_id = 1 AND receiver_id = ? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if bl:
        result[0]["last_text"], result[0]["last_time"] = bl['text'], bl['timestamp']
        result[0]["unread"] = cur.execute("SELECT COUNT(*) FROM messages WHERE sender_id = 1 AND receiver_id = ? AND is_read = 0", (uid,)).fetchone()[0]
    me = cur.execute("SELECT id, username, display_name, avatar_v, premium, premium_until, nick_color, status_emoji, hide_online, public_id, bio, role FROM users WHERE id = ?", (uid,)).fetchone()
    if me:
        hf0, hp0 = hidden_filter(cur, uid, uid)
        nl = cur.execute("SELECT m.text, m.timestamp FROM messages m WHERE m.sender_id = ? AND m.receiver_id = ?" + hf0 + " ORDER BY m.id DESC LIMIT 1", [uid, uid] + hp0).fetchone()
        result.append({"id": str(uid), "real_id": uid, "username": me['username'], "display_name": "Заметки", "avatar_emoji": "📝", "is_notes": True,
                       "me": me_prem(me), "is_pinned": True, "is_group": False, "last_text": nl['text'] if nl else "Сохраняйте сюда сообщения и файлы", "last_time": nl['timestamp'] if nl else "", "unread": 0, "last_mine": False, "last_read": False})

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
            "last_text": lm['text'] if lm else ("Канал создан" if g['is_channel'] else "Группа создана"), "last_time": lm['timestamp'] if lm else "", "unread": 0, "_lid": (lm['id'] if lm else 0)
        })

    for p in peers:
        pid = p['id']
        disp_id = f"*{pid}" if p['is_bot'] else str(pid)
        hf, hp = hidden_filter(cur, uid, pid)
        cur.execute("SELECT m.id AS mid, m.text, m.timestamp, m.sender_id, m.is_read FROM messages m WHERE ((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))" + hf + " ORDER BY m.id DESC LIMIT 1", [uid, pid, pid, uid] + hp)
        lm = cur.fetchone()
        if not lm:   # чат удалён — в списке его нет, пока не придёт новое сообщение (очищенный остаётся пустым)
            cr = cur.execute("SELECT gone FROM chat_clear WHERE user_id = ? AND peer = ?", (uid, str(pid))).fetchone()
            if cr and cr['gone']:
                continue
        cur.execute("SELECT COUNT(*) FROM messages m WHERE m.sender_id = ? AND m.receiver_id = ? AND m.is_read = 0" + hf, [pid, uid] + hp)
        unread = cur.fetchone()[0]
        result.append({"id": disp_id, "real_id": pid, "username": p['username'], "display_name": p['display_name'], "avatar_emoji": p['avatar_emoji'] or '👤', "avatar_url": av_url(pid, p['avatar_v']),
                       "is_group": False, "last_text": lm['text'] if lm else "", "last_time": lm['timestamp'] if lm else "", "unread": unread, "_lid": (lm['mid'] if lm else 0),
                       "last_mine": bool(lm and lm['sender_id'] == uid), "last_read": bool(lm and lm['sender_id'] == uid and lm['is_read'])})

    pm = prem_info(cur, [r.get('real_id') for r in result if not r.get('is_group') and not r.get('is_notes')]
                   + [m['id'] for r in result for m in r.get('members_info', [])])
    for r in result:
        if not r.get('is_group') and not r.get('is_notes'):
            r.update(pm.get(r.get('real_id'), {"premium": False}))
        for m in r.get('members_info', []):
            m.update(pm.get(m['id'], {"premium": False}))
    arch = {r[0] for r in cur.execute("SELECT peer FROM chat_archive WHERE user_id = ?", (uid,)).fetchall()}
    for r in result:
        if not r.get('is_notes') and (str(r['id']) if r.get('is_group') else str(r.get('real_id'))) in arch:
            r['archived'] = True
            r['is_pinned'] = False
    # закреплённые (бот, заметки) остаются сверху; остальные — по последнему сообщению, новые выше
    pinned = [r for r in result if r.get('is_pinned')]
    rest = sorted((r for r in result if not r.get('is_pinned')), key=lambda r: r.pop('_lid', 0), reverse=True)
    result = pinned + rest
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


MSG_SELECT = ("SELECT m.id, m.sender_id, m.receiver_id, m.text, m.timestamp, m.is_read, m.reply_to, m.is_edited, m.voice_dur, m.fwd_from, m.expire_at, m.btn, "
              "u.display_name AS sender_name, u.username AS sender_username, u.avatar_v AS sender_av, m.invite_id, i.status AS invite_status, "
              "f.kind AS file_kind, f.name AS file_name, f.size AS file_size, f.has_cap AS file_cap "
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
    pm = prem_info(cur, [m['sender_id'] for m in msgs])
    for m in msgs:
        m['sender_avatar'] = av_url(m['sender_id'], m.pop('sender_av', 0))
        x = pm.get(m['sender_id'])
        if x:
            m['sender_premium'], m['sender_color'], m['sender_status'], m['sender_public_id'] = x['premium'], x['nick_color'], x['status_emoji'], x['public_id']
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
    seen = cur.execute("SELECT last_seen, is_bot, hide_online, premium, premium_until FROM users WHERE id = ?", (pid,)).fetchone()
    ago = int(max(0, time.time() - seen['last_seen'])) if seen and seen['last_seen'] and not seen['is_bot'] else None
    if ago is not None and pid != uid and seen['hide_online'] and prem_active(seen):
        ago = HIDDEN_AGO
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
    return jsonify({"status": "ok"})


def deliver(cur, uid, rid, text, t, reply_to, voice_dur=None, fwd=None, expires=True):
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
        ttl = get_ttl(cur, uid, rid) if expires else 0
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
        ttl = get_ttl(cur, uid, rid_int) if (rid_int != ADMIN_BOT_ID and expires) else 0
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
    cap = (request.form.get('text') or '').strip()[:4000]
    text = cap or {'photo': 'Фото', 'video': 'Видео'}.get(kind, name)
    msg_id, e = deliver(cur, uid, rid, text, request.form.get('timestamp', '00:00'), to_int(request.form.get('reply_to')), expires=False)
    if e:
        conn.close()
        os.remove(path)
        return e
    cur.execute("INSERT INTO files (msg_id, path, name, mime, size, kind, has_cap) VALUES (?, ?, ?, ?, ?, ?, ?)", (msg_id, stored, name, mime, size, kind, 1 if cap else 0))
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
    check_reports(cur)
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
    check_reports(cur)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ───────────────────────── статус, удаление, очистка, таймер ─────────────────────────

@app.route('/api/ping', methods=['GET', 'POST'])
def ping():
    uid = request.args.get('user_id', type=int) or to_int(body().get('user_id'))
    did = (request.args.get('did') or body().get('device_id') or '')[:64]
    if uid and did:
        c = get_db()
        try:
            ok = dev_touch(c.cursor(), uid, did)
            c.commit()
        finally:
            c.close()
        if not ok:
            return jsonify({"status": "revoked", "revoked": True, "message": "Сеанс завершён"}), 401
    touch(uid)
    return jsonify({"status": "ok"})


@app.route('/api/user/<int:tid>')
def user_info(tid):
    conn = get_db()
    r = conn.execute("SELECT id, username, display_name, avatar_emoji, avatar_v, is_bot, last_seen, deleted, hide_online, premium, premium_until, nick_color, status_emoji, public_id, bio, role FROM users WHERE id = ?", (tid,)).fetchone()
    conn.close()
    if not r:
        return err("Пользователь не найден!", 404)
    ago = None if (r['is_bot'] or r['deleted'] or not r['last_seen']) else int(max(0, time.time() - r['last_seen']))
    pa = prem_active(r)
    if ago is not None and pa and r['hide_online']:
        ago = HIDDEN_AGO
    return jsonify({"id": r['id'], "username": r['username'], "display_name": r['display_name'], "is_bot": r['is_bot'],
                    "avatar_url": av_url(r['id'], r['avatar_v']), "ago": ago, "premium": pa, "public_id": r['public_id'], "bio": r['bio'], "role": 'admin' if r['id'] == ADMIN_BOT_ID else r['role'],
                    "nick_color": r['nick_color'] if pa else None, "status_emoji": r['status_emoji'] if pa else None})


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
    check_reports(cur)
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
    check_reports(cur)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


def ttl_label(t):
    if not t: return 'выключено'
    for n, u in ((86400, 'д'), (3600, 'ч'), (60, 'мин'), (1, 'с')):
        if t % n == 0 or n == 1:
            return f"через {t // n} {u}"


@app.route('/api/chat/ttl', methods=['POST'])
def set_chat_ttl():
    """Автоудаление новых сообщений: в личке может менять любой из двоих, в группе — создатель."""
    d = body()
    uid, peer, ttl = to_int(d.get('user_id')), str(d.get('peer') or ''), to_int(d.get('ttl')) or 0
    if not uid or not peer or ttl < 0 or (ttl and ttl < 5) or ttl > 315360000:
        return err("Некорректный таймер!")
    conn = get_db()
    cur = conn.cursor()
    if peer.startswith('g_'):
        g = cur.execute("SELECT owner_id FROM groups WHERE id = ?", (to_int(peer[2:]),)).fetchone()
        if not g or g['owner_id'] != uid:
            conn.close()
            return err("Таймер удаления меняет только создатель!", 403)
        cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read) VALUES (1, ?, ?, ?, 1)",
                    (peer, f"Автоудаление сообщений: {ttl_label(ttl)}.", d.get('timestamp', '00:00')))
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
        f = cur.execute("SELECT path, name, mime, size, kind, has_cap FROM files WHERE msg_id = ?", (mid,)).fetchone()
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
            cur.execute("INSERT INTO files (msg_id, path, name, mime, size, kind, has_cap) VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id, stored, f['name'], f['mime'], f['size'], f['kind'], f['has_cap'] or 0))
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


# ───────────────────────── Telegram-админка ─────────────────────────
# Бот работает только для TG_ADMIN. На хостинге с публичным адресом (Render задаёт RENDER_EXTERNAL_URL) ставится webhook,
# локально — long polling в фоне.
TG_TOKEN = (os.environ.get('TG_BOT_TOKEN') or '').strip()   # токен админ-бота — только из переменной окружения
REPORT_TOKEN = (os.environ.get('REPORT_BOT_TOKEN') or '').strip()   # токен бота жалоб
REPORT_API = 'https://api.telegram.org/bot' + REPORT_TOKEN
TG_ADMIN = int((os.environ.get('TG_ADMIN_ID') or '0').strip() or 0)   # Telegram ID админа — только из переменной окружения TG_ADMIN_ID
TG_SECRET = hashlib.sha256(('extera-tg:' + TG_TOKEN).encode()).hexdigest()[:40]
TG_API = 'https://api.telegram.org/bot' + TG_TOKEN
_tg_last = [0]
TG_HELP = ("Админ-панель Extera\n\n"
           "/stats — статистика\n"
           "/user <id|@ник> — карточка\n"
           "/premium <id|@ник> [дней] — выдать премиум (без срока — навсегда)\n"
           "/unpremium <id|@ник> — снять премиум\n"
           "/delete <id|@ник> — удалить аккаунт\n"
           "/username <id|@ник> <новый ник> — выдать юзернейм\n"
           "/pubid <id|@ник> <число|0> — уникальный ID для профиля (0 — убрать)\n"
           "/role <id|@ник> <dev|tester|user> — роль в профиле (user — убрать)\n"
           "/warn <id|@ник> <причина> — выдать предупреждение\n"
           "/unwarn <id|@ник> [номер|all] — снять предупреждение (по умолчанию последнее)\n"
           "/warns <id|@ник> — предупреждения аккаунта и устройства\n"
           "/unbandev <id|@ник> — снять бан устройства и обнулить его счётчик\n"
           "/reporters — кто получает жалобы\n"
           "/addreporter <telegram id> — добавить получателя жалоб\n"
           "/delreporter <telegram id> — убрать получателя\n"
           "/dump — выгрузить базу данных")
TG_MENU = {"inline_keyboard": [[{"text": "Статистика", "callback_data": "/stats"}, {"text": "Выгрузить базу", "callback_data": "/dump"}]]}


def tg_call(method, **data):
    try:
        return requests.post('%s/%s' % (TG_API, method), json=data, timeout=35).json()
    except Exception as e:
        print('telegram:', method, e)
        return {}


def tg_send(text, markup=None):
    d = {'chat_id': TG_ADMIN, 'text': str(text)[:4000]}
    if markup:
        d['reply_markup'] = markup
    tg_call('sendMessage', **d)


def tg_find(cur, ref):
    ref = (ref or '').strip()
    if not ref:
        return None
    cols = "id, username, display_name, public_id, premium, premium_until, last_seen, deleted, is_bot, creator_did, creator_ip"
    if ref.isdigit():
        n = int(ref)
        return (cur.execute("SELECT %s FROM users WHERE id = ?" % cols, (n,)).fetchone()
                or cur.execute("SELECT %s FROM users WHERE public_id = ?" % cols, (n,)).fetchone())
    return (cur.execute("SELECT %s FROM users WHERE lower(username) = lower(?)" % cols, (ref.lstrip('@'),)).fetchone()
            or (not ref.startswith('@') and cur.execute("SELECT %s FROM users WHERE lower(public_id) = lower(?)" % cols, (ref,)).fetchone()))


def tg_card(r):
    now = time.time()
    if not r['last_seen']:
        seen = 'никогда'
    elif now - r['last_seen'] < 35:
        seen = 'в сети'
    else:
        seen = datetime.datetime.fromtimestamp(r['last_seen'], datetime.timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    if prem_active(r):
        prem = 'до ' + datetime.datetime.fromtimestamp(r['premium_until'], datetime.timezone.utc).strftime('%d.%m.%Y') if r['premium_until'] else 'навсегда'
    else:
        prem = 'нет'
    return ("ID: %s\nЮзернейм: @%s\nИмя: %s\nУникальный ID: %s\nПремиум: %s\nБыл в сети: %s%s"
            % (r['id'], r['username'], r['display_name'], r['public_id'] or '—', prem, seen, '\nАККАУНТ УДАЛЁН' if r['deleted'] else ''))


def tg_action(text):
    parts = text.split()
    if not parts:
        return
    cmd, args = parts[0].split('@')[0].lower(), parts[1:]
    if cmd in ('/start', '/help'):
        return tg_send(TG_HELP, TG_MENU)
    if cmd == '/cancel':
        return tg_send('Отменено')
    if cmd == '/dump':
        return tg_dump()
    conn = get_db()
    cur = conn.cursor()
    try:
        if cmd in ('/reporters', '/addreporter', '/delreporter'):
            if cmd == '/reporters':
                ids = [r[0] for r in cur.execute("SELECT tg_id FROM report_admins ORDER BY tg_id").fetchall()]
                return tg_send('Получатели жалоб:\n%s (главный)\n%s' % (TG_ADMIN, '\n'.join(map(str, ids))))
            if not args or not args[0].lstrip('-').isdigit():
                return tg_send('Формат: %s <telegram id>' % cmd)
            n = int(args[0])
            if cmd == '/addreporter':
                cur.execute("INSERT OR IGNORE INTO report_admins (tg_id) VALUES (?)", (n,))
                tg_send('Добавлен: %d. Этот человек должен один раз нажать /start в боте жалоб, иначе Telegram не даст боту писать ему.' % n)
            else:
                if n == TG_ADMIN:
                    return tg_send('Главного админа убрать нельзя.')
                cur.execute("DELETE FROM report_admins WHERE tg_id = ?", (n,))
                tg_send('Убран: %d' % n)
            return conn.commit()
        if cmd == '/stats':
            now = time.time()
            q = lambda sql, *p: cur.execute(sql, p).fetchone()[0]
            return tg_send("Пользователей: %d\nВ сети сейчас: %d\nЗа 24 часа: %d\nС премиумом: %d\nСообщений: %d\nГрупп: %d\nКаналов: %d" % (
                q("SELECT COUNT(*) FROM users WHERE deleted = 0 AND is_bot = 0"),
                q("SELECT COUNT(*) FROM users WHERE deleted = 0 AND is_bot = 0 AND last_seen > ?", now - 35),
                q("SELECT COUNT(*) FROM users WHERE deleted = 0 AND is_bot = 0 AND last_seen > ?", now - 86400),
                q("SELECT COUNT(*) FROM users WHERE deleted = 0 AND premium = 1 AND (premium_until IS NULL OR premium_until > ?)", now),
                q("SELECT COUNT(*) FROM messages"),
                q("SELECT COUNT(*) FROM groups WHERE is_channel = 0"),
                q("SELECT COUNT(*) FROM groups WHERE is_channel = 1")))
        if cmd in MOD_CMDS:
            return tg_send(mod_cmd(conn, cmd, args))
        if cmd not in ('/user', '/premium', '/unpremium', '/delete', '/delok', '/username', '/pubid', '/role'):
            return tg_send('Неизвестная команда. /help')
        r = tg_find(cur, args[0] if args else '')
        if not r or r['is_bot']:
            return tg_send('Пользователь не найден. Формат: /команда <id|@ник>')
        uid = r['id']
        if cmd == '/user':
            return tg_send(tg_card(r))
        if r['deleted']:
            return tg_send('Аккаунт уже удалён.\n\n' + tg_card(r))
        if cmd == '/premium':
            days = int(args[1]) if len(args) > 1 and args[1].isdigit() and int(args[1]) > 0 else None
            set_premium(cur, uid, True, days)
            tg_send('Премиум выдан (%s): @%s' % ('на %d дн.' % days if days else 'навсегда', r['username']))
        elif cmd == '/unpremium':
            set_premium(cur, uid, False)
            tg_send('Премиум снят: @%s' % r['username'])
        elif cmd == '/delete':
            tg_send('Удалить аккаунт @%s (ID %s, %s)? Это необратимо.' % (r['username'], uid, r['display_name']),
                    {"inline_keyboard": [[{"text": "Да, удалить", "callback_data": "/delok %d" % uid}, {"text": "Отмена", "callback_data": "/cancel"}]]})
        elif cmd == '/delok':
            if uid == ADMIN_BOT_ID:
                return tg_send('Системного бота удалить нельзя.')
            do_delete_account(cur, uid)
            tg_send('Аккаунт удалён: @%s (ID %s)' % (r['username'], uid))
        elif cmd == '/username':
            new = (args[1] if len(args) > 1 else '').lstrip('@')
            if not re.fullmatch(r'[A-Za-z0-9_]{3,32}', new):
                return tg_send('Юзернейм: 3-32 символа, латиница, цифры и _. Формат: /username <id|@ник> <новый>')
            if cur.execute("SELECT 1 FROM users WHERE lower(username) = lower(?) AND id != ?", (new, uid)).fetchone() or \
                    cur.execute("SELECT 1 FROM groups WHERE lower(username) = lower(?)", (new,)).fetchone():
                return tg_send('Юзернейм @%s уже занят.' % new)
            cur.execute("UPDATE users SET username = ? WHERE id = ?", (new, uid))
            tg_send('Готово: @%s -> @%s. Войти теперь можно только с новым юзернеймом.' % (r['username'], new))
        elif cmd == '/pubid':
            v = args[1] if len(args) > 1 else ''
            if v != '0' and not re.fullmatch(r'[A-Za-z0-9_]{1,20}', v):
                return tg_send('Формат: /pubid <id|@ник> <цифры/буквы/_ (до 20)|0>')
            n = 0 if v == '0' else (int(v) if v.isdigit() and len(v) <= 9 else v)
            if n == 0:
                cur.execute("UPDATE users SET public_id = NULL WHERE id = ?", (uid,))
                tg_send('Уникальный ID убран у @%s' % r['username'])
            else:
                if cur.execute("SELECT 1 FROM users WHERE (id = ? AND id != ?) OR (lower(public_id) = lower(?) AND id != ?)", (n, uid, n, uid)).fetchone():
                    return tg_send('ID %s уже занят.' % n)
                cur.execute("UPDATE users SET public_id = ? WHERE id = ?", (n, uid))
                tg_send('Уникальный ID %s выдан @%s' % (n, r['username']))
        elif cmd == '/role':
            v = (args[1] if len(args) > 1 else '').lower()
            m = {'dev': 'dev', 'developer': 'dev', 'tester': 'tester', 'test': 'tester', 'user': None}
            if v not in m:
                return tg_send('Формат: /role <id|@ник> <dev|tester|user>')
            cur.execute("UPDATE users SET role = ? WHERE id = ?", (m[v], uid))
            tg_send('Роль у @%s: %s' % (r['username'], m[v] or 'пользователь'))
        conn.commit()
    finally:
        conn.close()


def tg_dump():
    """Копия всей базы одним .sqlite-файлом (Turso → локальный файл → документ в Telegram)."""
    tg_send('Собираю базу…')
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(fd)
        conn = get_db()
        cur = conn.cursor()
        out = sqlite3.connect(path)
        for t in cur.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL").fetchall():
            out.execute(t['sql'])
            last = -1
            while True:   # порциями, чтобы не держать в памяти всю таблицу (аватарки и голосовые тяжёлые)
                rows = cur.execute('SELECT rowid AS _rid, * FROM "%s" WHERE rowid > ? ORDER BY rowid LIMIT 300' % t['name'], (last,)).fetchall()
                if not rows:
                    break
                out.executemany('INSERT INTO "%s" VALUES (%s)' % (t['name'], ','.join('?' * (len(rows[0]) - 1))), [tuple(r)[1:] for r in rows])
                last = rows[-1][0]
        for i in cur.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL").fetchall():
            out.execute(i['sql'])
        out.commit()
        out.close()
        conn.close()
        name = 'extera_%s.sqlite' % datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M')
        if os.path.getsize(path) > 45 * 1024 * 1024:
            gz = path + '.gz'
            with open(path, 'rb') as f, gzip.open(gz, 'wb') as g:
                shutil.copyfileobj(f, g)
            os.remove(path)
            path, name = gz, name + '.gz'
        if os.path.getsize(path) > 49 * 1024 * 1024:
            return tg_send('База больше 50 МБ даже в архиве — Telegram столько не принимает.')
        with open(path, 'rb') as f:
            requests.post(TG_API + '/sendDocument', data={'chat_id': TG_ADMIN}, files={'document': (name, f)}, timeout=180)
    except Exception as e:
        tg_send('Не удалось выгрузить базу: %s' % e)
    finally:
        for p in (path, (path or '') + '.gz'):
            if p and os.path.exists(p):
                os.remove(p)


def tg_handle(u):
    cb = u.get('callback_query')
    src = cb or u.get('message') or {}
    if not TG_ADMIN or (src.get('from') or {}).get('id') != TG_ADMIN:   # всем остальным бот молчит
        return
    try:
        if cb:
            tg_call('answerCallbackQuery', callback_query_id=cb['id'])
            tg_action(cb.get('data') or '')
        else:
            tg_action((u['message'].get('text') or '').strip())
    except Exception as e:
        tg_send('Ошибка: %s' % e)


def tg_dispatch(u):
    n = u.get('update_id', 0)
    if n and n <= _tg_last[0]:   # повторная доставка
        return
    _tg_last[0] = max(_tg_last[0], n)
    threading.Thread(target=tg_handle, args=(u,), daemon=True).start()


@app.route('/tg/<secret>', methods=['POST'])
def tg_webhook(secret):
    if not hmac.compare_digest(secret, TG_SECRET) or request.headers.get('X-Telegram-Bot-Api-Secret-Token') != TG_SECRET:
        return '', 404
    tg_dispatch(request.get_json(silent=True) or {})
    return 'ok'


def tg_start():
    if not TG_TOKEN:
        return
    tg_call('setMyCommands', commands=[{"command": "start", "description": "Меню"}, {"command": "stats", "description": "Статистика"},
                                       {"command": "dump", "description": "Выгрузить базу"}, {"command": "help", "description": "Все команды"}])
    base = (os.environ.get('PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
    if base:
        r = tg_call('setWebhook', url='%s/tg/%s' % (base, TG_SECRET), secret_token=TG_SECRET, allowed_updates=['message', 'callback_query'])
        print('telegram webhook:', r.get('ok'), r.get('description', ''))
        return
    tg_call('deleteWebhook')
    off = 0
    while True:   # локальная разработка: long polling
        r = tg_call('getUpdates', offset=off, timeout=25, allowed_updates=['message', 'callback_query'])
        for u in r.get('result', []):
            off = u['update_id'] + 1
            tg_dispatch(u)
        if not r.get('ok'):
            time.sleep(5)


# ───────────────────────── жалобы ─────────────────────────
# Жалобы уходят отдельным ботом (REPORT_BOT_TOKEN) главному админу и тем, кого он добавил через админ-бота (/addreporter).
REPORT_REASONS = ['Спам', 'Оскорбления или травля', 'Мошенничество', 'Угрозы или насилие', 'Материалы 18+', 'Другое']
MAX_SHOT = 8 * 1024 * 1024


def rtg(method, files=None, **data):
    try:
        return requests.post('%s/%s' % (REPORT_API, method), data=data, files=files, timeout=60).json()
    except Exception as e:
        print('report bot:', method, e)
        return {}


def conv_key(m):
    rid = str(m['receiver_id'])
    return rid if rid.startswith('g_') else '%d_%s' % (min(m['sender_id'], int(rid)), max(m['sender_id'], int(rid)))


def report_recipients(cur):
    return ([TG_ADMIN] if TG_ADMIN else []) + [r[0] for r in cur.execute("SELECT tg_id FROM report_admins").fetchall() if r[0] != TG_ADMIN]


def tg_report_send(rid, photo, caption, transcript):
    conn = get_db()
    try:
        cur = conn.cursor()
        for chat in report_recipients(cur):
            kb = json.dumps({"inline_keyboard": [[{"text": "✅ Принять", "callback_data": "r:%d:ok" % rid}, {"text": "❌ Отклонить", "callback_data": "r:%d:no" % rid}]]})
            r = rtg('sendPhoto', files={'photo': ('screenshot.jpg', photo)}, chat_id=chat, caption=caption[:1000], reply_markup=kb)
            mid = (r.get('result') or {}).get('message_id')
            if mid:
                cur.execute("INSERT INTO report_tg (report_id, chat_id, tg_msg) VALUES (?, ?, ?)", (rid, chat, mid))
                r2 = rtg('sendMessage', chat_id=chat, text=(transcript + '\n\n↩️ Ответьте (Reply) на это сообщение — текст уйдёт автору жалобы.')[:4000], reply_to_message_id=mid)
                m2 = (r2.get('result') or {}).get('message_id')
                if m2:
                    cur.execute("INSERT INTO report_tg (report_id, chat_id, tg_msg) VALUES (?, ?, ?)", (rid, chat, m2))
        conn.commit()
    finally:
        conn.close()


def tg_report_cancel(items):
    for rid, chats in items:
        for chat, mid in chats:
            rtg('sendMessage', chat_id=chat, text='❌ Жалоба #%d отменена: автор изменил или удалил своё сообщение из контекста.' % rid, reply_to_message_id=mid)


def check_reports(cur):
    """Если жалобщик изменил/удалил своё сообщение из контекста жалобы — жалоба отменяется."""
    rows = cur.execute("SELECT c.report_id, c.text, c.is_edited, m.text AS mt, m.is_edited AS me, m.id AS mid FROM report_ctx c JOIN reports r ON r.id = c.report_id "
                       "LEFT JOIN messages m ON m.id = c.msg_id WHERE r.status = 'sent' AND r.created > ?", (time.time() - 7 * 86400,)).fetchall()
    bad = sorted({r['report_id'] for r in rows if r['mid'] is None or r['mt'] != r['text'] or (r['me'] or 0) != (r['is_edited'] or 0)})
    if not bad:
        return
    items = []
    for rid in bad:
        cur.execute("UPDATE reports SET status = 'cancelled' WHERE id = ?", (rid,))
        items.append((rid, [(x['chat_id'], x['tg_msg']) for x in cur.execute("SELECT chat_id, tg_msg FROM report_tg WHERE report_id = ?", (rid,)).fetchall()]))
    threading.Thread(target=tg_report_cancel, args=(items,), daemon=True).start()


@app.route('/api/report', methods=['POST'])
def report():
    if not REPORT_TOKEN:
        return err("Жалобы временно недоступны.", 503)
    uid, mid = to_int(request.form.get('user_id')), to_int(request.form.get('msg_id'))
    reason, details = (request.form.get('reason') or '').strip(), (request.form.get('details') or '').strip()[:500]
    shot = request.files.get('screenshot')
    ctx_ids = sorted({i for i in (to_int(x) for x in (request.form.get('ctx') or '').split(',')[:40]) if i} | {mid or 0} - {0})
    if not uid or not mid:
        return err("Некорректная жалоба!")
    if reason not in REPORT_REASONS:
        return err("Выберите причину жалобы!")
    if not shot:
        return err("Приложите скриншот с предыдущими сообщениями!")
    raw = shot.read(MAX_SHOT + 1)
    if not raw or len(raw) > MAX_SHOT:
        return err("Скриншот должен быть картинкой до 8 МБ!")
    if not (raw[:3] == b'\xff\xd8\xff' or raw[:8] == b'\x89PNG\r\n\x1a\n' or (raw[:4] == b'RIFF' and raw[8:12] == b'WEBP')):
        return err("Скриншот должен быть в формате JPG, PNG или WEBP!")
    conn = get_db()
    cur = conn.cursor()
    try:
        m = cur.execute("SELECT id, sender_id, receiver_id, text FROM messages WHERE id = ?", (mid,)).fetchone()
        if not m or not can_access(cur, uid, m):
            return err("Сообщение не найдено!", 404)
        if m['sender_id'] == uid or m['sender_id'] == ADMIN_BOT_ID:
            return err("Пожаловаться на это сообщение нельзя!")
        if cur.execute("SELECT 1 FROM reports WHERE reporter_id = ? AND msg_id = ? AND status = 'sent'", (uid, mid)).fetchone():
            return err("Вы уже отправили жалобу на это сообщение.")
        if cur.execute("SELECT COUNT(*) FROM reports WHERE reporter_id = ? AND created > ?", (uid, time.time() - 3600)).fetchone()[0] >= 5:
            return err("Слишком много жалоб. Попробуйте позже.", 429)
        key = conv_key(m)
        found = {r['id']: r for r in cur.execute("SELECT m.id, m.sender_id, m.receiver_id, m.text, m.is_edited, u.display_name AS sname FROM messages m LEFT JOIN users u ON u.id = m.sender_id WHERE m.id IN (%s)" % ','.join('?' * len(ctx_ids)), ctx_ids).fetchall()}
        ctx = []
        for i in ctx_ids:
            r = found.get(i)
            if r is None:   # сообщение из контекста уже удалено
                return err("Жалоба отменена: в контексте есть удалённые сообщения. Обновите чат и повторите.")
            if conv_key(r) != key or not can_access(cur, uid, r):
                return err("Некорректный контекст жалобы!")
            if r['sender_id'] == uid and r['is_edited']:
                return err("Жалоба отменена: ваше сообщение в контексте было изменено.")
            ctx.append(r)
        t = cur.execute("SELECT username, display_name FROM users WHERE id = ?", (m['sender_id'],)).fetchone()
        me = cur.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
        cur.execute("INSERT INTO reports (reporter_id, target_id, msg_id, reason, details, created) VALUES (?, ?, ?, ?, ?, ?)", (uid, m['sender_id'], mid, reason, details, time.time()))
        rid = cur.lastrowid
        for r in ctx:
            if r['sender_id'] == uid:
                cur.execute("INSERT OR IGNORE INTO report_ctx (report_id, msg_id, text, is_edited) VALUES (?, ?, ?, ?)", (rid, r['id'], r['text'], r['is_edited'] or 0))
        conn.commit()
        where = ('группа/канал ' + str(m['receiver_id'])) if str(m['receiver_id']).startswith('g_') else 'личная переписка'
        caption = ("Жалоба #%d\nПричина: %s\nОт: @%s (ID %s)\nНа: @%s (ID %s), %s\nСообщение #%s: %s%s"
                   % (rid, reason, me['username'] if me else '?', uid, t['username'] if t else '?', m['sender_id'], where, mid, (m['text'] or '')[:200],
                      ('\nДополнение: ' + details) if details else ''))
        transcript = 'Контекст жалобы #%d:\n' % rid + '\n'.join('%s: %s' % (r['sname'] or r['sender_id'], (r['text'] or '')[:150]) for r in ctx[-12:])
    finally:
        conn.close()
    threading.Thread(target=tg_report_send, args=(rid, raw, caption, transcript), daemon=True).start()
    return jsonify({"status": "ok"})



# ───────────────────────── устройства (сеансы) ─────────────────────────
def ua_name(ua):
    ua = ua or ''
    os_ = 'Android' if 'Android' in ua else 'iPhone' if 'iPhone' in ua else 'iPad' if 'iPad' in ua else 'Windows' if 'Windows' in ua else 'Mac' if 'Mac OS' in ua else 'Linux' if 'Linux' in ua else 'Устройство'
    br = 'Edge' if 'Edg/' in ua else 'Opera' if 'OPR/' in ua else 'Firefox' if 'Firefox' in ua else 'Chrome' if ('Chrome' in ua or 'CriOS' in ua) else 'Safari' if 'Safari' in ua else 'Браузер'
    return '%s · %s' % (os_, br)


def dev_touch(cur, uid, did, fresh=False):
    """Обновляет «был активен» у устройства. False — сеанс этого устройства завершён."""
    now = time.time()
    row = cur.execute("SELECT revoked FROM devices WHERE user_id = ? AND did = ?", (uid, did)).fetchone()
    if row:
        if row['revoked'] and not fresh:
            return False
        cur.execute("UPDATE devices SET last_seen = ?, revoked = 0 WHERE user_id = ? AND did = ?", (now, uid, did))
    else:
        cur.execute("INSERT INTO devices (user_id, did, name, created, last_seen) VALUES (?, ?, ?, ?, ?)", (uid, did, ua_name(request.headers.get('User-Agent')), now, now))
    return True


def dev_register(uid, did):
    did = str(did or '')[:64]
    if not uid or not did:
        return
    c = get_db()
    try:
        dev_touch(c.cursor(), uid, did, fresh=True)   # вход по паролю возвращает устройство в строй
        c.commit()
    finally:
        c.close()


@app.route('/api/devices')
def devices_list():
    uid, did = request.args.get('user_id', type=int), (request.args.get('did') or '')[:64]
    if not uid:
        return jsonify([]), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM devices WHERE revoked = 1 AND last_seen < ?", (time.time() - 30 * 86400,))
    if did:
        dev_touch(cur, uid, did)
    conn.commit()
    rows = cur.execute("SELECT id, did, name, created, last_seen FROM devices WHERE user_id = ? AND revoked = 0 ORDER BY last_seen DESC", (uid,)).fetchall()
    conn.close()
    now = time.time()
    return jsonify([{"id": r['id'], "name": r['name'] or 'Устройство', "current": r['did'] == did, "ago": int(max(0, now - (r['last_seen'] or now))), "created": r['created']} for r in rows])


@app.route('/api/devices/revoke', methods=['POST'])
def devices_revoke():
    d = body()
    uid, did = to_int(d.get('user_id')), str(d.get('device_id') or '')[:64]
    if not uid or not did:
        return err("Некорректный запрос!")
    conn = get_db()
    cur = conn.cursor()
    if d.get('all_others'):
        cur.execute("UPDATE devices SET revoked = 1 WHERE user_id = ? AND did != ? AND revoked = 0", (uid, did))
    else:
        for i in [to_int(x) for x in (d.get('ids') or [])[:200]]:
            if i:
                cur.execute("UPDATE devices SET revoked = 1 WHERE id = ? AND user_id = ? AND did != ?", (i, uid, did))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ───────────────────────── архив чатов ─────────────────────────
@app.route('/api/chat/archive', methods=['POST'])
def chat_archive():
    d = body()
    uid, peer = to_int(d.get('user_id')), str(d.get('peer') or '')[:40]
    if not uid or not peer:
        return err("Некорректный запрос!")
    conn = get_db()
    if d.get('on'):
        conn.execute("INSERT OR IGNORE INTO chat_archive (user_id, peer) VALUES (?, ?)", (uid, peer))
    else:
        conn.execute("DELETE FROM chat_archive WHERE user_id = ? AND peer = ?", (uid, peer))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ───────────────────────── ответы на жалобы (бот жалоб принимает сообщения) ─────────────────────────
RTG_SECRET = hashlib.sha256(('extera-rtg:' + REPORT_TOKEN).encode()).hexdigest()[:40]
_rtg_last = [0]
RTG_HELP = ("Бот жалоб Extera\n\nОтветьте (Reply) на сообщение жалобы — текст уйдёт автору жалобы в чат exteraAdmin.\n"
            "«Принять» выдаёт нарушителю предупреждение (3 предупреждения — аккаунт удаляется автоматически).\n"
            "Команды: /warn <id|@ник> <причина>, /unwarn <id|@ник> [номер|all], /warns <id|@ник>, /unbandev <id|@ник>\n"
            "Кнопки под жалобой: «Принять» — нарушение подтверждено, «Отклонить» — нарушений нет (автор получит уведомление).")


def hhmm():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).strftime('%H:%M')


# ───────────────────────── предупреждения и блокировки ─────────────────────────
# 3 предупреждения у аккаунта → аккаунт удаляется автоматически (без обжалования), устройству, на котором он был создан, +1 «предупреждение».
# 3 таких удаления с одного устройства → устройству нельзя создавать новые аккаунты. Остальные аккаунты на устройстве не трогаем.
MOD_CMDS = ('/warn', '/unwarn', '/warns', '/unbandev')


def device_banned(cur, did, ip):
    if did and cur.execute("SELECT 1 FROM banned_devices WHERE dkey = ?", (did,)).fetchone():
        return True
    return bool(ip and cur.execute("SELECT 1 FROM banned_devices WHERE ip = ?", (ip,)).fetchone())


def dev_key(did, ip):
    return did or (('ip:' + ip) if ip else None)


def device_strike(cur, uid):
    """+1 предупреждение устройству, на котором создан аккаунт. Возвращает (сколько_теперь, забанено_ли)."""
    r = cur.execute("SELECT creator_did, creator_ip FROM users WHERE id = ?", (uid,)).fetchone()
    key = dev_key(r['creator_did'], r['creator_ip']) if r else None
    if not key:
        return 0, False
    cur.execute("INSERT INTO device_warns (dkey, ip, n, updated) VALUES (?, ?, 1, ?) ON CONFLICT(dkey) DO UPDATE SET n = n + 1, ip = excluded.ip, updated = excluded.updated",
                (key, r['creator_ip'], time.time()))
    n = cur.execute("SELECT n FROM device_warns WHERE dkey = ?", (key,)).fetchone()['n']
    if n >= DEV_MAX_WARNS:
        cur.execute("INSERT OR REPLACE INTO banned_devices (dkey, ip, created) VALUES (?, ?, ?)", (key, r['creator_ip'], time.time()))
    return n, n >= DEV_MAX_WARNS


def warn_count(cur, uid):
    return cur.execute("SELECT COUNT(*) FROM warns WHERE user_id = ?", (uid,)).fetchone()[0]


def warn_text(n, reason):
    t = "⚠️ Вам выдано предупреждение\n\nПричина: %s\nПредупреждений: %d из %d\n\n" % (reason, n, MAX_WARNS)
    if n == MAX_WARNS - 1:
        t += "Это последнее предупреждение: следующее приведёт к удалению аккаунта.\n\n"
    t += ("Памятка: если вы продолжите нарушать правила, это приведёт к блокировке аккаунта. При %d предупреждениях аккаунт удаляется автоматически, "
          "без возможности обжалования. Предупреждения сами не пропадают, снять их может только модератор.\n\n"
          "Если считаете, что предупреждение выдано ошибочно, нажмите «Обжаловать»." % MAX_WARNS)
    return t


def give_warn(cur, uid, reason, report_id=None, by='report'):
    """Выдаёт предупреждение. None — выдать некому (аккаунт удалён/бот); иначе словарь с итогом."""
    u = cur.execute("SELECT deleted, is_bot FROM users WHERE id = ?", (uid,)).fetchone()
    if not u or u['deleted'] or u['is_bot'] or uid == ADMIN_BOT_ID:
        return None
    cur.execute("INSERT INTO warns (user_id, reason, report_id, created, by_mod) VALUES (?, ?, ?, ?, ?)", (uid, reason, report_id, time.time(), by))
    n = warn_count(cur, uid)
    if n < MAX_WARNS:
        cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read, btn) VALUES (?, ?, ?, ?, 0, 'appeal')", (ADMIN_BOT_ID, uid, warn_text(n, reason), hhmm()))
        return {'n': n}
    dn, banned = device_strike(cur, uid)   # устройство берём до удаления (данные в строке остаются, но так надёжнее)
    do_delete_account(cur, uid)
    return {'n': n, 'deleted': True, 'dev_n': dn, 'dev_ban': banned}


def warn_line(w, uname=''):
    if w is None:
        return 'Аккаунт нарушителя уже удалён — предупреждение не выдано.'
    if w.get('deleted'):
        return 'Предупреждение %d/%d — аккаунт %sудалён автоматически. Предупреждений устройству: %d/%d%s.' % (
            w['n'], MAX_WARNS, uname, w['dev_n'], DEV_MAX_WARNS, ', устройство заблокировано' if w['dev_ban'] else '')
    return 'Нарушителю %sвыдано предупреждение %d/%d, ему отправлено сообщение.' % (uname, w['n'], MAX_WARNS)


def fmt_dt(ts):
    return datetime.datetime.fromtimestamp(ts or 0, datetime.timezone.utc).strftime('%d.%m.%Y %H:%M UTC')


def mod_cmd(conn, cmd, args):
    """Команды модератора (общие для админ-бота и бота жалоб). Возвращает текст ответа."""
    cur = conn.cursor()
    r = tg_find(cur, args[0] if args else '')
    if not r or r['is_bot']:
        return 'Пользователь не найден. Формат: %s <id|@ник>' % cmd
    uid = r['id']
    did, ip = r['creator_did'], r['creator_ip']
    key = dev_key(did, ip)
    who = '@%s (ID %s)' % (r['username'], uid)
    if cmd == '/warns':
        rows = cur.execute("SELECT id, reason, created, report_id, by_mod FROM warns WHERE user_id = ? ORDER BY id", (uid,)).fetchall()
        dw = cur.execute("SELECT n FROM device_warns WHERE dkey = ?", (key,)).fetchone() if key else None
        out = ['%s%s\nПредупреждения: %d/%d' % (who, ' — АККАУНТ УДАЛЁН' if r['deleted'] else '', len(rows), MAX_WARNS)]
        for w in rows:
            out.append('№%d · %s · %s (%s)' % (w['id'], fmt_dt(w['created']), w['reason'], ('жалоба #%s' % w['report_id']) if w['report_id'] else 'модератор'))
        out.append('Устройство: %d/%d%s' % (dw['n'] if dw else 0, DEV_MAX_WARNS, ' — ЗАБЛОКИРОВАНО' if device_banned(cur, did, ip) else ''))
        return '\n'.join(out)[:4000]
    if cmd == '/warn':
        reason = ' '.join(args[1:]).strip()[:200]
        if not reason:
            return 'Формат: /warn <id|@ник> <причина>'
        if r['deleted']:
            return 'Аккаунт уже удалён.'
        w = give_warn(cur, uid, reason, None, 'manual')
        conn.commit()
        return warn_line(w, '@%s ' % r['username'])
    if cmd == '/unwarn':
        if r['deleted']:
            return 'Аккаунт удалён автоматически за нарушения, обжалованию не подлежит.'
        what = (args[1] if len(args) > 1 else '').lower()
        if what == 'all':
            cur.execute("DELETE FROM warns WHERE user_id = ?", (uid,))
        else:
            if what.isdigit():
                row = cur.execute("SELECT id FROM warns WHERE id = ? AND user_id = ?", (int(what), uid)).fetchone()
            else:
                row = cur.execute("SELECT id FROM warns WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
            if not row:
                return 'У %s нет такого предупреждения.' % who
            cur.execute("DELETE FROM warns WHERE id = ?", (row['id'],))
        conn.commit()
        return 'Снято. У %s осталось предупреждений: %d/%d.' % (who, warn_count(cur, uid), MAX_WARNS)
    if cmd == '/unbandev':
        if not key:
            return 'Нет данных об устройстве этого аккаунта.'
        cur.execute("DELETE FROM banned_devices WHERE dkey = ?", (key,))
        if ip:
            cur.execute("DELETE FROM banned_devices WHERE ip = ?", (ip,))
        cur.execute("UPDATE device_warns SET n = 0 WHERE dkey = ?", (key,))
        conn.commit()
        return 'Устройство %s разблокировано, счётчик обнулён.' % who


def report_notify(cur, rid, text):
    r = cur.execute("SELECT reporter_id FROM reports WHERE id = ?", (rid,)).fetchone()
    if not r:
        return False
    cur.execute("INSERT INTO messages (sender_id, receiver_id, text, timestamp, is_read) VALUES (?, ?, ?, ?, 0)", (ADMIN_BOT_ID, r['reporter_id'], text, hhmm()))
    return True


def report_resolve(cur, rid, act):
    r = cur.execute("SELECT status, target_id, reason FROM reports WHERE id = ?", (rid,)).fetchone()
    if not r:
        return 'Жалоба #%d не найдена.' % rid
    if r['status'] == 'cancelled':
        return 'Жалоба #%d отменена автором.' % rid
    if r['status'] in ('accepted', 'rejected'):
        return 'Жалоба #%d уже рассмотрена.' % rid
    if act == 'ok':
        cur.execute("UPDATE reports SET status = 'accepted' WHERE id = ?", (rid,))
        w = give_warn(cur, r['target_id'], r['reason'], rid, 'report')
        report_notify(cur, rid, '✅ Ваша жалоба #%d рассмотрена: нарушение подтверждено, меры приняты. Спасибо!' % rid)
        return '✅ Жалоба #%d принята, автору отправлено уведомление.\n%s' % (rid, warn_line(w))
    cur.execute("UPDATE reports SET status = 'rejected' WHERE id = ?", (rid,))
    report_notify(cur, rid, '❌ Ваша жалоба #%d рассмотрена: нарушений не найдено.' % rid)
    return '❌ Жалоба #%d отклонена, автору отправлено уведомление.' % rid


def rtg_handle(u):
    cb = u.get('callback_query')
    src = cb or u.get('message') or {}
    who = (src.get('from') or {}).get('id')
    conn = get_db()
    cur = conn.cursor()
    try:
        if who not in report_recipients(cur):
            return
        if cb:
            rtg('answerCallbackQuery', callback_query_id=cb['id'])
            m = re.match(r'^r:(\d+):(ok|no)$', cb.get('data') or '')
            if not m:
                return
            rid = int(m.group(1))
            res = report_resolve(cur, rid, m.group(2))
            conn.commit()
            for x in cur.execute("SELECT chat_id, tg_msg FROM report_tg WHERE report_id = ?", (rid,)).fetchall():
                rtg('editMessageReplyMarkup', chat_id=x['chat_id'], message_id=x['tg_msg'], reply_markup=json.dumps({"inline_keyboard": []}))
            rtg('sendMessage', chat_id=who, text=res, reply_to_message_id=cb['message']['message_id'])
            return
        msg = u.get('message') or {}
        text = (msg.get('text') or '').strip()
        if text.startswith('/'):
            parts = text.split()
            c = parts[0].split('@')[0].lower()
            rtg('sendMessage', chat_id=who, text=(mod_cmd(conn, c, parts[1:]) if c in MOD_CMDS else RTG_HELP)[:4000])
            return
        rep = msg.get('reply_to_message')
        if not text or not rep:
            return
        row = cur.execute("SELECT report_id FROM report_tg WHERE chat_id = ? AND tg_msg = ?", (who, rep['message_id'])).fetchone()
        if not row:
            rtg('sendMessage', chat_id=who, text='Ответьте именно на сообщение жалобы (фото или контекст).', reply_to_message_id=msg['message_id'])
            return
        rid = row['report_id']
        st = cur.execute("SELECT status FROM reports WHERE id = ?", (rid,)).fetchone()
        if not st or st['status'] == 'cancelled':
            rtg('sendMessage', chat_id=who, text='Жалоба #%d отменена автором.' % rid, reply_to_message_id=msg['message_id'])
            return
        report_notify(cur, rid, '📩 Ответ на вашу жалобу #%d:\n%s' % (rid, text[:1000]))
        cur.execute("UPDATE reports SET status = 'answered' WHERE id = ? AND status = 'sent'", (rid,))
        conn.commit()
        rtg('sendMessage', chat_id=who, text='✅ Ответ на жалобу #%d отправлен автору.' % rid, reply_to_message_id=msg['message_id'])
    except Exception as e:
        print('report bot handle:', e)
    finally:
        conn.close()


def rtg_dispatch(u):
    n = u.get('update_id', 0)
    if n and n <= _rtg_last[0]:
        return
    _rtg_last[0] = max(_rtg_last[0], n)
    threading.Thread(target=rtg_handle, args=(u,), daemon=True).start()


@app.route('/rtg/<secret>', methods=['POST'])
def rtg_webhook(secret):
    if not REPORT_TOKEN or not hmac.compare_digest(secret, RTG_SECRET) or request.headers.get('X-Telegram-Bot-Api-Secret-Token') != RTG_SECRET:
        return '', 404
    rtg_dispatch(request.get_json(silent=True) or {})
    return 'ok'


def rtg_start():
    if not REPORT_TOKEN:
        return
    allowed = json.dumps(['message', 'callback_query'])
    base = (os.environ.get('PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
    if base:
        r = rtg('setWebhook', url='%s/rtg/%s' % (base, RTG_SECRET), secret_token=RTG_SECRET, allowed_updates=allowed)
        print('report webhook:', r.get('ok'), r.get('description', ''))
        return
    rtg('deleteWebhook')
    off = 0
    while True:   # локально: long polling
        r = rtg('getUpdates', offset=off, timeout=25, allowed_updates=allowed)
        for u in r.get('result', []):
            off = u['update_id'] + 1
            rtg_dispatch(u)
        if not r.get('ok'):
            time.sleep(5)


init_db()
threading.Thread(target=tg_start, daemon=True).start()
threading.Thread(target=rtg_start, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
