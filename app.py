from flask import Flask, request, session, jsonify, send_from_directory, redirect
from flask_session import Session
import sqlite3, bcrypt, requests, os, json, re, secrets
from datetime import datetime, timedelta

app = Flask(__name__, static_folder='public')
# CRITICO: nunca usar una clave fija como fallback. Si no hay SESSION_SECRET configurada,
# se genera una aleatoria en cada arranque. Esto invalida automaticamente cualquier cookie
# de sesion vieja despues de un reinicio/redeploy, evitando que se "pegue" a otra cuenta
# si la base de datos se reseteo y los IDs de usuario volvieron a empezar desde 1.
app.secret_key = os.environ.get('SESSION_SECRET') or secrets.token_hex(32)
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = '/tmp/flask_sessions'
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB, cubre imagenes en base64 (~33% mas pesadas que el original)
os.makedirs('/tmp/flask_sessions', exist_ok=True)
Session(app)

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_PATH = os.environ.get('DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database.db')

MP_ACCESS_TOKEN = os.environ.get('MP_ACCESS_TOKEN', '')
BASE_URL = os.environ.get('BASE_URL', 'https://invertia.onrender.com')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '').strip().lower()
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
RESEND_FROM_EMAIL = os.environ.get('RESEND_FROM_EMAIL', 'InvertIA <onboarding@resend.dev>')
CRON_SECRET = os.environ.get('CRON_SECRET', '')

def is_admin():
    if not ADMIN_EMAIL or not session.get('user_id'): return False
    conn = get_db()
    user = conn.execute('SELECT email FROM users WHERE id=?', (session['user_id'],)).fetchone()
    conn.close()
    return bool(user) and user['email'].lower() == ADMIN_EMAIL
PLAN_PRICES_ARS = {'paid': 8000, 'advanced': 18000}  # precio mensual fijo en pesos
DESCUENTO_ANUAL = 0.20  # -20% de descuento si paga anual
PLAN_NAMES = {'paid': 'Pro', 'advanced': 'Advanced'}
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            phone TEXT,
            plan TEXT DEFAULT 'free',
            demo_used INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            profile_key TEXT,
            profile_label TEXT,
            scores TEXT,
            capital TEXT,
            objetivo TEXT,
            objetivo_monto REAL,
            objetivo_plazo_meses INTEGER,
            objetivo_plazo_deseado_meses INTEGER,
            capital_mensual REAL,
            objetivo_retorno_anual REAL,
            objetivo_ahorrado_actual REAL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS financial_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            ingresos REAL DEFAULT 0,
            egresos REAL DEFAULT 0,
            categorias TEXT,
            gastos_hormiga TEXT,
            cuotas TEXT,
            ahorro_declarado REAL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS financial_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            period TEXT,
            ingresos REAL DEFAULT 0,
            egresos REAL DEFAULT 0,
            categorias TEXT,
            gastos_hormiga TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(user_id, period)
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            currency TEXT DEFAULT 'USD',
            status TEXT DEFAULT 'paid',
            plan TEXT,
            mp_preapproval_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS registration_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS pending_registrations (
            email TEXT PRIMARY KEY,
            name TEXT,
            phone TEXT,
            password_hash TEXT,
            code TEXT,
            ip TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    for col in ['objetivo_monto REAL','objetivo_plazo_meses INTEGER','objetivo_plazo_deseado_meses INTEGER','capital_mensual REAL','objetivo_retorno_anual REAL','objetivo_ahorrado_actual REAL']:
        try: conn.execute(f'ALTER TABLE user_profile ADD COLUMN {col}')
        except: pass
    for col in ['phone TEXT','last_summary_sent TEXT']:
        try: conn.execute(f'ALTER TABLE users ADD COLUMN {col}')
        except: pass
    for col in ['mp_preapproval_id TEXT']:
        try: conn.execute(f'ALTER TABLE payments ADD COLUMN {col}')
        except: pass
    for col in ['gastos_hormiga TEXT','cuotas TEXT','ahorro_declarado REAL','current_period TEXT']:
        try: conn.execute(f'ALTER TABLE financial_data ADD COLUMN {col}')
        except: pass
    conn.commit()
    conn.close()

init_db()

WEB_SEARCH_TOOLS = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]

def call_claude(messages, system, model='claude-haiku-4-5-20251001', max_tokens=1000, retries=1, tools=None):
    """Llama a la API de Anthropic. Si la respuesta trae un error transitorio
    (overloaded_error / rate_limit_error) reintenta una vez antes de rendirse.
    Siempre loguea el detalle del error para poder diagnosticarlo en los logs de Render.
    Si se pasa 'tools' (ej: WEB_SEARCH_TOOLS), el modelo puede buscar en la web
    antes de responder (util para cotizaciones, tasas o noticias de mercado actuales)."""
    result = {}
    for attempt in range(retries + 1):
        payload = {'model':model,'max_tokens':max_tokens,'system':system,'messages':messages}
        if tools:
            payload['tools'] = tools
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type':'application/json','x-api-key':ANTHROPIC_KEY,'anthropic-version':'2023-06-01'},
            json=payload,
            timeout=55
        )
        result = resp.json()
        if 'error' not in result:
            return result
        err = result.get('error', {})
        print(f'[Claude API error] attempt={attempt+1} type={err.get("type")} msg={err.get("message")}', flush=True)
        if err.get('type') in ('overloaded_error','rate_limit_error') and attempt < retries:
            continue
        break
    return result

def extract_text(result):
    """Concatena todos los bloques de tipo 'text' de la respuesta, en orden.
    Necesario cuando se usan tools server-side (como web_search): la respuesta
    trae bloques intercalados (server_tool_use, web_search_tool_result, text...)
    y el texto final puede estar partido en varios bloques de texto."""
    blocks = result.get('content', []) or []
    return ''.join(b.get('text','') for b in blocks if b.get('type') == 'text').strip()

def save_msg(uid, role, content):
    conn = get_db()
    conn.execute('INSERT INTO chat_messages (user_id,role,content) VALUES (?,?,?)',(uid,role,content))
    conn.commit(); conn.close()

def get_history(uid, limit=12):
    conn = get_db()
    msgs = conn.execute('SELECT role,content FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT ?',(uid,limit)).fetchall()
    conn.close()
    return [{'role':m['role'],'content':m['content']} for m in reversed(msgs)]

def days_since(uid):
    conn = get_db()
    last = conn.execute('SELECT created_at FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT 1',(uid,)).fetchone()
    conn.close()
    if not last: return None
    try:
        return (datetime.now()-datetime.fromisoformat(last['created_at'])).days
    except: return None

# STATIC
@app.route('/') 
def index(): return send_from_directory('public','index.html')
@app.route('/privacidad')
def privacidad(): return send_from_directory('public','privacidad.html')
@app.route('/terminos')
def terminos(): return send_from_directory('public','terminos.html')
@app.route('/register')
def register_page():
    if session.get('user_id'): return redirect('/app')
    return send_from_directory('public','register.html')
@app.route('/login')
def login_page():
    if session.get('user_id'): return redirect('/app')
    return send_from_directory('public','login.html')
@app.route('/app')
def app_page():
    if not session.get('user_id'): return redirect('/login')
    return send_from_directory('public','app.html')

@app.route('/admin')
def admin_page():
    if not session.get('user_id'): return redirect('/login')
    if not is_admin(): return redirect('/app')
    return send_from_directory('public','admin.html')

@app.route('/api/admin/users')
def admin_users():
    if not is_admin(): return jsonify({'ok': False, 'error': 'No autorizado.'}), 403
    conn = get_db()
    rows = conn.execute('SELECT id,name,email,phone,plan,demo_used,created_at FROM users ORDER BY created_at DESC').fetchall()
    conn.close()
    return jsonify({'ok': True, 'users': [dict(r) for r in rows]})

@app.route('/api/admin/set-plan', methods=['POST'])
def admin_set_plan():
    if not is_admin(): return jsonify({'ok': False, 'error': 'No autorizado.'}), 403
    d = request.json or {}
    user_id = d.get('user_id')
    plan = d.get('plan')
    if plan not in ('free', 'paid', 'advanced'): return jsonify({'ok': False, 'error': 'Plan invalido.'})
    conn = get_db()
    conn.execute('UPDATE users SET plan=? WHERE id=?', (plan, user_id))
    conn.commit(); conn.close()
    print(f'[admin] plan de user_id={user_id} cambiado a {plan}', flush=True)
    return jsonify({'ok': True})

@app.route('/api/admin/create-user', methods=['POST'])
def admin_create_user():
    if not is_admin(): return jsonify({'ok': False, 'error': 'No autorizado.'}), 403
    d = request.json or {}
    name,email,pw = d.get('name','').strip(), d.get('email','').strip().lower(), d.get('password','')
    phone = d.get('phone','').strip()
    plan = d.get('plan') if d.get('plan') in ('free','paid','advanced') else 'free'
    if not name or not email or not pw: return jsonify({'ok': False, 'error': 'Completá nombre, email y contraseña.'})
    if len(pw) < 6: return jsonify({'ok': False, 'error': 'La contraseña debe tener al menos 6 caracteres.'})
    conn = get_db()
    if conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
        conn.close(); return jsonify({'ok': False, 'error': 'Ya existe una cuenta con ese email.'})
    hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    conn.execute('INSERT INTO users (name,email,password,phone,plan) VALUES (?,?,?,?,?)', (name,email,hashed,phone,plan))
    conn.commit(); conn.close()
    print(f'[admin] usuario nuevo creado: {email} (plan={plan})', flush=True)
    return jsonify({'ok': True})

@app.route('/upgrade')
def upgrade_page():
    # Pagina vieja, reemplazada por el modal "Mejorar plan" dentro de la app (usaba el endpoint
    # /api/activate-paid que ya no existe). La redirigimos por si alguien todavia tiene el link guardado.
    return redirect('/app')
@app.route('/logout')
def logout(): session.clear(); return redirect('/')

# AUTH
def send_verification_email(to_email, code):
    if not RESEND_API_KEY:
        print('[Resend] RESEND_API_KEY no configurada, no se pudo enviar el codigo.', flush=True)
        return False
    try:
        resp = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'from': RESEND_FROM_EMAIL,
                'to': [to_email],
                'subject': f'Tu código de verificación InvertIA: {code}',
                'html': f'''
                    <div style="font-family:sans-serif;max-width:420px;margin:0 auto;padding:2rem;">
                        <h2 style="color:#0D0D1A;">Invert<span style="color:#6C47FF;">IA</span></h2>
                        <p style="color:#1F2937;font-size:15px;">Tu código de verificación es:</p>
                        <div style="font-size:32px;font-weight:700;letter-spacing:6px;color:#6C47FF;margin:1rem 0;">{code}</div>
                        <p style="color:#6B7280;font-size:13px;">Vence en 10 minutos. Si vos no pediste esto, ignorá este mail.</p>
                    </div>
                '''
            }, timeout=15
        )
        if resp.status_code >= 300:
            print('[Resend] error al enviar:', resp.status_code, resp.text, flush=True)
            return False
        return True
    except Exception as e:
        print('[Resend] excepcion al enviar:', repr(e), flush=True)
        return False

