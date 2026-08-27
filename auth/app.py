import os, secrets, sqlite3, smtplib, logging, json
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from urllib.request import Request, urlopen
from urllib.error import URLError
from base64 import b64encode
from flask import Flask, request, jsonify, redirect, make_response, abort

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DB_PATH     = os.getenv('DB_PATH', '/data/auth.db')
BASE_URL    = os.getenv('BASE_URL', 'https://stream.example.com')
SMTP_HOST   = os.getenv('SMTP_HOST', 'localhost')
SMTP_PORT   = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER   = os.getenv('SMTP_USER', '')
SMTP_PASS   = os.getenv('SMTP_PASS', '')
SMTP_FROM   = os.getenv('SMTP_FROM', 'noreply@example.com')
SMTP_SSL    = os.getenv('SMTP_SSL', 'false').lower() == 'true'
SMTP_TLS    = os.getenv('SMTP_TLS', 'true').lower() == 'true'
LINK_TTL    = int(os.getenv('MAGIC_LINK_EXPIRE_MINUTES', '15'))
SESSION_TTL = int(os.getenv('SESSION_EXPIRE_DAYS', '7'))
WELCOME_TTL = int(os.getenv('WELCOME_LINK_EXPIRE_HOURS', '72')) * 60
STREAM_SECRET = os.getenv('STREAM_SECRET', '')
STREAM_SEP    = os.getenv('STREAM_SEPARATOR', '~')
OME_API_URL   = os.getenv('OME_API_URL', 'http://host-gateway:8081')
OME_API_TOKEN = os.getenv('OME_API_TOKEN', '')

# ── Database ──────────────────────────────────────────────────────────────────
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')  # better concurrency under gunicorn
        try:
            c.execute('ALTER TABLE sessions ADD COLUMN ref TEXT')
        except Exception:
            pass
        c.executescript('''
        CREATE TABLE IF NOT EXISTS emails (
            email TEXT PRIMARY KEY COLLATE NOCASE,
            name  TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS links (
            key        TEXT PRIMARY KEY,
            name       TEXT,
            expires_at TEXT,
            last_used  TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS magic_tokens (
            token      TEXT PRIMARY KEY,
            email      TEXT COLLATE NOCASE,
            expires_at TEXT,
            used       INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            name       TEXT,
            ref        TEXT,
            source     TEXT,
            expires_at TEXT,
            last_seen  TEXT,
            ip         TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS permissions (
            ref    TEXT NOT NULL,
            stream TEXT NOT NULL,
            PRIMARY KEY (ref, stream)
        );
        ''')

init_db()

if not SMTP_SSL and not SMTP_TLS:
    logging.warning('SMTP_SSL and SMTP_TLS are both false — email credentials sent in plaintext')

def now_str(): return datetime.utcnow().isoformat()
def future(days=0, minutes=0):
    return (datetime.utcnow() + timedelta(days=days, minutes=minutes)).isoformat()

def make_session(name, ref, source):
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute('INSERT INTO sessions (token,name,ref,source,expires_at,last_seen,ip) VALUES (?,?,?,?,?,?,?)',
                  (token, name, ref, source, future(days=SESSION_TTL), now_str(), request.remote_addr))
    return token

def set_session_cookie(resp, token):
    from urllib.parse import urlparse
    domain = urlparse(BASE_URL).hostname
    resp.set_cookie('st', token, httponly=True, secure=True, samesite='Lax',
                    max_age=SESSION_TTL * 86400, path='/', domain=domain)
    return resp

def get_session():
    token = request.cookies.get('st')
    if not token: return None
    with db() as c:
        row = c.execute('SELECT * FROM sessions WHERE token=? AND expires_at>?',
                        (token, now_str())).fetchone()
    if row:
        with db() as c:
            c.execute('UPDATE sessions SET last_seen=? WHERE token=?', (now_str(), token))
    return row

# ── OME API helper ────────────────────────────────────────────────────────────
def ome_get(path):
    token_b64 = b64encode(OME_API_TOKEN.encode()).decode()
    req = Request(f"{OME_API_URL}{path}", headers={'Authorization': f'Basic {token_b64}'})
    try:
        with urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        app.logger.error(f"OME API error {path}: {e}")
        return None

