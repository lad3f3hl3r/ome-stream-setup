import os, secrets, sqlite3, smtplib, logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, redirect, make_response, abort

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DB_PATH    = os.getenv('DB_PATH', '/data/auth.db')
BASE_URL   = os.getenv('BASE_URL', 'https://stream.example.com')
SMTP_HOST  = os.getenv('SMTP_HOST', 'localhost')
SMTP_PORT  = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER  = os.getenv('SMTP_USER', '')
SMTP_PASS  = os.getenv('SMTP_PASS', '')
SMTP_FROM  = os.getenv('SMTP_FROM', 'noreply@example.com')
SMTP_SSL   = os.getenv('SMTP_SSL', 'false').lower() == 'true'   # true = SMTPS port 465
SMTP_TLS   = os.getenv('SMTP_TLS', 'true').lower() == 'true'    # true = STARTTLS port 587
LINK_TTL   = int(os.getenv('MAGIC_LINK_EXPIRE_MINUTES', '15'))
SESSION_TTL= int(os.getenv('SESSION_EXPIRE_DAYS', '7'))

# ── Database ──────────────────────────────────────────────────────────────────
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
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
            source     TEXT,
            expires_at TEXT,
            last_seen  TEXT,
            ip         TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        ''')

init_db()

def now_str(): return datetime.utcnow().isoformat()
def future(days=0, minutes=0):
    return (datetime.utcnow() + timedelta(days=days, minutes=minutes)).isoformat()

def make_session(name, source):
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute('INSERT INTO sessions (token,name,source,expires_at,last_seen,ip) VALUES (?,?,?,?,?,?)',
                  (token, name, source, future(days=SESSION_TTL), now_str(), request.remote_addr))
    return token

def set_session_cookie(resp, token):
    resp.set_cookie('st', token, httponly=True, secure=True, samesite='Strict',
                    max_age=SESSION_TTL * 86400, path='/')
    return resp

# ── Auth check (nginx auth_request) ──────────────────────────────────────────
@app.route('/auth/check')
def auth_check():
    token = request.cookies.get('st')
    if not token: abort(401)
    with db() as c:
        row = c.execute('SELECT token FROM sessions WHERE token=? AND expires_at>?',
                        (token, now_str())).fetchone()
    if not row: abort(401)
    with db() as c:
        c.execute('UPDATE sessions SET last_seen=? WHERE token=?', (now_str(), token))
    return '', 200

# ── Magic link ────────────────────────────────────────────────────────────────
@app.route('/auth/request-link', methods=['POST'])
def request_link():
    email = (request.get_json(silent=True) or {}).get('email', '').strip().lower()
    if not email:
        return jsonify(error='Email required'), 400
    with db() as c:
        row = c.execute('SELECT name FROM emails WHERE email=?', (email,)).fetchone()
    if not row:
        return jsonify(ok=True)  # silent: don't reveal if email is authorized
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute('INSERT INTO magic_tokens (token,email,expires_at) VALUES (?,?,?)',
                  (token, email, future(minutes=LINK_TTL)))
    name = row['name'] or email
    link = f"{BASE_URL}/auth/verify-magic?token={token}"
    try:
        send_mail(email, 'Your sign-in link',
            f"Hi {name},\n\nSign in here (expires in {LINK_TTL} minutes):\n\n{link}\n\n"
            f"If you did not request this, ignore this email.")
    except Exception as e:
        app.logger.error(f"SMTP error: {e}")
        return jsonify(error='Failed to send email. Check SMTP configuration.'), 500
    return jsonify(ok=True)

@app.route('/auth/verify-magic')
def verify_magic():
    token = request.args.get('token', '')
    with db() as c:
        row = c.execute('SELECT email FROM magic_tokens WHERE token=? AND used=0 AND expires_at>?',
                        (token, now_str())).fetchone()
    if not row:
        return redirect('/login.html?error=expired')
    with db() as c:
        c.execute('UPDATE magic_tokens SET used=1 WHERE token=?', (token,))
        em = c.execute('SELECT name FROM emails WHERE email=?', (row['email'],)).fetchone()
    name = (em['name'] if em and em['name'] else row['email'])
    resp = make_response(redirect('/'))
    return set_session_cookie(resp, make_session(name, 'email'))

# ── Invite link ───────────────────────────────────────────────────────────────
@app.route('/auth/verify-link')
def verify_link():
    key = request.args.get('key', '')
    with db() as c:
        row = c.execute('SELECT name FROM links WHERE key=? AND (expires_at IS NULL OR expires_at>?)',
                        (key, now_str())).fetchone()
    if not row:
        return redirect('/login.html?error=invalid')
    with db() as c:
        c.execute('UPDATE links SET last_used=? WHERE key=?', (now_str(), key))
    name = row['name'] or key
    resp = make_response(redirect('/'))
    return set_session_cookie(resp, make_session(name, 'link'))

# ── Logout ────────────────────────────────────────────────────────────────────
@app.route('/auth/logout', methods=['POST'])
def logout():
    token = request.cookies.get('st')
    if token:
        with db() as c:
            c.execute('DELETE FROM sessions WHERE token=?', (token,))
    resp = make_response(redirect('/login.html'))
    resp.delete_cookie('st', path='/')
    return resp

# ── Admin API (protected by nginx Basic Auth) ─────────────────────────────────
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
        c.execute('INSERT OR REPLACE INTO emails (email,name) VALUES (?,?)', (email, name or None))
    return jsonify(ok=True)

@app.route('/auth/admin/emails/<path:email>', methods=['DELETE'])
def admin_del_email(email):
    email = email.lower()
    with db() as c:
        c.execute('DELETE FROM emails WHERE email=?', (email,))
        c.execute("DELETE FROM sessions WHERE source='email' AND name=?", (email,))
    return jsonify(ok=True)

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
    with db() as c:
        c.execute('INSERT INTO links (key,name,expires_at) VALUES (?,?,?)', (key, name or None, exp))
    return jsonify(key=key, url=f"{BASE_URL}/invite/{key}")

@app.route('/auth/admin/links/<key>', methods=['DELETE'])
def admin_del_link(key):
    with db() as c:
        c.execute('DELETE FROM links WHERE key=?', (key,))
        c.execute("DELETE FROM sessions WHERE source='link' AND name=(SELECT name FROM links WHERE key=?)", (key,))
    return jsonify(ok=True)

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
    app.run(host='0.0.0.0', port=5000)