@app.route('/register', methods=['POST'])
def register():
    d = request.json or {}
    name,email,pw = d.get('name','').strip(),d.get('email','').strip().lower(),d.get('password','')
    phone = d.get('phone','').strip()
    if not name or not email or not pw or not phone: return jsonify({'ok':False,'error':'Completá todos los campos.'})
    if len(pw)<6: return jsonify({'ok':False,'error':'La contraseña debe tener al menos 6 caracteres.'})
    digits = re.sub(r'\D','',phone)
    if len(digits)<8: return jsonify({'ok':False,'error':'Ingresá un teléfono válido, con código de área.'})

    # Freno anti-abuso: no mas de 3 cuentas nuevas por dia desde la misma conexion
    ip = (request.headers.get('X-Forwarded-For','') or request.remote_addr or '').split(',')[0].strip()
    conn = get_db()
    if ip:
        hace_24h = (datetime.now() - timedelta(hours=24)).isoformat()
        intentos = conn.execute('SELECT COUNT(*) as c FROM registration_attempts WHERE ip=? AND created_at>=?',(ip,hace_24h)).fetchone()['c']
        if intentos >= 3:
            conn.close(); return jsonify({'ok':False,'error':'Se alcanzó el límite de cuentas nuevas creadas desde esta conexión hoy. Si sos vos mismo y necesitás otra cuenta, escribinos a soporte.'})

    if conn.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone():
        conn.close(); return jsonify({'ok':False,'error':'Ya existe una cuenta con ese email.'})
    if conn.execute('SELECT id FROM users WHERE phone=?',(digits,)).fetchone():
        conn.close(); return jsonify({'ok':False,'error':'Ya existe una cuenta creada con ese teléfono.'})

    hashed = bcrypt.hashpw(pw.encode(),bcrypt.gensalt()).decode()
    code = f'{secrets.randbelow(1000000):06d}'
    conn.execute('''INSERT INTO pending_registrations (email,name,phone,password_hash,code,ip,created_at) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET name=excluded.name,phone=excluded.phone,password_hash=excluded.password_hash,
        code=excluded.code,ip=excluded.ip,created_at=excluded.created_at''',
        (email,name,digits,hashed,code,ip,datetime.now().isoformat()))
    conn.commit(); conn.close()

    enviado = send_verification_email(email, code)
    if not enviado:
        return jsonify({'ok':False,'error':'No pudimos enviar el código de verificación. Intentá de nuevo en un momento.'})
    return jsonify({'ok':True,'needsVerification':True,'email':email})

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    d = request.json or {}
    email = d.get('email','').strip().lower()
    code = d.get('code','').strip()
    if not email or not code: return jsonify({'ok':False,'error':'Faltan datos.'})
    conn = get_db()
    row = conn.execute('SELECT * FROM pending_registrations WHERE email=?',(email,)).fetchone()
    if not row:
        conn.close(); return jsonify({'ok':False,'error':'No encontramos un registro pendiente para ese email. Registrate de nuevo.'})
    vencido = datetime.now() - datetime.fromisoformat(row['created_at']) > timedelta(minutes=10)
    if vencido:
        conn.close(); return jsonify({'ok':False,'error':'El código venció. Pedí uno nuevo.'})
    if row['code'] != code:
        conn.close(); return jsonify({'ok':False,'error':'Código incorrecto.'})

    # Chequeos de nuevo por las dudas (pudo haberse registrado alguien mas mientras tanto)
    ip = (request.headers.get('X-Forwarded-For','') or request.remote_addr or '').split(',')[0].strip()
    if conn.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone():
        conn.close(); return jsonify({'ok':False,'error':'Ya existe una cuenta con ese email.'})
    cur = conn.execute('INSERT INTO users (name,email,password,phone) VALUES (?,?,?,?)',(row['name'],email,row['password_hash'],row['phone']))
    conn.execute('INSERT INTO registration_attempts (ip) VALUES (?)',(ip,))
    conn.execute('DELETE FROM pending_registrations WHERE email=?',(email,))
    conn.commit(); session['user_id']=cur.lastrowid; session['user_name']=row['name']; conn.close()
    return jsonify({'ok':True})

@app.route('/api/resend-code', methods=['POST'])
def resend_code():
    d = request.json or {}
    email = d.get('email','').strip().lower()
    if not email: return jsonify({'ok':False,'error':'Falta el email.'})
    conn = get_db()
    row = conn.execute('SELECT * FROM pending_registrations WHERE email=?',(email,)).fetchone()
    if not row:
        conn.close(); return jsonify({'ok':False,'error':'No encontramos un registro pendiente para ese email.'})
    code = f'{secrets.randbelow(1000000):06d}'
    conn.execute('UPDATE pending_registrations SET code=?, created_at=? WHERE email=?',(code,datetime.now().isoformat(),email))
    conn.commit(); conn.close()
    enviado = send_verification_email(email, code)
    if not enviado: return jsonify({'ok':False,'error':'No pudimos reenviar el código. Intentá en un momento.'})
    return jsonify({'ok':True})

@app.route('/login', methods=['POST'])
def login():
    d = request.json or {}
    email,pw = d.get('email','').strip().lower(),d.get('password','')
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone(); conn.close()
    if not user or not bcrypt.checkpw(pw.encode(),user['password'].encode()):
        return jsonify({'ok':False,'error':'Email o contraseña incorrectos.'})
    session['user_id']=user['id']; session['user_name']=user['name']
    return jsonify({'ok':True})

def send_monthly_summary_email(user, actual, anterior):
    ingresos, egresos = actual['ingresos'] or 0, actual['egresos'] or 0
    balance = ingresos - egresos
    tasa = round((balance/ingresos)*100) if ingresos else 0
    y, m = actual['period'].split('-')
    periodo_label = f"{MESES_ES[int(m)-1].capitalize()} {y}"
    comparacion_html = ''
    if anterior and anterior['ingresos']:
        tasa_ant = round(((anterior['ingresos']-anterior['egresos'])/anterior['ingresos'])*100)
        diff = tasa - tasa_ant
        if diff > 0:
            comparacion_html = f'<p style="color:#00A87A;font-size:14px;">🎉 Ahorraste {diff} puntos más que el mes anterior.</p>'
        elif diff < 0:
            comparacion_html = f'<p style="color:#EF4444;font-size:14px;">Bajaste {abs(diff)} puntos respecto al mes anterior.</p>'
        else:
            comparacion_html = '<p style="color:#6B7280;font-size:14px;">Mismo nivel de ahorro que el mes anterior.</p>'
    if not RESEND_API_KEY: return False
    try:
        resp = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'from': RESEND_FROM_EMAIL,
                'to': [user['email']],
                'subject': f'Tu resumen de {periodo_label} — InvertIA',
                'html': f'''
                    <div style="font-family:sans-serif;max-width:460px;margin:0 auto;padding:2rem;">
                        <h2 style="color:#0D0D1A;">Invert<span style="color:#6C47FF;">IA</span></h2>
                        <p style="color:#1F2937;font-size:16px;">Hola {user['name']}, así te fue en <strong>{periodo_label}</strong>:</p>
                        <div style="display:flex;gap:10px;margin:1.25rem 0;">
                            <div style="flex:1;background:#F3F4F6;border-radius:10px;padding:.75rem;text-align:center;">
                                <div style="font-size:11px;color:#6B7280;">INGRESOS</div>
                                <div style="font-size:15px;font-weight:700;color:#00A87A;">${ingresos:,.0f}</div>
                            </div>
                            <div style="flex:1;background:#F3F4F6;border-radius:10px;padding:.75rem;text-align:center;">
                                <div style="font-size:11px;color:#6B7280;">EGRESOS</div>
                                <div style="font-size:15px;font-weight:700;color:#EF4444;">${egresos:,.0f}</div>
                            </div>
                            <div style="flex:1;background:#EEE9FF;border-radius:10px;padding:.75rem;text-align:center;">
                                <div style="font-size:11px;color:#6B7280;">AHORRO</div>
                                <div style="font-size:15px;font-weight:700;color:#6C47FF;">{tasa}%</div>
                            </div>
                        </div>
                        {comparacion_html}
                        <a href="{BASE_URL}/app" style="display:inline-block;margin-top:1rem;padding:10px 20px;background:#6C47FF;color:white;text-decoration:none;border-radius:9px;font-size:14px;font-weight:600;">Ver el detalle completo →</a>
                        <p style="color:#9CA3AF;font-size:12px;margin-top:2rem;">Contenido educativo. No constituye asesoramiento financiero profesional.</p>
                    </div>
                '''
            }, timeout=15
        )
        return resp.status_code < 300
    except Exception as e:
        print('[Resend] excepcion al enviar resumen mensual:', repr(e), flush=True)
        return False

def maybe_send_monthly_summary(user):
    """Manda el resumen del mes anterior (ya cerrado), apenas arranca el mes nuevo — nunca un mes
    a medio terminar. Se puede llamar todos los dias sin riesgo: solo manda una vez por mes real."""
    if user['plan'] != 'advanced': return False
    hoy = datetime.now()
    primer_dia_mes_actual = hoy.replace(day=1)
    ultimo_dia_mes_pasado = primer_dia_mes_actual - timedelta(days=1)
    mes_pasado = ultimo_dia_mes_pasado.strftime('%Y-%m')  # el mes recien terminado, el que hay que resumir
    if user['last_summary_sent'] == mes_pasado: return False  # ya se lo mandamos

    primer_dia_mes_pasado = ultimo_dia_mes_pasado.replace(day=1)
    mes_anterior_al_pasado = (primer_dia_mes_pasado - timedelta(days=1)).strftime('%Y-%m')

    conn = get_db()
    actual = conn.execute('SELECT * FROM financial_snapshots WHERE user_id=? AND period=?',(user['id'],mes_pasado)).fetchone()
    if not actual:
        conn.close(); return False  # no cargo datos financieros ese mes, no hay nada que resumir
    anterior = conn.execute('SELECT * FROM financial_snapshots WHERE user_id=? AND period=?',(user['id'],mes_anterior_al_pasado)).fetchone()
    conn.close()

    enviado = send_monthly_summary_email(user, actual, anterior)
    if enviado:
        conn = get_db()
        conn.execute('UPDATE users SET last_summary_sent=? WHERE id=?',(mes_pasado,user['id']))
        conn.commit(); conn.close()
    return enviado

@app.route('/api/cron/send-monthly-summaries')
def cron_send_monthly_summaries():
    """Pensado para ser llamado por un servicio de cron externo (ej: cron-job.org) una vez por dia.
    Revisa a todos los usuarios Advanced y le manda el resumen solo a quien todavia no lo recibio este mes
    (la funcion maybe_send_monthly_summary ya se encarga de no duplicar envios)."""
    if not CRON_SECRET or request.args.get('secret') != CRON_SECRET:
        return jsonify({'ok': False, 'error': 'No autorizado.'}), 403
    conn = get_db()
    usuarios = conn.execute("SELECT id,name,email,plan,last_summary_sent FROM users WHERE plan='advanced'").fetchall()
    conn.close()
    enviados = 0
    for u in usuarios:
        try:
            if maybe_send_monthly_summary(u): enviados += 1
        except Exception as e:
            print('[cron] error mandando resumen a', u['email'], repr(e), flush=True)
    print(f'[cron] resumenes mensuales: {enviados} enviados de {len(usuarios)} usuarios Advanced revisados', flush=True)
    return jsonify({'ok': True, 'revisados': len(usuarios), 'enviados': enviados})

