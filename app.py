from flask import Flask, request, session, jsonify, send_from_directory, redirect
from flask_session import Session
import sqlite3, bcrypt, requests, os, json, re
from datetime import datetime, timedelta

app = Flask(__name__, static_folder='public')
app.secret_key = os.environ.get('SESSION_SECRET', 'invertia-dev-secret-2026')
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = '/tmp/flask_sessions'
os.makedirs('/tmp/flask_sessions', exist_ok=True)
Session(app)

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database.db')

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
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
    ''')
    # Add new columns if they don't exist
    try:
        conn.execute('ALTER TABLE user_profile ADD COLUMN objetivo_monto REAL')
    except: pass
    try:
        conn.execute('ALTER TABLE user_profile ADD COLUMN objetivo_plazo_meses INTEGER')
    except: pass
    try:
        conn.execute('ALTER TABLE user_profile ADD COLUMN objetivo_plazo_deseado_meses INTEGER')
    except: pass
    try:
        conn.execute('ALTER TABLE user_profile ADD COLUMN capital_mensual REAL')
    except: pass
    conn.commit()
    conn.close()

init_db()

def call_claude(messages, system, model='claude-haiku-4-5-20251001', max_tokens=1000):
    resp = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01'},
        json={'model': model, 'max_tokens': max_tokens, 'system': system, 'messages': messages},
        timeout=55
    )
    return resp.json()

def save_message(user_id, role, content):
    conn = get_db()
    conn.execute('INSERT INTO chat_messages (user_id, role, content) VALUES (?,?,?)', (user_id, role, content))
    conn.commit()
    conn.close()

def get_recent_history(user_id, limit=12):
    conn = get_db()
    msgs = conn.execute(
        'SELECT role, content FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT ?',
        (user_id, limit)
    ).fetchall()
    conn.close()
    return [{'role': m['role'], 'content': m['content']} for m in reversed(msgs)]

def days_since_last_visit(user_id):
    conn = get_db()
    last = conn.execute(
        'SELECT created_at FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT 1',
        (user_id,)
    ).fetchone()
    conn.close()
    if not last:
        return None
    try:
        last_dt = datetime.fromisoformat(last['created_at'])
        return (datetime.now() - last_dt).days
    except:
        return None

# --- STATIC ROUTES ---
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/privacidad')
def privacidad():
    return send_from_directory('public', 'privacidad.html')

@app.route('/terminos')
def terminos():
    return send_from_directory('public', 'terminos.html')

@app.route('/register')
def register_page():
    if session.get('user_id'): return redirect('/app')
    return send_from_directory('public', 'register.html')

@app.route('/login')
def login_page():
    if session.get('user_id'): return redirect('/app')
    return send_from_directory('public', 'login.html')

@app.route('/app')
def app_page():
    if not session.get('user_id'): return redirect('/login')
    return send_from_directory('public', 'app.html')

@app.route('/upgrade')
def upgrade_page():
    if not session.get('user_id'): return redirect('/login')
    return send_from_directory('public', 'upgrade.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# --- AUTH ---
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    name = data.get('name','').strip()
    email = data.get('email','').strip().lower()
    password = data.get('password','')
    if not name or not email or not password:
        return jsonify({'ok': False, 'error': 'Completá todos los campos.'})
    if len(password) < 6:
        return jsonify({'ok': False, 'error': 'La contraseña debe tener al menos 6 caracteres.'})
    conn = get_db()
    if conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Ya existe una cuenta con ese email.'})
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    cur = conn.execute('INSERT INTO users (name, email, password) VALUES (?,?,?)', (name, email, hashed))
    conn.commit()
    session['user_id'] = cur.lastrowid
    session['user_name'] = name
    conn.close()
    return jsonify({'ok': True})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email','').strip().lower()
    password = data.get('password','')
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    conn.close()
    if not user or not bcrypt.checkpw(password.encode(), user['password'].encode()):
        return jsonify({'ok': False, 'error': 'Email o contraseña incorrectos.'})
    session['user_id'] = user['id']
    session['user_name'] = user['name']
    return jsonify({'ok': True})

@app.route('/me')
def me():
    if not session.get('user_id'): return jsonify({'loggedIn': False})
    conn = get_db()
    user = conn.execute('SELECT id, name, email, plan, demo_used FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user:
        conn.close()
        return jsonify({'loggedIn': False})
    prof = conn.execute('SELECT * FROM user_profile WHERE user_id=?', (user['id'],)).fetchone()
    fin = conn.execute('SELECT * FROM financial_data WHERE user_id=?', (user['id'],)).fetchone()
    conn.close()
    result = {'loggedIn': True, 'user': dict(user)}
    if prof:
        result['savedProfile'] = {
            'profileKey': prof['profile_key'],
            'profileLabel': prof['profile_label'],
            'scores': json.loads(prof['scores']) if prof['scores'] else None,
            'capital': prof['capital'],
            'objetivo': prof['objetivo'],
            'objetivoMonto': prof['objetivo_monto'],
            'objetivoPlazoMeses': prof['objetivo_plazo_meses'],
            'objetivoPlazoDeseadoMeses': prof['objetivo_plazo_deseado_meses'],
            'capitalMensual': prof['capital_mensual'],
        }
    if fin:
        result['financialData'] = {
            'ingresos': fin['ingresos'],
            'egresos': fin['egresos'],
            'categorias': json.loads(fin['categorias']) if fin['categorias'] else {}
        }
    days = days_since_last_visit(user['id'])
    result['daysSinceLastVisit'] = days
    return jsonify(result)

# --- SAVE PROFILE ---
@app.route('/api/save-profile', methods=['POST'])
def save_profile():
    if not session.get('user_id'): return jsonify({'ok': False})
    data = request.json or {}
    conn = get_db()
    conn.execute('''INSERT INTO user_profile 
        (user_id, profile_key, profile_label, scores, capital, objetivo, objetivo_monto, objetivo_plazo_meses, objetivo_plazo_deseado_meses, capital_mensual, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        profile_key=excluded.profile_key, profile_label=excluded.profile_label,
        scores=excluded.scores, capital=excluded.capital, objetivo=excluded.objetivo,
        objetivo_monto=COALESCE(excluded.objetivo_monto, objetivo_monto),
        objetivo_plazo_meses=COALESCE(excluded.objetivo_plazo_meses, objetivo_plazo_meses),
        objetivo_plazo_deseado_meses=COALESCE(excluded.objetivo_plazo_deseado_meses, objetivo_plazo_deseado_meses),
        capital_mensual=COALESCE(excluded.capital_mensual, capital_mensual),
        updated_at=excluded.updated_at''',
        (session['user_id'], data.get('profileKey'), data.get('profileLabel'),
         json.dumps(data.get('scores')), data.get('capital'), data.get('objetivo'),
         data.get('objetivoMonto'), data.get('objetivoPlazoMeses'),
         data.get('objetivoPlazoDeseadoMeses'), data.get('capitalMensual'),
         datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# --- SAVE FINANCIAL DATA (Advanced) ---
@app.route('/api/save-financial', methods=['POST'])
def save_financial():
    if not session.get('user_id'): return jsonify({'ok': False})
    conn = get_db()
    user = conn.execute('SELECT plan FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user or user['plan'] != 'advanced':
        conn.close()
        return jsonify({'ok': False, 'error': 'Requiere plan Advanced'})
    data = request.json or {}
    conn.execute('''INSERT INTO financial_data (user_id, ingresos, egresos, categorias, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        ingresos=COALESCE(excluded.ingresos, ingresos),
        egresos=COALESCE(excluded.egresos, egresos),
        categorias=COALESCE(excluded.categorias, categorias),
        updated_at=excluded.updated_at''',
        (session['user_id'], data.get('ingresos'), data.get('egresos'),
         json.dumps(data.get('categorias', {})), datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# --- INITIAL RECOMMENDATION ---
@app.route('/api/recommend', methods=['POST'])
def recommend():
    if not session.get('user_id'): return jsonify({'ok': False, 'error': 'No autenticado.'})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user:
        conn.close()
        return jsonify({'ok': False, 'error': 'Usuario no encontrado.'})
    if user['plan'] != 'paid' and user['plan'] != 'advanced' and user['demo_used'] >= 1:
        conn.close()
        return jsonify({'ok': False, 'error': 'demo_limit'})

    data = request.json or {}
    profile = data.get('profile', '')
    scores = data.get('scores', [0,0,0])
    name = user['name']

    prompt = (
        f"Sos un asesor financiero argentino experto. Usas el voseo. Directo, profesional y claro. "
        f"Terminos tecnicos los aclaras entre parentesis. Sin asteriscos ni markdown.\n\n"
        f"El usuario se llama {name}, perfil {profile} "
        f"(Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%).\n"
        f"Mercado junio 2026: inflacion 2.3% mensual bajando, dolar estable en bandas, "
        f"bolsa AR volatil por rebalanceo de indices, mercado americano alcista por sector tecnologico, "
        f"riesgo pais en baja sostenida.\n\n"
        f"Respondé brevemente:\n"
        f"- 1 oracion saludando a {name} y validando su perfil\n"
        f"- 2 oraciones sobre el mercado hoy, concretas\n"
        f"- 3 instrumentos numerados (1. 2. 3.) con nombre, que es en una frase, y por que encaja con su perfil\n"
        f"Sin preguntas al final. Sin frases motivacionales. Maximo 160 palabras."
    )

    try:
        result = call_claude([{'role': 'user', 'content': prompt}], '', model='claude-haiku-4-5-20251001', max_tokens=600)
        if 'error' in result:
            conn.close()
            return jsonify({'ok': False, 'error': str(result['error'].get('message',''))})

        text = result.get('content', [{}])[0].get('text', '').strip()

        if user['plan'] not in ('paid', 'advanced'):
            conn.execute('UPDATE users SET demo_used = demo_used + 1 WHERE id=?', (session['user_id'],))
            conn.commit()

        save_message(session['user_id'], 'assistant', text)
        updated = conn.execute('SELECT demo_used, plan FROM users WHERE id=?', (session['user_id'],)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'text': text, 'demoUsed': updated['demo_used'], 'plan': updated['plan']})

    except Exception as e:
        print('Exception recommend:', str(e))
        conn.close()
        return jsonify({'ok': False, 'error': str(e)})


# --- MAIN CHAT ---
@app.route('/api/chat', methods=['POST'])
def chat():
    if not session.get('user_id'): return jsonify({'ok': False, 'error': 'No autenticado.'})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user:
        conn.close()
        return jsonify({'ok': False, 'error': 'Usuario no encontrado.'})

    data = request.json or {}
    profile = data.get('profile', '')
    scores = data.get('scores', [0,0,0])
    capital = data.get('capital', '')
    objetivo = data.get('objetivo', '')
    message = data.get('message', '')
    objetivo_monto = data.get('objetivoMonto')
    objetivo_plazo = data.get('objetivoPlazoMeses')
    capital_mensual = data.get('capitalMensual')

    is_advanced = user['plan'] == 'advanced'

    # Convert capital to USD
    capital_usd = ""
    if capital:
        nums = re.findall(r'[\d\.]+', capital.replace(',','.'))
        num = float(nums[0]) if nums else 0
        if any(w in capital.lower() for w in ['peso','ars',' p ']):
            usd = num / 1700
            capital_usd = f"{capital} (~USD {usd:,.0f})"
        else:
            capital_usd = capital

    high_ticket = any(w in objetivo.lower() for w in ['casa','departamento','auto','viaje','retiro','jubilacion','inmueble']) if objetivo else False

    # Get recent history from DB
    recent_history = get_recent_history(session['user_id'], limit=12)

    context_parts = []
    if capital_usd: context_parts.append(f"Capital mensual disponible: {capital_usd}")
    if objetivo: context_parts.append(f"Objetivo financiero: {objetivo}")
    if objetivo_monto: context_parts.append(f"Monto necesario para el objetivo: USD {objetivo_monto:,.0f}")
    if objetivo_plazo: context_parts.append(f"Plazo proyectado para alcanzar el objetivo: {objetivo_plazo} meses")
    if capital_mensual: context_parts.append(f"Capital mensual que invertira: USD {capital_mensual:,.0f}")

    system = (
        f"Sos un asesor financiero argentino experto y personal. Usas el voseo. "
        f"Directo, profesional y claro. Terminos tecnicos los aclaras entre parentesis. Sin asteriscos ni markdown.\n"
        f"Perfil: {profile}. Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%.\n"
        + (('\n'.join(context_parts) + '\n') if context_parts else '')
        + ("IMPORTANTE: El objetivo involucra un bien de alto valor. Siempre habla en USD.\n" if high_ticket else "")
        + (f"El usuario tiene plan Advanced con acceso a control financiero personal.\n" if is_advanced else
           f"El usuario tiene plan Pro. Si pregunta por control financiero, gastos, presupuesto o ingresos, decile que esas funciones son parte del plan Advanced.\n")
        + "Mercado junio 2026: inflacion ~2.3% mensual, dolar estable en bandas (~1700 ARS/USD), bolsa AR volatil, S&P500 alcista por tech, riesgo pais bajando.\n\n"
        "Este es un asesor de uso mensual recurrente con historial guardado.\n\n"
        "FLUJO DE CONVERSACION:\n"
        "1. Si el usuario menciona un objetivo concreto (casa, auto, viaje, retiro) y no tenemos el plazo deseado:\n"
        "   - Para casa/departamento: 'Para una vivienda, lo habitual es planificar entre 5 y 15 años. ¿Vos para cuándo lo tenías en mente?'\n"
        "   - Para auto: 'Un auto generalmente se planifica entre 1 y 3 años. ¿Cuándo lo querías tener?'\n"
        "   - Para viaje: 'Un viaje suele planificarse entre 6 meses y 2 años. ¿Cuándo lo tenías pensado?'\n"
        "   - Para retiro: 'El retiro suele planificarse a 10-30 años. ¿En qué horizonte estás pensando?'\n"
        "2. Cuando tengas capital mensual + objetivo + plazo deseado:\n"
        "   - Calculá si el plazo es realista con el capital actual\n"
        "   - Si NO alcanza: sugerí aumentar la inversion mensual O estirar el plazo, pregunta cual prefiere\n"
        "   - Si acepta aumentar: mostrá proyeccion comparativa (inversion actual vs aumentada)\n"
        "   - Siempre incluí ---CHART--- con JSON cuando hay proyecciones o distribuciones\n"
        "3. Recien cuando esten alineadas las expectativas → presentá la cartera personalizada\n\n"
        "REGLAS:\n"
        "- Respuestas concisas. Sin preguntas multiples. Una cosa a la vez.\n"
        "- Si hay capital y objetivo: SIEMPRE incluí ---CHART--- con distribucion.\n"
        "- Si hablás de proyecciones: SIEMPRE incluí ---CHART--- con datos.\n"
        "- Para proyecciones comparativas incluí 2 datasets en el JSON.\n"
        "- Sin asteriscos ni markdown.\n\n"
        "JSON despues de ---CHART---:\n"
        '{"instruments":[{"name":"nombre","pct":33,"description":"descripcion simple",'
        '"trend":"up|down|neutral","trendNote":"rendimiento",'
        '"labels":["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"],'
        '"data":[100,105,110],"data2":[100,110,120],"label2":"Con inversion aumentada",'
        '"unit":"USD"}],'
        '"objetivoUpdate":{"monto":50000,"plazoMeses":60,"plazoDeseadoMeses":36,"capitalMensual":500},'
        '"financialUpdate":{"ingresos":2000,"egresos":1500,"categorias":{"vivienda":800,"comida":400,"transporte":300}}}'
        "\nobjeivoUpdate y financialUpdate son opcionales, incluirlos solo cuando hay datos nuevos del usuario."
    )

    messages = []
    for h in recent_history:
        if h.get('role') and h.get('content'):
            messages.append({'role': h['role'], 'content': h['content']})
    messages.append({'role': 'user', 'content': message})

    try:
        result = call_claude(messages, system, model='claude-sonnet-4-6', max_tokens=2000)
        if 'error' in result:
            conn.close()
            return jsonify({'ok': False, 'error': str(result['error'])})

        full_text = result.get('content', [{}])[0].get('text', '').strip()
        parts = full_text.split('---CHART---')
        text = parts[0].strip()
        instruments = []
        objetivo_update = None
        financial_update = None

        if len(parts) > 1:
            try:
                json_str = parts[1].strip().replace('```json','').replace('```','').strip()
                parsed = json.loads(json_str)
                instruments = parsed.get('instruments', [])
                objetivo_update = parsed.get('objetivoUpdate')
                financial_update = parsed.get('financialUpdate')
            except Exception as e:
                print('JSON parse error:', e)

        # Save updates to DB
        if objetivo_update:
            conn.execute('''INSERT INTO user_profile 
                (user_id, profile_key, profile_label, scores, capital, objetivo, objetivo_monto, objetivo_plazo_meses, objetivo_plazo_deseado_meses, capital_mensual, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                objetivo_monto=COALESCE(excluded.objetivo_monto, objetivo_monto),
                objetivo_plazo_meses=COALESCE(excluded.objetivo_plazo_meses, objetivo_plazo_meses),
                objetivo_plazo_deseado_meses=COALESCE(excluded.objetivo_plazo_deseado_meses, objetivo_plazo_deseado_meses),
                capital_mensual=COALESCE(excluded.capital_mensual, capital_mensual),
                updated_at=excluded.updated_at''',
                (session['user_id'], profile, profile, json.dumps(scores), capital, objetivo,
                 objetivo_update.get('monto'), objetivo_update.get('plazoMeses'),
                 objetivo_update.get('plazoDeseadoMeses'), objetivo_update.get('capitalMensual'),
                 datetime.now().isoformat()))
            conn.commit()

        if financial_update and is_advanced:
            conn.execute('''INSERT INTO financial_data (user_id, ingresos, egresos, categorias, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                ingresos=COALESCE(excluded.ingresos, ingresos),
                egresos=COALESCE(excluded.egresos, egresos),
                categorias=COALESCE(excluded.categorias, categorias),
                updated_at=excluded.updated_at''',
                (session['user_id'], financial_update.get('ingresos'), financial_update.get('egresos'),
                 json.dumps(financial_update.get('categorias', {})), datetime.now().isoformat()))
            conn.commit()

        save_message(session['user_id'], 'user', message)
        save_message(session['user_id'], 'assistant', text)

        conn.close()
        return jsonify({
            'ok': True, 'text': text, 'instruments': instruments,
            'objetivoUpdate': objetivo_update,
            'financialUpdate': financial_update
        })

    except Exception as e:
        print('Exception chat:', str(e))
        conn.close()
        return jsonify({'ok': False, 'error': str(e)})


# --- RETURNING USER GREETING ---
@app.route('/api/returning-greeting', methods=['POST'])
def returning_greeting():
    if not session.get('user_id'): return jsonify({'ok': False})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    prof = conn.execute('SELECT * FROM user_profile WHERE user_id=?', (session['user_id'],)).fetchone()
    conn.close()

    if not prof: return jsonify({'ok': False})

    name = user['name']
    capital = prof['capital'] or ''
    objetivo = prof['objetivo'] or ''
    profile = prof['profile_label'] or ''
    days = days_since_last_visit(user['id'])

    returning_context = ""
    if days and days >= 25:
        returning_context = f"El usuario vuelve despues de {days} dias. Preguntale como le fue con sus inversiones y si quiere actualizar el plan."
    elif days and days >= 7:
        returning_context = f"El usuario vuelve despues de {days} dias."

    prompt = (
        f"Sos un asesor financiero personal de {name}. Usas el voseo. Sin asteriscos.\n"
        f"Perfil: {profile}. Capital: {capital}. Objetivo: {objetivo}.\n"
        f"{returning_context}\n\n"
        f"Dale una bienvenida personalizada a {name}. Mencioná algo concreto de su contexto. "
        f"{'Preguntale como le fue con sus inversiones el ultimo mes.' if days and days >= 25 else 'Preguntale en que lo podes ayudar hoy.'} "
        f"Maximo 2 oraciones. Sin asteriscos."
    )

    try:
        result = call_claude([{'role': 'user', 'content': prompt}], '', model='claude-haiku-4-5-20251001', max_tokens=150)
        text = result.get('content', [{}])[0].get('text', '').strip()
        save_message(session['user_id'], 'assistant', text)
        return jsonify({'ok': True, 'text': text, 'daysSinceLastVisit': days})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# --- NOVA SUPPORT BOT ---
@app.route('/api/nova', methods=['POST'])
def nova():
    data = request.json or {}
    message = data.get('message', '')
    history = data.get('history', [])

    system = """Tu nombre es Nova. Sos la asistente virtual de InvertIA. Sos amable, clara y concisa. Respondés en español con voseo. Nunca te presentes como otra cosa que no sea Nova.

InvertIA es una app de asesoramiento financiero con IA para el mercado argentino:
- Plan Demo: gratis, 1 análisis con gráficos, modo oscuro/claro
- Plan Pro: U$S 8/mes, chat ilimitado, cartera personalizada, memoria entre sesiones, análisis de mercado, seguimiento mensual
- Plan Advanced: U$S 21/mes, todo lo del Pro + control financiero personal + panel de inversiones actualizable
- Descuento anual del 20% en planes pagos
- Se puede cancelar en cualquier momento
- No accedemos a cuentas bancarias ni datos financieros reales
- Las recomendaciones son educativas, no reemplazan a un asesor matriculado
- Los pagos se hacen por MercadoPago

REGLAS:
1. Si la pregunta es sobre planes, funcionalidades, precios, privacidad o como funciona → respondé directamente. Maximo 2-3 oraciones.
2. Si es problema tecnico, bug, reclamo, o no sabes la respuesta → needs_ticket: true con mensaje simple explicando que vas a derivar al equipo.
3. NUNCA menciones email ni des canales alternativos. Solo abrí el ticket.
4. NUNCA des dos opciones al mismo tiempo.

FORMATO OBLIGATORIO - respondé ÚNICAMENTE con JSON válido, sin texto antes ni después, sin markdown:
{"text": "tu respuesta aquí", "needs_ticket": false}"""

    messages = []
    for h in history[-6:]:
        if h.get('role') and h.get('content'):
            messages.append({'role': h['role'], 'content': h['content']})
    messages.append({'role': 'user', 'content': message})

    try:
        result = call_claude(messages, system, model='claude-haiku-4-5-20251001', max_tokens=300)
        text = result.get('content', [{}])[0].get('text', '').strip()
        try:
            clean = re.sub(r'```json\s*', '', text)
            clean = re.sub(r'```\s*', '', clean).strip()
            match = re.search(r'\{[^{}]*"text"[^{}]*\}', clean, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                resp_text = parsed.get('text', '').replace('\\n', '\n').replace('**', '')
                return jsonify({'ok': True, 'text': resp_text, 'needs_ticket': parsed.get('needs_ticket', False)})
            else:
                parsed = json.loads(clean)
                resp_text = parsed.get('text', clean).replace('\\n', '\n').replace('**', '')
                return jsonify({'ok': True, 'text': resp_text, 'needs_ticket': parsed.get('needs_ticket', False)})
        except Exception as parse_err:
            fallback = text.replace('{"text":', '').replace('"needs_ticket": false}', '').replace('"needs_ticket": true}', '').strip().strip('"')
            return jsonify({'ok': True, 'text': fallback, 'needs_ticket': False})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/activate-paid', methods=['POST'])
def activate_paid():
    if not session.get('user_id'): return jsonify({'ok': False})
    conn = get_db()
    conn.execute("UPDATE users SET plan='paid' WHERE id=?", (session['user_id'],))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=False)