# ── Stream permission helpers ─────────────────────────────────────────────────
def get_allowed_streams(ref):
    with db() as c:
        rows = c.execute('SELECT stream FROM permissions WHERE ref=?', (ref,)).fetchall()
    if not rows:
        return set()  # no rows = no access (use * for all streams)
    allowed = {r['stream'] for r in rows}
    return allowed  # '*' in set means all; otherwise specific names only

def display_key(full_key):
    prefix = STREAM_SECRET + STREAM_SEP
    return full_key[len(prefix):] if full_key.startswith(prefix) else full_key

def filter_stream_list(full_keys, allowed):
    if allowed is None or '*' in allowed:
        return full_keys
    return [k for k in full_keys if display_key(k) in allowed]

# ── Auth check (nginx auth_request) ──────────────────────────────────────────
@app.route('/auth/check')
def auth_check():
    if not get_session(): abort(401)
    return '', 200

# ── Stream list (session-authenticated + permission-filtered) ─────────────────
@app.route('/api/streams')
def api_streams():
    session = get_session()
    if not session: abort(401)
    data = ome_get('/v1/vhosts/default/apps/live/streams')
    if not data:
        return jsonify(message='OK', response=[], statusCode=200)
    allowed = get_allowed_streams(session['ref'] if session['ref'] else None)
    filtered = filter_stream_list(data.get('response', []), allowed)
    # Return {key, name} objects — client gets full key for URL construction
    # but never needs to know STREAM_SECRET to reconstruct it
    prefix = STREAM_SECRET + STREAM_SEP
    streams = [{'key': k, 'name': k[len(prefix):] if k.startswith(prefix) else k} for k in filtered]
    if '*' in allowed:
        permitted = None
    else:
        permitted = list(allowed)
    return jsonify(message='OK', response=streams, permitted=permitted, statusCode=200)

@app.route('/api/stream/<path:full_key>')
def api_stream_detail(full_key):
    session = get_session()
    if not session: abort(401)
    allowed = get_allowed_streams(session['ref'] if session['ref'] else None)
    if allowed is not None and '*' not in allowed and display_key(full_key) not in allowed:
        abort(403)
    data = ome_get(f'/v1/vhosts/default/apps/live/streams/{full_key}')
    if not data: abort(404)
    return jsonify(data)

# ── Magic link ────────────────────────────────────────────────────────────────
@app.route('/auth/request-link', methods=['POST'])
def request_link():
    email = (request.get_json(silent=True) or {}).get('email', '').strip().lower()
    if not email: return jsonify(error='Email required'), 400
    with db() as c:
        row = c.execute('SELECT name FROM emails WHERE email=?', (email,)).fetchone()
    if not row: return jsonify(ok=True)
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute('INSERT INTO magic_tokens (token,email,expires_at) VALUES (?,?,?)',
                  (token, email, future(minutes=LINK_TTL)))
    link = f"{BASE_URL}/auth/verify-magic?token={token}"
    name = row['name'] or email
    try:
        send_mail(email, 'Your sign-in link',
            f"Hi {name},\n\nSign in here (expires in {LINK_TTL} minutes):\n\n{link}\n\n"
            f"If you did not request this, ignore this email.")
    except Exception as e:
        app.logger.error(f"SMTP error: {e}")
        return jsonify(error='Failed to send email.'), 500
    return jsonify(ok=True)

@app.route('/auth/verify-magic')
def verify_magic():
    token = request.args.get('token', '')
    with db() as c:
        row = c.execute('SELECT email FROM magic_tokens WHERE token=? AND used=0 AND expires_at>?',
                        (token, now_str())).fetchone()
    if not row: return redirect(BASE_URL + '/login.html?error=expired')
    with db() as c:
        c.execute('UPDATE magic_tokens SET used=1 WHERE token=?', (token,))
        em = c.execute('SELECT name FROM emails WHERE email=?', (row['email'],)).fetchone()
    name = (em['name'] if em and em['name'] else row['email'])
    resp = make_response(redirect(BASE_URL + '/'))
    return set_session_cookie(resp, make_session(name, row['email'], 'email'))