@app.route('/me')
def me():
    if not session.get('user_id'): return jsonify({'loggedIn':False})
    conn = get_db()
    user = conn.execute('SELECT id,name,email,plan,demo_used,created_at,last_summary_sent FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user: conn.close(); return jsonify({'loggedIn':False})
    prof = conn.execute('SELECT * FROM user_profile WHERE user_id=?',(user['id'],)).fetchone()
    fin = conn.execute('SELECT * FROM financial_data WHERE user_id=?',(user['id'],)).fetchone()
    pmts = conn.execute('SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 5',(user['id'],)).fetchall()
    conn.close()
    result = {'loggedIn':True,'user':dict(user)}
    if prof:
        result['savedProfile'] = {
            'profileKey':prof['profile_key'],'profileLabel':prof['profile_label'],
            'scores':json.loads(prof['scores']) if prof['scores'] else None,
            'capital':prof['capital'],'objetivo':prof['objetivo'],
            'objetivoMonto':prof['objetivo_monto'],'objetivoPlazoMeses':prof['objetivo_plazo_meses'],
            'objetivoPlazoDeseadoMeses':prof['objetivo_plazo_deseado_meses'],'capitalMensual':prof['capital_mensual'],
            'objetivoRetornoAnual':prof['objetivo_retorno_anual'],
            'objetivoAhorradoActual':prof['objetivo_ahorrado_actual'],
        }
    if fin:
        result['financialData'] = {'ingresos':fin['ingresos'],'egresos':fin['egresos'],
            'categorias':json.loads(fin['categorias']) if fin['categorias'] else {},
            'gastosHormiga':json.loads(fin['gastos_hormiga']) if fin['gastos_hormiga'] else {},
            'cuotas':json.loads(fin['cuotas']) if fin['cuotas'] else [],
            'ahorroDeclarado':fin['ahorro_declarado']}
    result['payments'] = [dict(p) for p in pmts]
    result['daysSinceLastVisit'] = days_since(user['id'])
    return jsonify(result)

@app.route('/api/save-profile', methods=['POST'])
def save_profile():
    if not session.get('user_id'): return jsonify({'ok':False})
    d = request.json or {}
    conn = get_db()
    conn.execute('''INSERT INTO user_profile (user_id,profile_key,profile_label,scores,capital,objetivo,objetivo_monto,objetivo_plazo_meses,objetivo_plazo_deseado_meses,capital_mensual,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
        profile_key=excluded.profile_key,profile_label=excluded.profile_label,scores=excluded.scores,
        capital=excluded.capital,objetivo=excluded.objetivo,
        objetivo_monto=COALESCE(excluded.objetivo_monto,objetivo_monto),
        objetivo_plazo_meses=COALESCE(excluded.objetivo_plazo_meses,objetivo_plazo_meses),
        objetivo_plazo_deseado_meses=COALESCE(excluded.objetivo_plazo_deseado_meses,objetivo_plazo_deseado_meses),
        capital_mensual=COALESCE(excluded.capital_mensual,capital_mensual),updated_at=excluded.updated_at''',
        (session['user_id'],d.get('profileKey'),d.get('profileLabel'),json.dumps(d.get('scores')),
         d.get('capital'),d.get('objetivo'),d.get('objetivoMonto'),d.get('objetivoPlazoMeses'),
         d.get('objetivoPlazoDeseadoMeses'),d.get('capitalMensual'),datetime.now().isoformat()))
    conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/api/add-movement', methods=['POST'])
def add_movement():
    """Atajo manual: cargar un ingreso o gasto puntual sin pasar por el chat con el asesor.
    Se suma/mergea con lo que ya haya en el panel financiero, respetando el formato de dos niveles
    (subcategoria -> {concepto: monto})."""
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    user = conn.execute('SELECT plan FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user or user['plan'] != 'advanced': conn.close(); return jsonify({'ok':False,'error':'Requiere plan Advanced'})
    d = request.json or {}
    tipo = d.get('tipo')  # 'ingreso' | 'gasto'
    concepto = (d.get('concepto') or '').strip()
    categoria = (d.get('categoria') or 'Otros').strip() or 'Otros'
    es_hormiga = bool(d.get('esHormiga'))
    try:
        monto = float(d.get('monto') or 0)
    except (ValueError, TypeError):
        monto = 0
    if not concepto or monto <= 0 or tipo not in ('ingreso','gasto'):
        conn.close(); return jsonify({'ok':False,'error':'Faltan datos (concepto, monto o tipo invalido).'})

    row = conn.execute('SELECT * FROM financial_data WHERE user_id=?',(session['user_id'],)).fetchone()
    ingresos = (row['ingresos'] if row and row['ingresos'] else 0) or 0
    egresos = (row['egresos'] if row and row['egresos'] else 0) or 0
    categorias = json.loads(row['categorias']) if row and row['categorias'] else {}
    hormiga = json.loads(row['gastos_hormiga']) if row and row['gastos_hormiga'] else {}

    if tipo == 'ingreso':
        ingresos += monto
    else:
        egresos += monto
        target = hormiga if es_hormiga else categorias
        if categoria not in target or not isinstance(target.get(categoria), dict): target[categoria] = {}
        target[categoria][concepto] = target[categoria].get(concepto, 0) + monto

    conn.execute('''INSERT INTO financial_data (user_id,ingresos,egresos,categorias,gastos_hormiga,updated_at) VALUES (?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET ingresos=excluded.ingresos, egresos=excluded.egresos,
        categorias=excluded.categorias, gastos_hormiga=excluded.gastos_hormiga, updated_at=excluded.updated_at''',
        (session['user_id'], ingresos, egresos, json.dumps(categorias), json.dumps(hormiga), datetime.now().isoformat()))

    current_period = datetime.now().strftime('%Y-%m')
    conn.execute('''INSERT INTO financial_snapshots (user_id,period,ingresos,egresos,categorias,gastos_hormiga,updated_at) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(user_id,period) DO UPDATE SET ingresos=excluded.ingresos, egresos=excluded.egresos,
        categorias=excluded.categorias, gastos_hormiga=excluded.gastos_hormiga, updated_at=excluded.updated_at''',
        (session['user_id'], current_period, ingresos, egresos, json.dumps(categorias), json.dumps(hormiga), datetime.now().isoformat()))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'financialData':{'ingresos':ingresos,'egresos':egresos,'categorias':categorias,'gastosHormiga':hormiga,'cuotas':json.loads(row['cuotas']) if row and row['cuotas'] else []}})

