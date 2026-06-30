from flask import Flask, request, session, jsonify, send_from_directory, redirect
from flask_session import Session
import sqlite3, bcrypt, requests, os, json
from datetime import datetime

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
    ''')
    conn.commit()
    conn.close()

init_db()

# --- STATIC ROUTES ---
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

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
    # Get saved profile
    prof = conn.execute('SELECT * FROM user_profile WHERE user_id=?', (user['id'],)).fetchone()
    conn.close()
    result = {'loggedIn': True, 'user': dict(user)}
    if prof:
        result['savedProfile'] = {
            'profileKey': prof['profile_key'],
            'profileLabel': prof['profile_label'],
            'scores': json.loads(prof['scores']) if prof['scores'] else None,
            'capital': prof['capital'],
            'objetivo': prof['objetivo']
        }
    return jsonify(result)

# --- SAVE PROFILE ---
@app.route('/api/save-profile', methods=['POST'])
def save_profile():
    if not session.get('user_id'): return jsonify({'ok': False})
    data = request.json or {}
    conn = get_db()
    conn.execute('''INSERT INTO user_profile (user_id, profile_key, profile_label, scores, capital, objetivo, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        profile_key=excluded.profile_key, profile_label=excluded.profile_label,
        scores=excluded.scores, capital=excluded.capital, objetivo=excluded.objetivo,
        updated_at=excluded.updated_at''',
        (session['user_id'], data.get('profileKey'), data.get('profileLabel'),
         json.dumps(data.get('scores')), data.get('capital'), data.get('objetivo'),
         datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# --- CHAT HISTORY ---
@app.route('/api/history')
def get_history():
    if not session.get('user_id'): return jsonify({'ok': False})
    conn = get_db()
    msgs = conn.execute(
        'SELECT role, content, created_at FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT 30',
        (session['user_id'],)
    ).fetchall()
    conn.close()
    return jsonify({'ok': True, 'messages': [dict(m) for m in reversed(msgs)]})

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

# --- INITIAL RECOMMENDATION ---
@app.route('/api/recommend', methods=['POST'])
def recommend():
    if not session.get('user_id'): return jsonify({'ok': False, 'error': 'No autenticado.'})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user:
        conn.close()
        return jsonify({'ok': False, 'error': 'Usuario no encontrado.'})
    if user['plan'] != 'paid' and user['demo_used'] >= 1:
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
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 600, 'messages': [{'role': 'user', 'content': prompt}]},
            timeout=55
        )
        result = resp.json()
        if 'error' in result:
            conn.close()
            return jsonify({'ok': False, 'error': str(result['error'].get('message',''))})

        text = result.get('content', [{}])[0].get('text', '').strip()

        if user['plan'] != 'paid':
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

    # Get recent history from DB
    recent_history = get_recent_history(session['user_id'], limit=12)

    # Build context string
    context_parts = []
    if capital: context_parts.append(f"Capital disponible: {capital}")
    if objetivo: context_parts.append(f"Objetivo financiero: {objetivo}")
    context_str = ". ".join(context_parts)

    # Convert capital to USD if in pesos (approx 1 USD = 1700 ARS)
    capital_usd = ""
    if capital:
        import re
        nums = re.findall(r'[\d\.]+', capital.replace(',','.'))
        num = float(nums[0]) if nums else 0
        if any(w in capital.lower() for w in ['peso','ars','$',' p ',' ar']):
            usd = num / 1700
            capital_usd = f"{capital} (aprox USD {usd:,.0f})"
        else:
            capital_usd = capital

    # High-ticket objectives always discussed in USD
    high_ticket_keywords = ['casa','departamento','auto','auto','carro','viaje','retiro','jubilacion','inmueble']
    objetivo_is_high_ticket = any(w in objetivo.lower() for w in high_ticket_keywords) if objetivo else False

    system = (
        f"Sos un asesor financiero argentino experto y personal. Usas el voseo. "
        f"Directo, profesional y claro. Terminos tecnicos los aclaras entre parentesis. Sin asteriscos ni markdown.\n"
        f"Perfil: {profile}. Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%.\n"
        + (f"Capital del usuario: {capital_usd}.\n" if capital_usd else "")
        + (f"Objetivo financiero: {objetivo}.\n" if objetivo else "")
        + ("IMPORTANTE: El objetivo del usuario involucra un bien o meta de alto valor. Siempre habla de ese objetivo en dolares (USD), no en pesos.\n" if objetivo_is_high_ticket else "")
        + f"Mercado junio 2026: inflacion ~2.3% mensual, dolar estable en bandas (~1700 ARS/USD), bolsa AR volatil, "
        f"S&P500 alcista por tech, riesgo pais bajando.\n\n"
        "Este es un asesor de uso mensual recurrente. El usuario puede volver mes a mes. "
        "Tenes historial para dar contexto y continuidad.\n\n"
        "REGLAS - SEGUIR AL PIE DE LA LETRA:\n"
        "1. Respuesta SIEMPRE completa. Nunca cortes. Nunca esperes que el usuario pida continuar.\n"
        "2. Estructura OBLIGATORIA cuando el usuario da capital y/o objetivo:\n"
        "   - Parrafo 1 (2-3 oraciones): contexto breve + como distribuirias la cartera con porcentajes (esto acompaña el grafico de torta)\n"
        "   - Por cada instrumento: 1-2 oraciones explicando QUE ES y POR QUE encaja. El sistema muestra automaticamente el grafico de proyeccion de ESE instrumento debajo de tu texto.\n"
        "   - Parrafo final (cierre): proyeccion total concreta en numeros, escenario optimista y conservador, y si necesita aumentar aportes o el plazo para llegar al objetivo decilo. Este parrafo acompaña el grafico de proyeccion total.\n"
        "3. Si solo hay capital sin objetivo: distribucion + descripcion de cada instrumento en 1 oraion.\n"
        "4. Objetivos como casa, auto, viaje, retiro: siempre en USD.\n"
        "   Para auto en Argentina: usados accesibles desde USD 3000-5000, usados buen estado USD 6000-10000, 0km desde USD 18000.\n"
        "   Para casa: depende mucho de la ciudad, no dar cifras fijas, preguntar implicitamente con un rango amplio o pedir que el usuario aclare zona si es relevante.\n"
        "4b. Si el objetivo es un auto: NO inventes precios. Un auto usado accesible en Argentina arranca desde aproximadamente USD 3000-5000, y un 0km desde aproximadamente USD 18000. No digas que un usado cuesta 15000 ni que faltan datos del usuario sobre el tipo de auto. Si no especifico, asumi gama media usada (USD 6000-10000) y aclaralo.\n"
        "5. Aportes mensuales: explicar como 'agregar dinero nuevo a la inversion cada mes'.\n"
        "6. Sin preguntas al final. Sin frases vacias.\n"
        "\nFORMATO OBLIGATORIO DEL JSON (despues de ---CHART---):\n"
        "Incluir SIEMPRE: 3 instrumentos reales con pct sumando 100, MAS un instrumento Proyeccion total.\n"
        "IMPORTANTE sobre el orden y los datos: cada instrumento individual debe tener su array data mostrando los proximos 12 MESES proyectados (no historicos), partiendo del monto que le corresponde a ese instrumento segun su pct del capital. El campo Proyeccion total muestra la suma de todo el capital a 24 meses.\n"
        '[EJEMPLO con capital de 500 USD mensuales, 12 meses ahorrando]\n{"instruments":[\n'
        '{"name":"Plazo Fijo UVA","pct":40,"description":"Deposito bancario que te protege de la inflacion","trend":"up","trendNote":"proyectado a 12 meses","labels":["Jul 26","Ago 26","Sep 26","Oct 26","Nov 26","Dic 26","Ene 27","Feb 27","Mar 27","Abr 27","May 27","Jun 27"],"data":[200,403,609,818,1030,1245,1463,1684,1908,2135,2365,2598]},\n'
        '{"name":"Bono USD","pct":40,"description":"Prestamo al gobierno argentino pagado en dolares","trend":"up","trendNote":"proyectado a 12 meses","labels":["Jul 26","Ago 26","Sep 26","Oct 26","Nov 26","Dic 26","Ene 27","Feb 27","Mar 27","Abr 27","May 27","Jun 27"],"data":[200,402,606,812,1020,1231,1444,1660,1879,2100,2324,2551]},\n'
        '{"name":"ETF S&P500","pct":20,"description":"Canasta de las 500 empresas mas grandes de EEUU","trend":"up","trendNote":"proyectado a 12 meses","labels":["Jul 26","Ago 26","Sep 26","Oct 26","Nov 26","Dic 26","Ene 27","Feb 27","Mar 27","Abr 27","May 27","Jun 27"],"data":[100,201,303,406,510,615,721,828,936,1045,1155,1266]},\n'
        '{"name":"Proyeccion total","pct":0,"description":"Suma proyectada de toda tu cartera","trend":"up","trendNote":"proyeccion a 24 meses","labels":["Jul 26","Ago 26","Sep 26","Oct 26","Nov 26","Dic 26","Ene 27","Feb 27","Mar 27","Abr 27","May 27","Jun 27","Jul 27","Ago 27","Sep 27","Oct 27","Nov 27","Dic 27","Ene 28","Feb 28","Mar 28","Abr 28","May 28","Jun 28"],"data":[500,1006,1518,2036,2560,3091,3628,4172,4723,5280,5844,6415,6993,7578,8170,8769,9376,9990,10611,11240,11876,12520,13172,13832]}\n]}'
        "\nUSA valores reales para los datos de cada instrumento. La proyeccion debe reflejar el capital real del usuario."
    )

    # Build messages with DB history
    messages = []
    for h in recent_history:
        if h.get('role') and h.get('content'):
            messages.append({'role': h['role'], 'content': h['content']})
    messages.append({'role': 'user', 'content': message})

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-sonnet-4-6', 'max_tokens': 2000, 'system': system, 'messages': messages},
            timeout=55
        )
        result = resp.json()
        if 'error' in result:
            conn.close()
            return jsonify({'ok': False, 'error': str(result['error'])})

        full_text = result.get('content', [{}])[0].get('text', '').strip()
        parts = full_text.split('---CHART---')
        text = parts[0].strip()
        instruments = []

        if len(parts) > 1:
            try:
                json_str = parts[1].strip().replace('```json','').replace('```','').strip()
                instruments = json.loads(json_str).get('instruments', [])
            except Exception as e:
                print('JSON parse error:', e)

        # Save both messages to DB
        save_message(session['user_id'], 'user', message)
        save_message(session['user_id'], 'assistant', text)

        conn.close()
        return jsonify({'ok': True, 'text': text, 'instruments': instruments})

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
    recent = get_recent_history(session['user_id'], limit=6)
    conn.close()

    if not prof: return jsonify({'ok': False})

    name = user['name']
    capital = prof['capital'] or ''
    objetivo = prof['objetivo'] or ''
    profile = prof['profile_label'] or ''

    history_summary = ""
    if recent:
        last = recent[-1]['content'][:200] if recent else ""
        history_summary = f"Ultimo intercambio: {last}"

    prompt = (
        f"Sos un asesor financiero personal de {name}. Usas el voseo. Sin asteriscos ni markdown.\n"
        f"Perfil: {profile}. Capital: {capital}. Objetivo: {objetivo}.\n"
        f"{history_summary}\n\n"
        f"Dale una bienvenida corta y personalizada a {name} que vuelve a usar la app. "
        f"Mencioná algo concreto del contexto (su objetivo o su capital si los tiene). "
        f"Preguntale en que lo podés ayudar hoy. Maximo 2 oraciones."
    )

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 150, 'messages': [{'role': 'user', 'content': prompt}]},
            timeout=30
        )
        result = resp.json()
        text = result.get('content', [{}])[0].get('text', '').strip()
        save_message(session['user_id'], 'assistant', text)
        return jsonify({'ok': True, 'text': text})
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