# ── Invite link ───────────────────────────────────────────────────────────────
@app.route('/auth/verify-link')
def verify_link():
    key = request.args.get('key', '')
    with db() as c:
        row = c.execute('SELECT name FROM links WHERE key=? AND (expires_at IS NULL OR expires_at>?)',
                        (key, now_str())).fetchone()
    if not row: return redirect(BASE_URL + '/login.html?error=invalid')
    with db() as c:
        c.execute('UPDATE links SET last_used=? WHERE key=?', (now_str(), key))
    name = row['name'] or key
    ref  = f'link:{key}'
    resp = make_response(redirect(BASE_URL + '/'))
    return set_session_cookie(resp, make_session(name, ref, 'link'))

# ── Logout ────────────────────────────────────────────────────────────────────
@app.route('/auth/logout', methods=['POST'])
def logout():
    # CSRF protection: only accept same-origin requests
    origin = request.headers.get('Origin', '')
    if origin and not origin.startswith(BASE_URL):
        abort(403)
    token = request.cookies.get('st')
    if token:
        with db() as c:
            c.execute('DELETE FROM sessions WHERE token=?', (token,))
    resp = make_response(redirect(BASE_URL + '/login.html'))
    resp.delete_cookie('st', path='/')
    return resp

# ── Admin: emails ─────────────────────────────────────────────────────────────
@app.route('/auth/admin/emails', methods=['GET'])
def admin_list_emails():
    with db() as c:
        rows = c.execute('SELECT email,name,created_at FROM emails ORDER BY created_at DESC').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/auth/admin/emails', methods=['POST'])
def admin_add_email():
    d = request.get_json(silent=True) or {}
    email = d.get('email', '').strip().lower()
    name  = d.get('name', '').strip()
    if not email: return jsonify(error='Email required'), 400
    with db() as c:
        is_new = c.execute('SELECT 1 FROM emails WHERE email=?', (email,)).fetchone() is None
        c.execute('INSERT OR REPLACE INTO emails (email,name) VALUES (?,?)', (email, name or None))
        if is_new:
            c.execute('INSERT OR IGNORE INTO permissions (ref,stream) VALUES (?,?)', (email, '*'))
    welcome_sent = False
    if is_new:
        try:
            token = secrets.token_urlsafe(32)
            with db() as c:
                c.execute('INSERT INTO magic_tokens (token,email,expires_at) VALUES (?,?,?)',
                          (token, email, future(minutes=WELCOME_TTL)))
            link    = f"{BASE_URL}/auth/verify-magic?token={token}"
            hours   = WELCOME_TTL // 60
            greeting = f"Hi {name}," if name else "Hi,"
            send_mail(email, 'You have been given access to the live stream viewer',
                f"{greeting}\n\nYou have been added to the guest list for the live stream viewer.\n\n"
                f"Click the link below to sign in (valid for {hours} hours):\n\n{link}\n\n"
                f"After signing in you can request a new link at any time from the login page.")
            welcome_sent = True
        except Exception as e:
            app.logger.error(f"Welcome email error for {email}: {e}")
    return jsonify(ok=True, welcome_sent=welcome_sent)

@app.route('/auth/admin/emails/<path:email>/resend', methods=['POST'])
def admin_resend_welcome(email):
    email = email.lower()
    with db() as c:
        row = c.execute('SELECT name FROM emails WHERE email=?', (email,)).fetchone()
    if not row: return jsonify(error='Email not found'), 404
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute('INSERT INTO magic_tokens (token,email,expires_at) VALUES (?,?,?)',
                  (token, email, future(minutes=WELCOME_TTL)))
    link  = f"{BASE_URL}/auth/verify-magic?token={token}"
    name  = row['name'] or email
    hours = WELCOME_TTL // 60
    try:
        send_mail(email, 'Your sign-in link for the live stream viewer',
            f"Hi {name},\n\nHere is your sign-in link (valid for {hours} hours):\n\n{link}\n\n"
            f"You can also request a new link at any time from the login page.")
        return jsonify(ok=True)
    except Exception as e:
        app.logger.error(f"Resend error for {email}: {e}")
        return jsonify(error='Failed to send email'), 500