@app.route('/api/edit-item', methods=['POST'])
def edit_item():
    """Editar el monto de un gasto puntual del panel, moverlo a otra categoria/seccion, o eliminarlo.
    Ajusta egresos por la diferencia para que el total no quede inconsistente."""
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    user = conn.execute('SELECT plan FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user or user['plan'] != 'advanced': conn.close(); return jsonify({'ok':False,'error':'Requiere plan Advanced'})
    d = request.json or {}
    section = d.get('section')  # 'categorias' | 'gastosHormiga'
    subcat = (d.get('subcategoria') or '').strip()
    concepto = (d.get('concepto') or '').strip()
    eliminar = 'monto' not in d and not d.get('nuevaSubcategoria') and not d.get('nuevaSeccion')
    if section not in ('categorias','gastosHormiga') or not subcat or not concepto:
        conn.close(); return jsonify({'ok':False,'error':'Datos incompletos.'})

    row = conn.execute('SELECT * FROM financial_data WHERE user_id=?',(session['user_id'],)).fetchone()
    if not row: conn.close(); return jsonify({'ok':False,'error':'No hay datos financieros todavia.'})
    egresos = row['egresos'] or 0
    categorias = json.loads(row['categorias']) if row['categorias'] else {}
    hormiga = json.loads(row['gastos_hormiga']) if row['gastos_hormiga'] else {}
    sections = {'categorias':categorias,'gastosHormiga':hormiga}
    origen = sections[section]

    old_val = 0
    if subcat in origen and isinstance(origen.get(subcat), dict) and concepto in origen[subcat]:
        old_val = origen[subcat][concepto]
        del origen[subcat][concepto]
        if not origen[subcat]: del origen[subcat]

    if eliminar:
        egresos = max(0, egresos - old_val)
    else:
        try: nuevo_monto = float(d.get('monto')) if d.get('monto') is not None else old_val
        except (ValueError, TypeError): nuevo_monto = old_val
        nueva_seccion = d.get('nuevaSeccion') or section
        nueva_subcat = (d.get('nuevaSubcategoria') or '').strip() or subcat
        if nueva_seccion not in sections: nueva_seccion = section
        destino = sections[nueva_seccion]
        if nueva_subcat not in destino or not isinstance(destino.get(nueva_subcat), dict): destino[nueva_subcat] = {}
        destino[nueva_subcat][concepto] = nuevo_monto
        egresos = max(0, egresos - old_val + nuevo_monto)

    conn.execute('UPDATE financial_data SET egresos=?, categorias=?, gastos_hormiga=?, updated_at=? WHERE user_id=?',
        (egresos, json.dumps(categorias), json.dumps(hormiga), datetime.now().isoformat(), session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'financialData':{'ingresos':row['ingresos'] or 0,'egresos':egresos,'categorias':categorias,'gastosHormiga':hormiga}})

@app.route('/api/add-cuota', methods=['POST'])
def add_cuota():
    """Atajo manual: agregar una compra en cuotas sin pasar por el chat."""
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    user = conn.execute('SELECT plan FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user or user['plan'] != 'advanced': conn.close(); return jsonify({'ok':False,'error':'Requiere plan Advanced'})
    d = request.json or {}
    concepto = (d.get('concepto') or '').strip()
    tarjeta = (d.get('tarjeta') or '').strip() or 'Sin especificar'
    categoria = (d.get('categoria') or '').strip() or 'Otros'
    try:
        monto_cuota = float(d.get('montoCuota') or 0)
        cuotas_totales = int(d.get('cuotasTotales') or 0)
        cuotas_pagadas = int(d.get('cuotasPagadas') or 0)
    except (ValueError, TypeError):
        monto_cuota, cuotas_totales, cuotas_pagadas = 0, 0, 0
    if not concepto or monto_cuota <= 0 or cuotas_totales <= 0:
        conn.close(); return jsonify({'ok':False,'error':'Completá concepto, monto por cuota y cantidad de cuotas.'})
    cuotas_pagadas = max(0, min(cuotas_pagadas, cuotas_totales))

    row = conn.execute('SELECT cuotas FROM financial_data WHERE user_id=?',(session['user_id'],)).fetchone()
    cuotas_list = json.loads(row['cuotas']) if row and row['cuotas'] else []
    cuotas_list = [c for c in cuotas_list if (c.get('concepto') or '').strip().lower() != concepto.lower()]
    cuotas_list.append({'concepto':concepto,'tarjeta':tarjeta,'categoria':categoria,
                         'montoCuota':monto_cuota,'cuotasTotales':cuotas_totales,'cuotasPagadas':cuotas_pagadas})

    conn.execute('''INSERT INTO financial_data (user_id,cuotas,updated_at) VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET cuotas=excluded.cuotas, updated_at=excluded.updated_at''',
        (session['user_id'], json.dumps(cuotas_list), datetime.now().isoformat()))
    conn.commit()
    fin = conn.execute('SELECT * FROM financial_data WHERE user_id=?',(session['user_id'],)).fetchone()
    conn.close()
    return jsonify({'ok':True,'financialData':{
        'ingresos':fin['ingresos'] or 0,'egresos':fin['egresos'] or 0,
        'categorias':json.loads(fin['categorias']) if fin['categorias'] else {},
        'gastosHormiga':json.loads(fin['gastos_hormiga']) if fin['gastos_hormiga'] else {},
        'cuotas':cuotas_list}})

@app.route('/api/edit-cuota', methods=['POST'])
def edit_cuota():
    """Editar los datos de una cuota existente (identificada por su concepto original), o eliminarla."""
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    user = conn.execute('SELECT plan FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user or user['plan'] != 'advanced': conn.close(); return jsonify({'ok':False,'error':'Requiere plan Advanced'})
    d = request.json or {}
    concepto_original = (d.get('conceptoOriginal') or '').strip()
    eliminar = bool(d.get('eliminar'))
    if not concepto_original:
        conn.close(); return jsonify({'ok':False,'error':'Falta identificar la cuota.'})

    row = conn.execute('SELECT cuotas FROM financial_data WHERE user_id=?',(session['user_id'],)).fetchone()
    cuotas_list = json.loads(row['cuotas']) if row and row['cuotas'] else []
    cuotas_list = [c for c in cuotas_list if (c.get('concepto') or '').strip().lower() != concepto_original.lower()]

    if not eliminar:
        concepto = (d.get('concepto') or concepto_original).strip()
        tarjeta = (d.get('tarjeta') or '').strip() or 'Sin especificar'
        categoria = (d.get('categoria') or '').strip() or 'Otros'
        try:
            monto_cuota = float(d.get('montoCuota') or 0)
            cuotas_totales = int(d.get('cuotasTotales') or 0)
            cuotas_pagadas = int(d.get('cuotasPagadas') or 0)
        except (ValueError, TypeError):
            monto_cuota, cuotas_totales, cuotas_pagadas = 0, 0, 0
        if not concepto or monto_cuota <= 0 or cuotas_totales <= 0:
            conn.close(); return jsonify({'ok':False,'error':'Completá concepto, monto por cuota y cantidad de cuotas.'})
        cuotas_pagadas = max(0, min(cuotas_pagadas, cuotas_totales))
        cuotas_list.append({'concepto':concepto,'tarjeta':tarjeta,'categoria':categoria,
                             'montoCuota':monto_cuota,'cuotasTotales':cuotas_totales,'cuotasPagadas':cuotas_pagadas})

    conn.execute('''INSERT INTO financial_data (user_id,cuotas,updated_at) VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET cuotas=excluded.cuotas, updated_at=excluded.updated_at''',
        (session['user_id'], json.dumps(cuotas_list), datetime.now().isoformat()))
    conn.commit()
    fin = conn.execute('SELECT * FROM financial_data WHERE user_id=?',(session['user_id'],)).fetchone()
    conn.close()
    return jsonify({'ok':True,'financialData':{
        'ingresos':fin['ingresos'] or 0,'egresos':fin['egresos'] or 0,
        'categorias':json.loads(fin['categorias']) if fin['categorias'] else {},
        'gastosHormiga':json.loads(fin['gastos_hormiga']) if fin['gastos_hormiga'] else {},
        'cuotas':cuotas_list}})

@app.route('/api/save-financial', methods=['POST'])
def save_financial():
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    user = conn.execute('SELECT plan FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user or user['plan'] != 'advanced': conn.close(); return jsonify({'ok':False,'error':'Requiere plan Advanced'})
    d = request.json or {}
    conn.execute('''INSERT INTO financial_data (user_id,ingresos,egresos,categorias,updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET ingresos=COALESCE(excluded.ingresos,ingresos),
        egresos=COALESCE(excluded.egresos,egresos),categorias=COALESCE(excluded.categorias,categorias),updated_at=excluded.updated_at''',
        (session['user_id'],d.get('ingresos'),d.get('egresos'),json.dumps(d.get('categorias',{})),datetime.now().isoformat()))
    conn.commit(); conn.close(); return jsonify({'ok':True})

MESES_ES = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre']

@app.route('/api/financial-history')
def financial_history():
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    user = conn.execute('SELECT plan FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user or user['plan'] != 'advanced': conn.close(); return jsonify({'ok':False,'error':'Requiere plan Advanced'})
    rows = conn.execute('SELECT * FROM financial_snapshots WHERE user_id=? ORDER BY period ASC',(session['user_id'],)).fetchall()
    conn.close()
    periods = []
    for r in rows:
        ingresos, egresos = r['ingresos'] or 0, r['egresos'] or 0
        savings_rate = round(((ingresos-egresos)/ingresos)*100) if ingresos else None
        y, m = r['period'].split('-')
        periods.append({
            'period': r['period'],
            'periodLabel': f"{MESES_ES[int(m)-1].capitalize()} {y}",
            'ingresos': ingresos, 'egresos': egresos,
            'categorias': json.loads(r['categorias']) if r['categorias'] else {},
            'gastosHormiga': json.loads(r['gastos_hormiga']) if r['gastos_hormiga'] else {},
            'savingsRate': savings_rate,
        })
    return jsonify({'ok':True,'periods':periods})

@app.route('/api/recommend', methods=['POST'])
def recommend():
    if not session.get('user_id'): return jsonify({'ok':False,'error':'No autenticado.'})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user: conn.close(); return jsonify({'ok':False,'error':'Usuario no encontrado.'})
    if user['plan'] not in ('paid','advanced') and user['demo_used']>=1:
        conn.close(); return jsonify({'ok':False,'error':'demo_limit'})
    d = request.json or {}
    profile,scores,name = d.get('profile',''),d.get('scores',[0,0,0]),user['name']
    system = ("Sos un asesor financiero argentino experto. Voseo. Directo, profesional y claro. Sin asteriscos ni markdown.\n"
        "Antes de responder, buscá en la web las condiciones actuales del mercado argentino (inflacion mensual, "
        "cotizacion del dolar, riesgo pais, indice Merval) y la tendencia reciente del S&P 500, para dar datos "
        "reales y actualizados en vez de generalidades. Si la busqueda falla, seguí igual con tu mejor estimacion "
        "pero sin inventar cifras exactas que no puedas respaldar.")
    prompt = (f"El usuario se llama {name}, perfil {profile} (Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%).\n\n"
        f"Responde:\n- 1 oracion saludando a {name} y validando su perfil\n- 2 oraciones sobre el mercado hoy (con datos concretos y actuales)\n"
        f"- 3 instrumentos numerados con nombre y por que encaja con su perfil\nMaximo 160 palabras. Sin preguntas al final.")
    try:
        result = call_claude([{'role':'user','content':prompt}],system,model='claude-haiku-4-5-20251001',max_tokens=800,tools=WEB_SEARCH_TOOLS)
        if 'error' in result:
            err = result.get('error', {})
            conn.close()
            return jsonify({'ok':False,'error':err.get('message','Error de conexión con la IA.'),'errorType':err.get('type')})
        text = extract_text(result)
        if user['plan'] not in ('paid','advanced'):
            conn.execute('UPDATE users SET demo_used=demo_used+1 WHERE id=?',(session['user_id'],))
            conn.commit()
        save_msg(session['user_id'],'assistant',text)
        updated = conn.execute('SELECT demo_used,plan FROM users WHERE id=?',(session['user_id'],)).fetchone()
        conn.close()
        return jsonify({'ok':True,'text':text,'demoUsed':updated['demo_used'],'plan':updated['plan']})
    except Exception as e:
        conn.close(); return jsonify({'ok':False,'error':str(e)})

def extract_first_json(text):
    """Devuelve el primer objeto JSON balanceado dentro del texto, ignorando
    cualquier cosa antes o después (el modelo a veces agrega comentarios
    despues del bloque JSON, y eso rompe un json.loads estricto)."""
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        return None
    return None

CHART_SYSTEM_SUFFIX = """

===== GRAFICOS =====

USA ---CHART--- SOLO en estos casos, y SIEMPRE en el mismo mensaje donde mencionas los instrumentos o la proyeccion
(nunca lo anuncies para "despues"):
1. Cuando presentes la distribucion de cartera por primera vez (2+ instrumentos con %)
2. Cuando hagas una proyeccion comparativa (inversion actual vs aumentada)
3. Cuando el usuario pida explicitamente ver un grafico o una proyeccion NUEVA (con numeros distintos a los ya mostrados)

NO uses ---CHART--- cuando:
- Estes explicando el mercado o contexto
- Estes haciendo preguntas sobre objetivos o capital
- La cartera ya fue presentada antes en la misma sesion, AUNQUE el usuario pida "mas detalle" de los instrumentos:
  ese detalle va SOLO en texto (nombres concretos, tickers, como comprarlos), sin repetir el grafico de torta
- El usuario SOLO esta confirmando o aceptando algo que ya viste ("ok", "dale", "perfecto", "arranco con esto",
  "listo", "genial"), sin pedir nada nuevo ni cambiar montos/plazos: respondele con una confirmacion breve en
  texto (ej: "¡Buenísimo! Cualquier duda sobre cómo arrancar con alguno de los instrumentos, avisame.") y NADA MAS,
  nunca reenviando el grafico ni la proyeccion que ya viste antes
- El usuario te confirma que ya hizo la inversion de este mes ("ya invertí", "ya la hice"): eso actualiza el
  progreso (objetivoUpdate con ahorradoActual), NUNCA repite el grafico de distribucion de cartera
- Sea una respuesta corta o conversacional

FORMATO cuando corresponde:
---CHART---
{"instruments":[{"name":"CEDEARs S&P500","pct":45,"description":"Acciones tech via CEDEAR","trend":"up","trendNote":"+12% anual","labels":["2026","2027","2028","2029","2030","2031"],"data":[1000,1120,1254,1405,1574,1763]},{"name":"Acciones AR","pct":25,"description":"Bolsa argentina","trend":"up","trendNote":"+15% anual","labels":["2026","2027","2028","2029","2030","2031"],"data":[1000,1150,1322,1521,1749,2011]},{"name":"Bonos USD","pct":30,"description":"Renta fija en dolares","trend":"up","trendNote":"+7% anual","labels":["2026","2027","2028","2029","2030","2031"],"data":[1000,1070,1145,1225,1311,1403]}],"objetivoUpdate":{"monto":60000,"plazoMeses":180,"plazoDeseadoMeses":180,"capitalMensual":294,"retornoAnualEstimado":8,"ahorradoActual":5000}}

Cuando el mensaje sea sobre control financiero (plan Advanced), el bloque puede llevar SOLO financialUpdate, sin instruments ni objetivoUpdate:
---CHART---
{"financialUpdate":{"ingresos":800000,"egresos":550000,"categorias":{"Vivienda":{"Alquiler":250000,"ABL":12000,"Expensas":60000},"Impuestos":{"Monotributo":45000},"Alimentacion":{"Supermercado":180000}},"gastosHormiga":{"Comida y delivery":{"Cafeterias":18000,"Delivery":45000},"Suscripciones":{"Streaming":12000}},"cuotas":[{"concepto":"Notebook","tarjeta":"Visa BBVA","categoria":"Tecnologia","montoCuota":25000,"cuotasTotales":12,"cuotasPagadas":3},{"concepto":"Viaje a Bariloche","tarjeta":"Naranja X","categoria":"Viajes","montoCuota":40000,"cuotasTotales":6,"cuotasPagadas":1}]}}

REGLAS del JSON:
- pct suma exactamente 100
- Minimo 2 instrumentos reales
- data: rendimiento del instrumento comenzando en 1000
- objetivoUpdate: incluir cuando tenes monto + plazo + capital confirmados
- retornoAnualEstimado: OBLIGATORIO incluirlo siempre que mandes objetivoUpdate. Es el % de retorno anual promedio
  que asumiste (ponderado segun la mezcla de instrumentos de la cartera) para calcular que ese monto+plazo+capital
  alcanzan la meta. El frontend lo usa para proyectar con interes compuesto, asi que tiene que ser el MISMO numero
  que usaste vos internamente al calcular el plazo o el monto (no un numero aleatorio o distinto). Para una cartera
  conservadora tipicamente ronda 5-7%, moderada 7-9%, arriesgada 9-13% anual, pero usa el numero real que calculaste.
- ahorradoActual: cuanto tiene el usuario YA ahorrado/invertido para ese objetivo especificamente, segun lo que el
  mismo te dijo (no es una proyeccion tuya). Poné 0 si te dijo que arranca de cero. Es un monto en USD.
- financialUpdate: incluir (plan Advanced) apenas el usuario te de ingresos y/o egresos. Numeros en pesos argentinos,
  sin puntos de miles ni simbolo $ (ej: 800000, no "800.000"). categorias y gastosHormiga son objetos de DOS NIVELES:
  subcategoria -> {nombre del gasto: monto}. NUNCA pongas un gasto individual suelto directamente en categorias/
  gastosHormiga: siempre agrupalo dentro de una subcategoria (ver reglas de agrupacion y busqueda web mas arriba).
  Ambos objetos opcionales pero recomendados.
- cuotas: array de objetos, uno por cada compra en cuotas que el usuario te confirme. Cada objeto: concepto (que
  es), tarjeta (en que tarjeta/medio de pago la esta pagando, ej 'Visa BBVA'), categoria (ej: Tecnologia, Viajes,
  Hogar, Salud), montoCuota (lo que paga por mes, numero), cuotasTotales (cuantas cuotas tiene en total),
  cuotasPagadas (cuantas ya pago). Si no te aclara la tarjeta o la categoria, preguntaselas — son importantes
  porque el panel agrupa las cuotas por tarjeta. Igual que el resto: NUNCA inventes una cuota que el usuario no
  te confirmo, ni le agregues cuotas de ejemplo.
- ahorroMensualDeclarado: SIEMPRE que en la conversacion de FINANZAS el usuario te diga cuanto ahorra por mes EN
  PESOS (ej: 'ahorro 500000 por mes', 'lo que me sobra son 500 lucas'), incluí ese numero en este campo. OJO: esto
  es un numero DISTINTO y en OTRA MONEDA que el capital mensual en dolares que definio al armar su objetivo de
  inversion — nunca confundas uno con el otro, ni asumas que son lo mismo. Si el usuario no menciono un monto de
  ahorro en pesos en esta conversacion de finanzas, NO mandes este campo (no inventes un numero ni lo calcules vos
  a partir de ingresos menos egresos).
=====
"""

_dolar_mep_cache = {'rate': None, 'fetched_at': 0}
_last_portfolio_signature = {}  # user_id -> firma de la ultima cartera (nombre+pct) ya mostrada en esta corrida

def get_dolar_mep():
    """Devuelve la cotizacion del dolar MEP actual. Se cachea 1 hora para no pegarle a la API
    en cada mensaje. Si la API falla, devuelve un fallback conservador en vez de romper el chat."""
    now = datetime.now().timestamp()
    if _dolar_mep_cache['rate'] and (now - _dolar_mep_cache['fetched_at']) < 3600:
        return _dolar_mep_cache['rate']
    try:
        resp = requests.get('https://dolarapi.com/v1/dolares/bolsa', timeout=8)
        data = resp.json()
        rate = float(data.get('venta') or data.get('compra'))
        if rate and rate > 0:
            _dolar_mep_cache['rate'] = rate
            _dolar_mep_cache['fetched_at'] = now
            return rate
    except Exception as e:
        print('[dolar MEP fetch error]', e, flush=True)
    return _dolar_mep_cache['rate'] or 1500  # fallback si la API falla y no hay cache previo

def ensure_current_month_rollover(user_id):
    """Si arranco un mes nuevo desde la ultima vez que se toco el panel financiero, traslada los
    gastos FIJOS (categorias) automaticamente sin que el usuario tenga que repetirlos, y resetea
    los gastos hormiga (variables, esos si hay que volver a contarlos cada mes). Devuelve True si
    hizo el traspaso ahora mismo (para avisarle al asesor en el contexto), False si no hizo falta."""
    conn = get_db()
    fin = conn.execute('SELECT * FROM financial_data WHERE user_id=?',(user_id,)).fetchone()
    if not fin or not fin['categorias']:
        conn.close(); return False
    mes_actual = datetime.now().strftime('%Y-%m')
    if fin['current_period'] == mes_actual:
        conn.close(); return False  # ya esta al dia, no hace falta nada

    # Antes de trasladar, dejamos guardado el snapshot final del mes que se cierra
    if fin['current_period']:
        conn.execute('''INSERT INTO financial_snapshots (user_id,period,ingresos,egresos,categorias,gastos_hormiga,updated_at) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id,period) DO UPDATE SET ingresos=excluded.ingresos,egresos=excluded.egresos,
            categorias=excluded.categorias,gastos_hormiga=excluded.gastos_hormiga,updated_at=excluded.updated_at''',
            (user_id,fin['current_period'],fin['ingresos'],fin['egresos'],fin['categorias'],fin['gastos_hormiga'],datetime.now().isoformat()))

    # Los gastos FIJOS (categorias) se mantienen igual mes a mes; los hormiga (variables) arrancan en cero de nuevo
    categorias = json.loads(fin['categorias']) if fin['categorias'] else {}
    total_fijos = sum(sum(v.values()) for v in categorias.values()) if categorias else 0
    conn.execute("UPDATE financial_data SET egresos=?, gastos_hormiga='{}', current_period=?, updated_at=? WHERE user_id=?",
        (total_fijos, mes_actual, datetime.now().isoformat(), user_id))
    conn.commit(); conn.close()
    return True

@app.route('/api/chat', methods=['POST'])
def chat():
    if not session.get('user_id'): return jsonify({'ok':False,'error':'No autenticado.'})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user: conn.close(); return jsonify({'ok':False,'error':'Usuario no encontrado.'})
    d = request.json or {}
    profile,scores = d.get('profile',''),d.get('scores',[0,0,0])
    capital,objetivo,message = d.get('capital',''),d.get('objetivo',''),d.get('message','')
    obj_monto,obj_plazo,cap_mensual = d.get('objetivoMonto'),d.get('objetivoPlazoMeses'),d.get('capitalMensual')
    is_advanced = user['plan']=='advanced'
    mes_nuevo_rollover = ensure_current_month_rollover(session['user_id']) if is_advanced else False

    capital_usd = capital
    tiene_pesos = capital and any(w in capital.lower() for w in ['peso','ars','pesos','argentino',' p '])
    dolar_mep = None
    if tiene_pesos:
        dolar_mep = get_dolar_mep()
        cleaned = re.sub(r'\.(?=\d{3})', '', capital)  # sacar puntos de miles (formato argentino: 1.000.000)
        cleaned = cleaned.replace(',', '.')  # coma decimal -> punto
        nums = re.findall(r'[\d.]+', cleaned)
        try:
            monto_ars = float(nums[0]) if nums else 0
        except (ValueError, IndexError):
            monto_ars = 0
        if monto_ars > 0:
            capital_usd = f"{capital} (= USD {monto_ars/dolar_mep:,.0f} al dolar MEP de hoy, ${dolar_mep:,.0f})"

    high_ticket = any(w in objetivo.lower() for w in ['casa','departamento','auto','viaje','retiro','jubilacion','inmueble']) if objetivo else False
    history = get_history(session['user_id'],limit=12)

    ctx = []
    if capital_usd: ctx.append(f"Capital mensual disponible: {capital_usd}"+(" — YA esta convertido a USD arriba, usa ese numero en USD directamente, no hace falta que vos convertirlo ni buscar el dolar." if tiene_pesos else ""))
    if objetivo: ctx.append(f"Objetivo: {objetivo}")
    if obj_monto: ctx.append(f"Monto objetivo: USD {obj_monto:,.0f}")
    if obj_plazo: ctx.append(f"Plazo proyectado: {obj_plazo} meses")
    if cap_mensual: ctx.append(f"Capital mensual a invertir: USD {cap_mensual:,.0f}")
    if mes_nuevo_rollover: ctx.append(
        "ARRANCO UN MES NUEVO: ya trasladamos automaticamente los gastos FIJOS del usuario (alquiler, "
        "expensas, deudas, etc, siguen iguales que el mes pasado, no hace falta pedirselos de nuevo). Lo "
        "unico que falta este mes son los gastos HORMIGA/variables, que arrancaron en cero. Si el usuario "
        "menciona algo de finanzas, contale brevemente que sus gastos fijos ya quedaron cargados solos, y "
        "preguntale si algo de eso cambio o si tiene gastos variables nuevos para contarte de este mes."
    )

    system = (
        f"Sos un asesor financiero argentino experto y personal. Voseo. Directo, profesional. Sin asteriscos ni markdown.\n"
        f"Perfil: {profile}. Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%.\n"
        + ('\n'.join(ctx)+'\n' if ctx else '')
        + ("IMPORTANTE: objetivo de alto valor, siempre en USD.\n" if high_ticket else "")
        + ("UI REAL DE LA APP — si el usuario pregunta como cambiar o mejorar su plan, decile que vaya al menu "
           "⚙️ (arriba a la derecha) y toque 'Mejorar plan'. NUNCA inventes el nombre de una seccion que no existe "
           "(nunca digas 'Mi Plan', 'Configuracion de plan', ni nada parecido) — el boton se llama exactamente "
           "'Mejorar plan'.\n")
        + ("\n===== ASESORAMIENTO PROACTIVO (esto te diferencia de un broker comun) =====\n"
           "1. FONDO DE EMERGENCIA ANTES DE INVERTIR: cuando alguien te cuenta cuanto quiere invertir por mes "
           "(sobre todo si suena como que es toda la plata que le sobra), preguntale si ya tiene un colchon de "
           "emergencia armado (idealmente 3 a 6 meses de sus gastos, en algo liquido tipo caja de ahorro o plazo "
           "fijo). Si no lo tiene, se lo decis con honestidad: recomendale priorizar juntar ese colchon antes de "
           "invertir todo lo que le sobra, aunque eso signifique invertir menos por ahora (o nada por unos meses). "
           "Un broker no te va a decir esto porque gana plata con que operes — vos si, porque tu objetivo es que a "
           "la persona le vaya bien de verdad, no maximizar cuanto invierte.\n"
           "2. SIMULADOR '¿QUE PASARIA SI...?': si el usuario plantea un hipotetico tipo '¿que pasa si dejo de "
           "gastar en X y meto esa plata en mi objetivo?' o '¿que pasa si aporto Y mas por mes?', calculalo en el "
           "momento con numeros concretos (usando la misma logica de interes compuesto que usas para proyectar el "
           "objetivo): cuanto suma en total al final, cuantos meses antes llegaria a la meta, etc. Nunca respondas "
           "en abstracto ('te ayudaria mucho') — siempre con el numero puntual.\n"
           "3. CUOTAS VS CONTADO: si el usuario te pregunta si le conviene financiar una compra en cuotas o pagarla "
           "de contado, compara concretamente dos cosas: (a) cuanto termina pagando de mas en cuotas si hay interes "
           "o recargo (si no sabes el recargo especifico de esa compra, preguntaselo), contra (b) cuanto podria "
           "generar esa misma plata si la invirtiera en vez de gastarla de contado, usando su perfil y retorno "
           "estimado. Dale una recomendacion clara con los numeros de las dos opciones, no una respuesta generica.\n"
           "==============================================================\n")
        + ("\n===== CONTROL FINANCIERO (plan Advanced) =====\n"
           "Cuando el usuario pida ayuda para organizar sus finanzas, armar un presupuesto, o entender en que gasta:\n"
           "1. Hacé las preguntas necesarias para armar el panel completo (podes ir de a poco en mensajes sucesivos, "
           "pero no repitas preguntas sobre datos que ya tenes en el contexto de la conversacion):\n"
           "   - Ingresos mensuales totales\n"
           "   - Egresos mensuales totales\n"
           "   - Cuanto ahorra por mes EN PESOS (esto es un dato propio, distinto del capital mensual en dolares que "
           "ya definio para su objetivo de inversion — pedilo igual, no lo des por sabido ni lo asumas a partir de "
           "ingresos menos egresos)\n"
           "   - Gastos FIJOS grandes y recurrentes (alquiler/hipoteca, servicios, seguros, cuotas, prepaga, etc)\n"
           "   - Gastos HORMIGA: gastos chicos y frecuentes que suelen pasar desapercibidos pero suman (cafes, "
           "delivery, apps de comida, salidas a comer afuera, antojos, suscripciones de streaming, taxis/uber). "
           "IMPORTANTE SOBRE LOS GASTOS FIJOS: una vez que el usuario te los cuenta, el sistema los guarda y los "
           "traslada SOLO todos los meses siguientes — el usuario NUNCA tiene que repetirtelos mes a mes. Podes "
           "contarle esto como algo bueno de la app ('los gastos fijos te los guardo, no hace falta que me los "
           "digas de nuevo cada mes — solo contame lo que cambia'). Lo que SI hay que volver a preguntar cada mes "
           "son los gastos HORMIGA/variables, esos arrancan en cero cada mes porque cambian todo el tiempo.\n"
           "Preguntaselos especificamente si el usuario no los menciono solo. IMPORTANTE — NO CONFUNDIR: la compra "
           "de supermercado, carniceria, verduleria y demas alimentacion BASICA/necesaria para vivir NUNCA es un "
           "gasto hormiga, es un gasto FIJO (va en categorias, subcategoria tipo 'Alimentacion'). Gasto hormiga es "
           "lo discrecional y evitable (un cafe de mas, pedir delivery en vez de cocinar), no la compra semanal "
           "de comida que la familia necesita.\n"
           "   - CUOTAS: en algun momento de esta primera charla (no hace falta que sea la primera pregunta), "
           "ofrecele armar el seguimiento de cuotas: algo como '¿Sabías que si me contás en qué estás pagando en "
           "cuotas (en que tarjeta, que es, la categoria, cuánto pagás por mes, cuántas cuotas en total y cuántas "
           "ya pagaste) te puedo mostrar cuánto te queda pendiente, agrupado por tarjeta?'. Si acepta, pedile esos "
           "6 datos por cada cuota: tarjeta (ej: Visa BBVA, Naranja X), concepto (que es), categoria (ej: "
           "Tecnologia, Viajes, Hogar, Salud), montoCuota, cuotasTotales, cuotasPagadas.\n"
           "2. En cuanto tengas ingresos y egresos (con o sin desglose todavia), en ESE MISMO mensaje dale tu analisis "
           "concreto en texto: si esta gastando mas de lo que gana, cuanto margen real tiene para invertir, en que "
           "rubro parece concentrarse el gasto hormiga. Se especifico con numeros, nunca generico. IMPORTANTE: si "
           "todavia no tenes el desglose completo de en que se va la plata (solo tenes el total de egresos, sin "
           "categorias), NO le digas que 'todo lo que sobra es ahorro' — aclarale que el balance (ingresos menos "
           "egresos declarados) no es necesariamente ahorro, porque probablemente incluye gastos que todavia no "
           "contamos (salidas, gastos sueltos, etc). Sugerile que te cuente el desglose para que el numero sea mas real.\n"
           "3. En ESE MISMO mensaje, decile que puede ver el detalle completo (ingresos, egresos, gastos fijos y "
           "gastos hormiga por separado) en su panel financiero, en la pestaña de arriba, y aclarale que ese panel "
           "se arma en base a lo que el mismo te va contando — si algo esta mal o incompleto, puede corregirlo el "
           "mismo tocando cualquier gasto del panel, o contartelo a vos para que lo actualices.\n"
           "4. En ESE MISMO mensaje incluí el bloque ---CHART--- con financialUpdate en el JSON (formato mas abajo), "
           "separando categorias (gastos fijos) de gastosHormiga.\n"
           "5. Si despues el usuario da mas detalle o corrige algo, mandá un financialUpdate actualizado.\n"
           "AGRUPACION EN SUBCATEGORIAS: no dejes cada gasto suelto. Agrupalos en subcategorias claras y utiles "
           "(ej: Vivienda, Servicios, Impuestos, Alimentacion, Transporte, Cuidado personal, Entretenimiento, "
           "Deudas/Tarjetas, Salud, Educacion, Otros). Cada gasto individual va DENTRO de su subcategoria en el JSON "
           "(ver formato de dos niveles mas abajo).\n"
           "PROHIBIDO INVENTAR GASTOS: el financialUpdate es un dato financiero real, NUNCA agregues un gasto, "
           "subcategoria, o monto que el usuario no te haya confirmado explicitamente (por texto o en una imagen "
           "que adjunto). Prohibido completar subcategorias con items de ejemplo o en $0 'por si acaso' (ej: nunca "
           "agregues cosas como 'Yoga: $0' o 'Chino mandarin: $0' si el usuario no las menciono). Si no tenes "
           "informacion de una subcategoria, simplemente no la incluyas en el JSON — mejor un panel incompleto que "
           "uno con datos falsos.\n"
           "ESTO TAMBIEN APLICA A INGRESOS Y EGRESOS TOTALES: nunca le sumes al ingreso o egreso declarado un monto "
           "estimado o supuesto tuyo (ej: prohibido calcular 'egresos totales = lo que me dijiste + 470000 que "
           "estimo de gastos facultativos'). ingresos y egresos son SIEMPRE el numero exacto que el usuario declaro, "
           "nada mas. Si crees que falta informacion, preguntaselo (y parate ahi, ver regla de arriba), no lo "
           "estimes vos y lo sumes silenciosamente.\n"
           "IDENTIFICAR GASTOS NO CLAROS: si un gasto tiene un nombre que no reconoces con TOTAL certeza (nombre de "
           "banco, empresa, comercio local, sigla como 'ABL', 'AySA', 'Edesur', 'ARCA', etc), buscalo en la web "
           "primero para saber que es exactamente (ej: buscar 'que es ARCA Argentina') antes de asignarle categoria. "
           "PROHIBIDO categorizar por parecido de sonido o adivinando: 'ARCA' NO es alimentacion solo porque suena "
           "a 'arca de comida' — ARCA es el nuevo nombre de AFIP (Agencia de Recaudacion y Control Aduanero), un "
           "organismo impositivo, no un comercio de alimentos. Muchos de estos son impuestos o servicios "
           "especificos de Argentina que no deberias adivinar. Si despues de buscar seguis sin poder identificarlo "
           "con confianza, preguntale al usuario que es ese gasto en vez de inventar una categoria.\n"
           "NUNCA DUPLIQUES UN MONTO YA DECLARADO: si el usuario aclara que una PARTE de un monto que ya te dio "
           "corresponde a algo especifico (ej: 'de los 1.000.000 de tarjeta, 470.000 son gastos facultativos'), eso "
           "es informacion adicional sobre un monto que YA esta incluido en el total — no es plata nueva. NO agregues "
           "un item nuevo con ese monto sumandolo aparte (eso duplica el gasto y rompe el total). En cambio, "
           "reorganizá/etiquetá el desglose dentro del monto ya existente, sin cambiar el total de esa subcategoria.\n"
           "Si el usuario adjunta una imagen (captura de un resumen de gastos, estado de cuenta, ticket, etc), "
           "analizala vos mismo y extraé los montos y categorias relevantes para el financialUpdate, sin pedirle "
           "que te los tipee de nuevo. Los mismos criterios de agrupacion y busqueda aplican a los items que veas ahi.\n"
           "==========\n" if is_advanced else
           "Plan Pro: si pregunta por control financiero/gastos/presupuesto/organizar sus finanzas, decile en ese mismo "
           "mensaje que ese panel es parte del plan Advanced, sin hacerle las preguntas de ingresos/egresos.\n")
        + "\n===== BUSQUEDA WEB =====\n"
        "Tenes acceso a busqueda web. USALA cuando:\n"
        "- El usuario pide instrumentos especificos (que bonos, que acciones, que ticker, que ON) → buscá los "
        "instrumentos reales que cotizan hoy en Argentina/EEUU para esa categoria, con nombres y tickers concretos\n"
        "- Preguntan por cotizaciones, tasas, riesgo pais, inflacion, o cualquier dato de mercado actual\n"
        "- Vas a presentar o actualizar una cartera y no tenes certeza de que tu informacion de mercado este vigente\n"
        "- Aparece un gasto con un nombre que no reconoces (banco, empresa, sigla, comercio) y necesitas saber que "
        "es para categorizarlo bien en el panel financiero\n"
        "NO busques para preguntas conceptuales que ya podes responder bien (que es un CEDEAR, como funciona un bono, etc).\n"
        "==========================\n\n"
        + "Como referencia de base (usala solo si la busqueda web no trae algo mejor): inflacion ~2% mensual, "
          "dolar estable ~1700 ARS/USD, bolsa AR volatil, S&P500 alcista, riesgo pais bajando.\n\n"
        "===== REGLA CRITICA: NUNCA ANUNCIES ALGO SIN ENTREGARLO YA =====\n"
        "PROHIBIDO terminar un mensaje diciendo que vas a mostrar, dar el detalle de, o presentar algo (una proyeccion, "
        "una cartera, un desglose, un grafico) sin haberlo incluido YA, completo, en ese mismo mensaje.\n"
        "Ejemplos de mensajes PROHIBIDOS (anuncian pero no entregan):\n"
        "  - 'Antes de darte el detalle, necesito mostrarte la proyeccion a 30 anos.' (¿y la proyeccion? no esta)\n"
        "  - 'Perfecto. Aca va el detalle concreto de cada instrumento.' (¿y el detalle? no esta)\n"
        "  - 'Dame un segundo que te preparo la cartera.'\n"
        "Si vas a mostrar algo, el contenido real va inmediatamente a continuacion en el MISMO mensaje, nunca en el siguiente turno. "
        "No existen 'pasos intermedios' de aviso: o lo mostras ahora, o todavia no lo menciones y en cambio hace la pregunta que "
        "te falta para poder calcularlo.\n"
        "==============================================================\n\n"
        "===== REGLA CRITICA: NUNCA TE PREGUNTES Y TE RESPONDAS SOLO =====\n"
        "PROHIBIDO hacerle una pregunta al usuario y responderla vos mismo en ese mismo mensaje (ej: '¿Usás dolar "
        "MEP, blue o el oficial? El dolar MEP cotiza hoy a...'). Eso es un error grave, no una forma de ser proactivo. "
        "Regla simple: si el dato lo podes buscar en la web u obtener vos mismo (cotizaciones, tasas, que es un "
        "gasto/empresa/sigla), buscalo y usalo directo, SIN preguntarle nada al usuario sobre eso. Si el dato "
        "depende de una decision personal del usuario (que tipo de cambio prefiere usar el, cuanto quiere invertir, "
        "etc) y no lo sabes, preguntaselo UNA vez y esperá su respuesta en el siguiente turno — nunca lo asumas ni "
        "te la respondas vos mismo en el acto.\n"
        "Para conversion de pesos a dolares especificamente: el capital mensual, si el usuario lo dio en pesos, YA "
        "viene convertido a USD en el contexto de arriba — usalo directo, no hace falta que hagas nada. Pero si en "
        "el chat aparece OTRO monto en pesos que necesites convertir (ej: el precio de una propiedad, un gasto, "
        "cualquier cifra nueva en ARS), ahi si buscá vos la cotizacion del dolar MEP de hoy y convertí directo, sin "
        "preguntarle al usuario que tipo de dolar usar.\n"
        "OTRO PATRON PROHIBIDO (variante del mismo error): hacer una pregunta genuina y despues, EN EL MISMO "
        "MENSAJE, decir algo como 'igual, con lo que ya tengo te armo el analisis' y seguir dando el analisis "
        "completo con supuestos. Eso vuelve inutil la pregunta. Si tenes una pregunta genuina para el usuario "
        "(algo que depende de su situacion personal y no podes resolver buscando), hacé LA pregunta y PARÁ tu "
        "mensaje ahi, sin seguir armando nada mas en ese mismo mensaje. Esperá la respuesta antes de continuar.\n"
        "TERCERA VARIANTE PROHIBIDA: narrar tu propio proceso de busqueda o investigacion en el texto de la "
        "respuesta (ej: 'necesito identificar bien algunos items de tu planilla... Perfecto, ya identifiqué todo lo "
        "que necesitaba'). Si vas a buscar algo en la web para identificar un gasto o dato, hacelo en silencio y "
        "presentá el resultado ya incorporado a tu respuesta — nunca expongas el paso intermedio de 'voy a buscar / "
        "ya busqué'. El usuario no necesita ver tu proceso, solo el resultado.\n"
        "NO REPITAS UNA CONFIRMACION QUE YA DISTE: si en un turno anterior ya le confirmaste algo al usuario (ej: "
        "'ya sumamos USD 328 a tu progreso'), no lo repitas de nuevo palabra por palabra en un mensaje posterior "
        "solo porque el usuario menciono una palabra relacionada (ej: 'ahorro') en su siguiente pregunta. Anda "
        "directo a responder lo nuevo que te esta preguntando.\n"
        "EJEMPLO EXACTO DE ESTE ERROR (para que lo reconozcas y NUNCA lo repitas): turno 1, vos decis 'Sumo tu "
        "primer aporte: ya llevas USD 328 invertidos'. Turno 2, el usuario pregunta algo totalmente distinto ('me "
        "ayudas con mis finanzas?'). Turno 2 INCORRECTO: empezar con '¡Buenisimo! Confirmamos tu primer aporte — "
        "llevas USD 328 acumulados...' (esto es exactamente lo que NO hay que hacer: nadie te pidio reconfirmar "
        "el aporte, ya quedo confirmado en el turno 1). Turno 2 CORRECTO: ir derecho a responder sobre finanzas, "
        "sin mencionar el aporte para nada. Si el JSON de objetivoUpdate/financialUpdate de tu respuesta actual no "
        "tiene NINGUN valor nuevo o distinto al que ya se guardo antes, es una señal clara de que no tenias nada "
        "nuevo que confirmar — en ese caso ni siquiera generes el bloque CHART para eso, y arranca tu mensaje "
        "directamente por la respuesta a lo nuevo.\n"
        "==============================================================\n\n"
        "===== REGLA CRITICA: NUNCA REPITAS UN GRAFICO YA MOSTRADO =====\n"
        "Si ya mostraste el grafico de distribucion de cartera en esta conversacion, NO lo vuelvas a mostrar, pase lo "
        "que pase, salvo que el usuario cambie montos/plazo y necesite una cartera DISTINTA. Un mensaje de "
        "confirmacion del usuario ('genial', 'ok', 'dale', 'perfecto', 'gracias', 'todo bien', 'no tengo dudas') "
        "NUNCA es un pedido de ver la cartera de nuevo — es solo una confirmacion, respondela en texto y nada mas.\n"
        "==============================================================\n\n"
        "FLUJO:\n"
        "1. Si menciona objetivo (casa/auto/viaje/retiro) y no tenemos plazo → dar contexto del plazo habitual y preguntar cuando lo quiere, EN ESE MISMO MENSAJE:\n"
        "   - Casa/depto: 'Para una vivienda lo habitual es planificar entre 5 y 15 anos. ¿Vos para cuando lo tenias en mente?'\n"
        "   - Auto: 'Un auto generalmente se planifica entre 1 y 3 anos. ¿Cuando lo querias tener?'\n"
        "   - Viaje: 'Un viaje suele planificarse entre 6 meses y 2 anos. ¿Cuando lo tenias pensado?'\n"
        "   - Retiro: 'El retiro suele planificarse a 10-30 anos. ¿En que horizonte estas pensando?'\n"
        "   Ademas, si todavia no sabes cuanto tiene ya ahorrado/invertido para ese objetivo especificamente, "
        "preguntaselo tambien (puede ser en el mismo mensaje o en el siguiente turno): '¿Ya tenés algo ahorrado o "
        "invertido para esto, o arrancás de cero?'. Guardalo como ahorradoActual en el JSON (0 si arranca de cero).\n"
        "2. Cuando tenes capital + objetivo + plazo → en ese MISMO mensaje calculá si es realista ASUMIENDO que el "
        "capital se invierte (no que se guarda sin rendimiento): definí un % de retorno anual razonable segun el perfil "
        "y la mezcla de instrumentos, calculá con esa tasa (interes compuesto, no suma simple) si el monto final "
        "alcanza la meta, y si no alcanza sugerir aumentar inversion O estirar plazo, con cifras concretas.\n"
        "3. Si acepta un AUMENTO de inversion que vos le propusiste (una cifra nueva y distinta a la que ya tenia) → "
        "en ese MISMO mensaje mostrar la proyeccion comparativa completa, con el bloque ---CHART--- incluyendo data2 "
        "en el JSON. No lo pospongas para el siguiente turno. OJO: esto es solo para cuando cambia el numero. Si el "
        "usuario simplemente confirma seguir con la cartera que ya le mostraste ('ok', 'dale', 'arranco con esto'), "
        "eso NO es aceptar un aumento — ver regla de confirmaciones simples mas abajo, no repitas nada.\n"
        "4. Cuando alineas expectativas → en ese MISMO mensaje presentar la cartera personalizada completa (nombres, "
        "porcentajes, montos en USD/mes) CON el bloque ---CHART--- obligatorio incluido ahi mismo. En ese MISMO "
        "mensaje, agregá tambien una linea corta explicando que a partir de ahora van a poder ver el progreso en la "
        "pestaña 'Mi objetivo', que VA A ARRANCAR EN 0% (es normal, no es un error) hasta que le confirmes cuanto "
        "llevás invertido, y que se va a ir actualizando cada vez que le cuentes que hiciste la inversion del mes.\n"
        "5. Si el usuario pide 'mas detalle' o nombres especificos de instrumentos ya presentados → buscá en la web si "
        "hace falta, y en ese MISMO mensaje dá el detalle concreto (nombres reales, tickers, como se compran, por que "
        "encajan) EN TEXTO, sin volver a incluir el bloque ---CHART--- (la cartera y el grafico ya se mostraron antes).\n"
        "5.5. Si el usuario te confirma que YA HIZO la inversion de este mes (frases como 'ya hice la inversión', "
        "'ya invertí', 'listo, la hice', 'ya la deposité'), esto es una actualizacion de PROGRESO, no una nueva "
        "cartera. En ese MISMO mensaje: sumá ese aporte al ahorradoActual que ya tenias (mandalo en objetivoUpdate "
        "con el bloque ---CHART---, SOLO con objetivoUpdate, sin instruments), confirmale en texto cuanto lleva "
        "acumulado ahora, y NO reenvies el grafico de distribucion de cartera — eso ya se mostro antes y no cambio.\n"
        "6. Despues de presentar la cartera → siempre cerrar con UNA pregunta concreta: '¿Tenés alguna duda sobre la "
        "distribución o cómo empezar?'\n"
        "   - Si en el siguiente turno el usuario tiene una duda concreta → respondela en texto, SIN repetir el grafico.\n"
        "   - Si el usuario responde que no tiene dudas, o con una confirmacion simple ('no', 'genial', 'todo bien', "
        "'ok', 'dale', 'gracias') → NO repitas la cartera ni el grafico. En cambio, contale brevemente que va a poder "
        "ver el progreso de su objetivo en la pestaña 'Mi objetivo', y que se actualiza cada vez que le confirme al "
        "asesor que efectivamente hizo la inversion ese mes (aclarale que el numero que ve ahi es real, basado en lo "
        "que el confirma, no una proyeccion automatica).\n"
        "PREGUNTA TODO LO NECESARIO ANTES DE ARMAR LA CARTERA, NO DESPUES: si hay un dato que puede cambiar el monto "
        "objetivo de forma relevante (ej: el destino de un viaje cambia bastante el costo total), preguntalo ANTES "
        "de armar y mostrar la primera cartera. Si el usuario no te lo da y preferis avanzar igual, usa un promedio "
        "razonable y aclaraselo (esto esta bien). Lo que NUNCA tenes que hacer es armar una cartera con un numero "
        "provisorio, preguntar el dato que falta, y despues volver a armar OTRA cartera cuando te contesta — eso es "
        "duplicar trabajo innecesariamente. Armala una sola vez, con la mejor informacion que tengas en ese momento.\n"
        "===== CHECKLIST OBLIGATORIO ANTES DE MANDAR UN MENSAJE QUE PRESENTA UNA CARTERA =====\n"
        "Todo mensaje que presente o actualice una cartera (paso 4) tiene que tener las TRES cosas, las tres en el "
        "mismo mensaje, nunca faltando ninguna:\n"
        "  1. El bloque ---CHART--- con los instrumentos y porcentajes.\n"
        "  2. La explicacion de que el progreso se va a ver en la pestaña 'Mi objetivo', que arranca en 0% (es "
        "normal) y se actualiza cuando el confirme su inversion.\n"
        "  3. Una pregunta de cierre concreta ('¿Tenés alguna duda...?').\n"
        "Si te falta cualquiera de las tres, tu respuesta esta incompleta — revisala antes de responder.\n"
        "==============================================================\n"
        + CHART_SYSTEM_SUFFIX
    )

    msgs = [{'role':h['role'],'content':h['content']} for h in history if h.get('role') and h.get('content')]

    image = d.get('image') or {}
    image_data, image_media_type = image.get('data'), image.get('mediaType')
    if image_data and image_media_type:
        block_type = 'document' if image_media_type == 'application/pdf' else 'image'
        msgs.append({'role':'user','content':[
            {'type':block_type,'source':{'type':'base64','media_type':image_media_type,'data':image_data}},
            {'type':'text','text':message or '(el usuario adjunto un archivo sin agregar texto, analizalo igual)'}
        ]})
    else:
        msgs.append({'role':'user','content':message})

    try:
        result = call_claude(msgs,system,model='claude-sonnet-4-6',max_tokens=4000,tools=WEB_SEARCH_TOOLS)
        if 'error' in result:
            err = result.get('error', {})
            conn.close()
            return jsonify({'ok':False,'error':err.get('message','Error de conexión con la IA.'),'errorType':err.get('type')})
        full = extract_text(result)
        parts = full.split('---CHART---')
        text = parts[0].strip()
        instruments,obj_update,fin_update = [],[],None

        if len(parts)>1:
            js_raw = parts[1].strip().replace('```json','').replace('```','').strip()
            parsed = extract_first_json(js_raw)
            if parsed:
                instruments = parsed.get('instruments',[])
                obj_update = parsed.get('objetivoUpdate')
                fin_update = parsed.get('financialUpdate')
                # Freno de codigo (no depende de que el modelo "se acuerde"): si esta cartera
                # (mismos nombres+porcentajes) ya se mostro antes en esta sesion, no la repitas.
                sig = tuple(sorted((i.get('name'),i.get('pct')) for i in instruments if i.get('pct',0) and i.get('pct',0)>0))
                if sig:
                    if _last_portfolio_signature.get(session['user_id']) == sig:
                        instruments = []
                    else:
                        _last_portfolio_signature[session['user_id']] = sig
            else:
                print('Chart JSON parse error: no se encontro un JSON balanceado. Raw:', js_raw[:300], flush=True)

        if obj_update:
            conn.execute('''INSERT INTO user_profile (user_id,profile_key,profile_label,scores,capital,objetivo,objetivo_monto,objetivo_plazo_meses,objetivo_plazo_deseado_meses,capital_mensual,objetivo_retorno_anual,objetivo_ahorrado_actual,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                objetivo_monto=COALESCE(excluded.objetivo_monto,objetivo_monto),
                objetivo_plazo_meses=COALESCE(excluded.objetivo_plazo_meses,objetivo_plazo_meses),
                objetivo_plazo_deseado_meses=COALESCE(excluded.objetivo_plazo_deseado_meses,objetivo_plazo_deseado_meses),
                capital_mensual=COALESCE(excluded.capital_mensual,capital_mensual),
                objetivo_retorno_anual=COALESCE(excluded.objetivo_retorno_anual,objetivo_retorno_anual),
                objetivo_ahorrado_actual=COALESCE(excluded.objetivo_ahorrado_actual,objetivo_ahorrado_actual),updated_at=excluded.updated_at''',
                (session['user_id'],profile,profile,json.dumps(scores),capital,objetivo,
                 obj_update.get('monto'),obj_update.get('plazoMeses'),obj_update.get('plazoDeseadoMeses'),
                 obj_update.get('capitalMensual'),obj_update.get('retornoAnualEstimado'),
                 obj_update.get('ahorradoActual'),datetime.now().isoformat()))
            conn.commit()

        if fin_update and is_advanced:
            conn.execute('''INSERT INTO financial_data (user_id,ingresos,egresos,categorias,gastos_hormiga,cuotas,ahorro_declarado,current_period,updated_at) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET ingresos=COALESCE(excluded.ingresos,ingresos),
                egresos=COALESCE(excluded.egresos,egresos),categorias=COALESCE(excluded.categorias,categorias),
                gastos_hormiga=COALESCE(excluded.gastos_hormiga,gastos_hormiga),
                cuotas=COALESCE(excluded.cuotas,cuotas),
                ahorro_declarado=COALESCE(excluded.ahorro_declarado,ahorro_declarado),
                current_period=COALESCE(current_period,excluded.current_period),updated_at=excluded.updated_at''',
                (session['user_id'],fin_update.get('ingresos'),fin_update.get('egresos'),
                 json.dumps(fin_update.get('categorias',{})),json.dumps(fin_update.get('gastosHormiga',{})),
                 json.dumps(fin_update.get('cuotas')) if fin_update.get('cuotas') is not None else None,
                 fin_update.get('ahorroMensualDeclarado'),datetime.now().strftime('%Y-%m'),
                 datetime.now().isoformat()))
            # Snapshot del mes actual, para poder ver la evolucion mes a mes en el historial
            current_period = datetime.now().strftime('%Y-%m')
            conn.execute('''INSERT INTO financial_snapshots (user_id,period,ingresos,egresos,categorias,gastos_hormiga,updated_at) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(user_id,period) DO UPDATE SET ingresos=COALESCE(excluded.ingresos,ingresos),
                egresos=COALESCE(excluded.egresos,egresos),categorias=COALESCE(excluded.categorias,categorias),
                gastos_hormiga=COALESCE(excluded.gastos_hormiga,gastos_hormiga),updated_at=excluded.updated_at''',
                (session['user_id'],current_period,fin_update.get('ingresos'),fin_update.get('egresos'),
                 json.dumps(fin_update.get('categorias',{})),json.dumps(fin_update.get('gastosHormiga',{})),datetime.now().isoformat()))
            conn.commit()

        save_msg(session['user_id'],'user',message)
        save_msg(session['user_id'],'assistant',text)
        conn.close()
        return jsonify({'ok':True,'text':text,'instruments':instruments,'objetivoUpdate':obj_update,'financialUpdate':fin_update})
    except Exception as e:
        print('Chat error:',repr(e), flush=True); conn.close(); return jsonify({'ok':False,'error':str(e)})

@app.route('/api/reset-profile', methods=['POST'])
def reset_profile():
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    conn.execute('DELETE FROM user_profile WHERE user_id=?',(session['user_id'],))
    conn.execute('DELETE FROM chat_messages WHERE user_id=?',(session['user_id'],))
    conn.commit(); conn.close()
    _last_portfolio_signature.pop(session['user_id'], None)
    return jsonify({'ok':True})

@app.route('/api/clear-chat', methods=['POST'])
def clear_chat():
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    conn.execute('DELETE FROM chat_messages WHERE user_id=?',(session['user_id'],))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/returning-greeting', methods=['POST'])
def returning_greeting():
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    prof = conn.execute('SELECT * FROM user_profile WHERE user_id=?',(session['user_id'],)).fetchone()
    conn.close()
    if not prof: return jsonify({'ok':False})
    days = days_since(user['id'])
    ask_update = days and days>=20 and prof['objetivo_monto']
    returning = (f"Vuelve despues de {days} dias. Necesitamos que confirme cuanto lleva ahorrado/invertido HOY para "
                 f"su objetivo ('{prof['objetivo']}'), para que el progreso real que le mostramos en la app sea preciso "
                 f"y no una suposicion nuestra." ) if ask_update else ""
    prompt = (f"Sos el asesor personal de {user['name']}. Voseo. Sin asteriscos.\n"
        f"Perfil: {prof['profile_label']}. Capital: {prof['capital']}. Objetivo: {prof['objetivo']}.\n{returning}\n"
        f"Dale bienvenida personalizada mencionando algo de su contexto. "
        f"{'Preguntale directamente cuanto lleva ahorrado/invertido hasta hoy para su objetivo, asi le actualizas el progreso real (no le preguntes generalidades, pedile el numero concreto).' if ask_update else 'Preguntale en que lo podes ayudar hoy.'} "
        f"Maximo 2 oraciones.")
    try:
        result = call_claude([{'role':'user','content':prompt}],'',model='claude-haiku-4-5-20251001',max_tokens=150)
        text = extract_text(result)
        save_msg(session['user_id'],'assistant',text)
        return jsonify({'ok':True,'text':text,'daysSinceLastVisit':days})
    except Exception as e: return jsonify({'ok':False,'error':str(e)})

@app.route('/api/nova', methods=['POST'])
def nova():
    d = request.json or {}
    message,history = d.get('message',''),d.get('history',[])
    system = """Tu nombre es Nova. Sos la asistente virtual de InvertIA. Amable, clara, concisa. Voseo. Nunca digas que sos otra cosa.

InvertIA: asesoramiento financiero IA para Argentina.
- Demo: gratis, 1 analisis
- Pro: U$S 8/mes, chat ilimitado, cartera personalizada, memoria entre sesiones
- Advanced: U$S 21/mes, todo Pro + control financiero + panel inversiones
- Descuento anual 20%. Pago por MercadoPago. Cancelacion cuando quieras.
- No accedemos a cuentas bancarias. Contenido educativo.

REGLAS: Si podes responder → respondé directo (max 2-3 oraciones). Si no sabes o es problema tecnico → needs_ticket:true.
NUNCA menciones email ni des canales alternativos. NUNCA des dos opciones a la vez.

FORMATO: SOLO JSON valido, sin texto antes ni despues, sin markdown:
{"text":"tu respuesta","needs_ticket":false}"""

    msgs = [{'role':h['role'],'content':h['content']} for h in history[-6:] if h.get('role') and h.get('content')]
    msgs.append({'role':'user','content':message})
    try:
        result = call_claude(msgs,system,model='claude-haiku-4-5-20251001',max_tokens=300)
        text = extract_text(result)
        try:
            clean = re.sub(r'```json\s*','',text); clean = re.sub(r'```\s*','',clean).strip()
            match = re.search(r'\{[^{}]*"text"[^{}]*\}',clean,re.DOTALL)
            parsed = json.loads(match.group() if match else clean)
            return jsonify({'ok':True,'text':parsed.get('text','').replace('\\n','\n').replace('**',''),'needs_ticket':parsed.get('needs_ticket',False)})
        except:
            fallback = re.sub(r'\{.*?"text":\s*"','',text,flags=re.DOTALL)
            fallback = re.sub(r'",?\s*"needs_ticket".*','',fallback,flags=re.DOTALL).strip().strip('"')
            return jsonify({'ok':True,'text':fallback or text,'needs_ticket':False})
    except Exception as e: return jsonify({'ok':False,'error':str(e)})

def get_plan_amount(plan, ciclo):
    """Calcula el monto a cobrar segun el plan y el ciclo (monthly/yearly).
    Anual = 12 meses con el descuento aplicado, cobrado como un solo pago cada 12 meses."""
    mensual = PLAN_PRICES_ARS.get(plan)
    if not mensual: return None
    if ciclo == 'yearly':
        return round(mensual * 12 * (1 - DESCUENTO_ANUAL))
    return mensual

def create_mp_subscription(user, plan, ciclo='monthly'):
    """Crea una suscripcion (preapproval) en Mercado Pago para el usuario, plan y ciclo dados.
    Devuelve el JSON de respuesta de Mercado Pago, que incluye 'init_point' (el link de pago
    al que hay que redirigir a la persona) e 'id' (el ID de la suscripcion)."""
    if ciclo not in ('monthly', 'yearly'): ciclo = 'monthly'
    monto = get_plan_amount(plan, ciclo)
    if not monto: return {'error': {'message': 'Plan invalido'}}
    frequency = 12 if ciclo == 'yearly' else 1
    body = {
        'reason': f'InvertIA - Plan {PLAN_NAMES.get(plan, plan)} ({"Anual" if ciclo=="yearly" else "Mensual"})',
        'auto_recurring': {
            'frequency': frequency,
            'frequency_type': 'months',
            'transaction_amount': monto,
            'currency_id': 'ARS',
        },
        'back_url': f'{BASE_URL}/app',
        'payer_email': user['email'],
        'external_reference': f"{user['id']}:{plan}:{ciclo}",
    }
    try:
        resp = requests.post(
            'https://api.mercadopago.com/preapproval',
            headers={'Authorization': f'Bearer {MP_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
            json=body, timeout=20
        )
        return resp.json()
    except Exception as e:
        print('[Mercado Pago create subscription error]', repr(e), flush=True)
        return {'error': {'message': str(e)}}

@app.route('/api/create-subscription', methods=['POST'])
def create_subscription():
    if not session.get('user_id'): return jsonify({'ok': False, 'error': 'No autenticado.'})
    if not MP_ACCESS_TOKEN: return jsonify({'ok': False, 'error': 'Mercado Pago no esta configurado todavia.'})
    d = request.json or {}
    plan = d.get('plan')
    ciclo = d.get('ciclo') if d.get('ciclo') in ('monthly', 'yearly') else 'monthly'
    if plan not in ('paid', 'advanced'): return jsonify({'ok': False, 'error': 'Plan invalido.'})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    conn.close()
    if not user: return jsonify({'ok': False, 'error': 'Usuario no encontrado.'})
    result = create_mp_subscription(user, plan, ciclo)
    if 'error' in result or not result.get('init_point'):
        print('[Mercado Pago response]', result, flush=True)
        return jsonify({'ok': False, 'error': 'No se pudo generar el link de pago. Intentá de nuevo.'})
    return jsonify({'ok': True, 'init_point': result['init_point']})

@app.route('/api/mp-webhook', methods=['POST'])
def mp_webhook():
    """Mercado Pago llama a esta URL cuando cambia el estado de una suscripcion.
    Por seguridad, NUNCA confiamos en el contenido de la notificacion directamente:
    volvemos a preguntarle a la API de Mercado Pago (con nuestro Access Token) cual es
    el estado real de esa suscripcion antes de activar ningun plan."""
    data = request.json or {}
    preapproval_id = data.get('data', {}).get('id') or request.args.get('id')
    if not preapproval_id or not MP_ACCESS_TOKEN:
        return jsonify({'ok': True})  # respondemos 200 igual para que MP no reintente sin parar
    try:
        resp = requests.get(
            f'https://api.mercadopago.com/preapproval/{preapproval_id}',
            headers={'Authorization': f'Bearer {MP_ACCESS_TOKEN}'}, timeout=20
        )
        sub = resp.json()
    except Exception as e:
        print('[Mercado Pago webhook fetch error]', repr(e), flush=True)
        return jsonify({'ok': True})

    external_ref = sub.get('external_reference', '')
    partes = external_ref.split(':')
    if len(partes) < 2: return jsonify({'ok': True})
    user_id_str, plan = partes[0], partes[1]
    ciclo = partes[2] if len(partes) > 2 else 'monthly'
    if not user_id_str.isdigit() or plan not in ('paid', 'advanced'): return jsonify({'ok': True})
    user_id = int(user_id_str)

    if sub.get('status') == 'authorized':
        conn = get_db()
        # Evitar procesar la misma suscripcion dos veces si Mercado Pago reintenta el webhook
        ya_procesado = conn.execute(
            'SELECT id FROM payments WHERE mp_preapproval_id=?', (preapproval_id,)
        ).fetchone()
        if not ya_procesado:
            conn.execute('UPDATE users SET plan=? WHERE id=?', (plan, user_id))
            monto = sub.get('auto_recurring', {}).get('transaction_amount', 0)
            conn.execute(
                'INSERT INTO payments (user_id,amount,currency,status,plan,mp_preapproval_id) VALUES (?,?,?,?,?,?)',
                (user_id, monto, 'ARS', 'paid', plan, preapproval_id)
            )
            conn.commit()
        conn.close()
    elif sub.get('status') in ('cancelled', 'paused'):
        # Si la persona cancela la suscripcion en Mercado Pago, le sacamos el plan pago
        conn = get_db()
        conn.execute("UPDATE users SET plan='free' WHERE id=? AND plan=?", (user_id, plan))
        conn.commit(); conn.close()

    return jsonify({'ok': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT',3000))
    app.run(host='0.0.0.0',port=port,debug=False)