@app.route('/auth/admin/emails/<path:email>', methods=['DELETE'])
def admin_del_email(email):
    email = email.lower()
    with db() as c:
        c.execute('DELETE FROM emails WHERE email=?', (email,))
        c.execute("DELETE FROM sessions WHERE source='email' AND ref=?", (email,))
        c.execute("DELETE FROM permissions WHERE ref=?", (email,))
        c.execute("DELETE FROM magic_tokens WHERE email=?", (email,))  # revoke outstanding links
    return jsonify(ok=True)

# ── Admin: invite links ───────────────────────────────────────────────────────
@app.route('/auth/admin/links', methods=['GET'])
def admin_list_links():
    with db() as c:
        rows = c.execute('SELECT key,name,expires_at,last_used,created_at FROM links ORDER BY created_at DESC').fetchall()
    return jsonify([{**dict(r), 'url': f"{BASE_URL}/invite/{r['key']}"} for r in rows])

@app.route('/auth/admin/links', methods=['POST'])
def admin_create_link():
    d = request.get_json(silent=True) or {}
    name     = d.get('name', '').strip()
    exp_days = d.get('expires_days')
    key = secrets.token_urlsafe(12)
    exp = future(days=int(exp_days)) if exp_days else None
    ref = f'link:{key}'
    with db() as c:
        c.execute('INSERT INTO links (key,name,expires_at) VALUES (?,?,?)', (key, name or None, exp))
        c.execute('INSERT OR IGNORE INTO permissions (ref,stream) VALUES (?,?)', (ref, '*'))
    return jsonify(key=key, url=f"{BASE_URL}/invite/{key}")

@app.route('/auth/admin/links/<key>', methods=['DELETE'])
def admin_del_link(key):
    ref = f'link:{key}'
    with db() as c:
        c.execute('DELETE FROM links WHERE key=?', (key,))
        c.execute("DELETE FROM sessions WHERE ref=?", (ref,))
        c.execute("DELETE FROM permissions WHERE ref=?", (ref,))
    return jsonify(ok=True)

# ── Admin: sessions ───────────────────────────────────────────────────────────
@app.route('/auth/admin/sessions', methods=['GET'])
def admin_list_sessions():
    with db() as c:
        rows = c.execute(
            'SELECT token,name,source,created_at,last_seen,ip FROM sessions WHERE expires_at>? ORDER BY last_seen DESC',
            (now_str(),)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/auth/admin/sessions/<token>', methods=['DELETE'])
def admin_del_session(token):
    with db() as c:
        c.execute('DELETE FROM sessions WHERE token=?', (token,))
    return jsonify(ok=True)

# ── Admin: active OME streams (for permission picker) ─────────────────────────
@app.route('/auth/admin/streams', methods=['GET'])
def admin_list_streams():
    data = ome_get('/v1/vhosts/default/apps/live/streams')
    if not data: return jsonify([])
    prefix = STREAM_SECRET + STREAM_SEP
    streams = [k[len(prefix):] for k in data.get('response', []) if k.startswith(prefix)]
    return jsonify(streams)

# ── Admin: permissions ────────────────────────────────────────────────────────
@app.route('/auth/admin/permissions/<path:ref>', methods=['GET'])
def admin_get_permissions(ref):
    with db() as c:
        rows = c.execute('SELECT stream FROM permissions WHERE ref=?', (ref,)).fetchall()
    return jsonify([r['stream'] for r in rows])

@app.route('/auth/admin/permissions/<path:ref>', methods=['POST'])
def admin_set_permissions(ref):
    streams = (request.get_json(silent=True) or {}).get('streams', [])
    with db() as c:
        c.execute('DELETE FROM permissions WHERE ref=?', (ref,))
        for s in streams:
            s = s.strip()
            if s:
                c.execute('INSERT OR IGNORE INTO permissions (ref,stream) VALUES (?,?)', (ref, s))
    return jsonify(ok=True)

# ── Email ─────────────────────────────────────────────────────────────────────
def send_mail(to, subject, body):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From']    = SMTP_FROM
    msg['To']      = to
    if SMTP_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
            if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            if SMTP_TLS: s.starttls()
            if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
