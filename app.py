from flask import Flask, request, session, jsonify, send_from_directory, redirect
from flask_session import Session
import sqlite3, bcrypt, requests, os, json

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
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            profile TEXT,
            recommendations TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    conn.close()

init_db()

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
    existing = conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'ok': False, 'error': 'Ya existe una cuenta con ese email.'})
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    cur = conn.execute('INSERT INTO users (name, email, password) VALUES (?,?,?)', (name, email, hashed))
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    session['user_id'] = user_id
    session['user_name'] = name
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
    conn.close()
    if not user: return jsonify({'loggedIn': False})
    return jsonify({'loggedIn': True, 'user': dict(user)})

# --- INITIAL RECOMMENDATION ---
@app.route('/api/recommend', methods=['POST'])
def recommend():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'No autenticado.'})
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
        f"Sos un asesor financiero argentino experto. Hablás con el voseo. "
        f"Sos directo, profesional y claro. Cuando usas un termino tecnico lo aclaras brevemente entre parentesis.\n\n"
        f"El usuario se llama {name}, tiene perfil {profile} "
        f"(Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%).\n"
        f"Mercado actual junio 2026: inflacion bajando al 2.3% mensual, dolar estable dentro de bandas cambiarias, "
        f"bolsa argentina volatil por rebalanceo de indices internacionales, mercado americano en tendencia alcista "
        f"impulsado por el sector tecnologico, riesgo pais argentino en baja sostenida.\n\n"
        f"Respondé de forma breve y profesional:\n"
        f"- 1 oracion saludando a {name} y validando su perfil\n"
        f"- 2 oraciones sobre el contexto del mercado hoy, claras y concretas\n"
        f"- 3 instrumentos recomendados como items, cada uno con: nombre, que es en una frase simple, y por que encaja con su perfil\n"
        f"- 1 oracion final sobre proximos pasos concretos\n"
        f"Sin asteriscos. Sin markdown. Sin frases motivacionales vacias. Maximo 180 palabras."
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
            return jsonify({'ok': False, 'error': str(result['error'].get('message','Error API'))})

        text = result.get('content', [{}])[0].get('text', '').strip()
        print('recommend ok, len:', len(text))

        if user['plan'] != 'paid':
            conn.execute('UPDATE users SET demo_used = demo_used + 1 WHERE id=?', (session['user_id'],))
            conn.commit()

        conn.execute('INSERT INTO history (user_id, profile, recommendations) VALUES (?,?,?)', (session['user_id'], profile, text))
        conn.commit()
        updated = conn.execute('SELECT demo_used, plan FROM users WHERE id=?', (session['user_id'],)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'text': text, 'demoUsed': updated['demo_used'], 'plan': updated['plan']})

    except requests.exceptions.Timeout:
        conn.close()
        return jsonify({'ok': False, 'error': 'La IA tardó demasiado. Intentá de nuevo.'})
    except Exception as e:
        print('Exception recommend:', str(e))
        conn.close()
        return jsonify({'ok': False, 'error': str(e)})


# --- CHAT ---
@app.route('/api/chat', methods=['POST'])
def chat():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'No autenticado.'})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user:
        conn.close()
        return jsonify({'ok': False, 'error': 'Usuario no encontrado.'})

    data = request.json or {}
    profile = data.get('profile', '')
    scores = data.get('scores', [0,0,0])
    history = data.get('history', [])
    message = data.get('message', '')
    needs_chart = data.get('needsChart', False)

    system = (
        "Sos un asesor financiero argentino experto. Hablás con el voseo. "
        "Sos directo, profesional y claro. Cuando usas un termino tecnico lo aclaras brevemente entre parentesis. "
        "Sin asteriscos ni markdown.\n"
        f"Perfil del usuario: {profile}. Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%.\n"
        "Mercado actual junio 2026: inflacion ~2.3% mensual, dolar estable en bandas, bolsa argentina volatil, "
        "S&P500 alcista por sector tech, riesgo pais bajando.\n\n"
        "REGLAS:\n"
        "- Respondé de forma concisa y util. No te extiendas innecesariamente.\n"
        "- Si el usuario menciona un monto de capital, arma una distribucion concreta con porcentajes que sumen 100.\n"
        "- Solo incluí graficos si la pregunta es sobre tendencias, proyecciones o comparacion de instrumentos.\n"
        "- Si incluís graficos, escribí ---CHART--- al final y un JSON con esta estructura exacta:\n"
        '{"instruments":[{"name":"nombre","pct":40,"description":"que es en palabras simples",'
        '"trend":"up|down|neutral","trendNote":"como le fue el ultimo año",'
        '"labels":["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"],'
        '"data":[100,105,110,108,115,120,118,125,130,128,135,140]}]}\n'
        "- Si hay distribucion de capital incluí pct en cada instrumento sumando 100. Si no hay capital, pct puede ser 0.\n"
        "- Maximo 3 instrumentos en el JSON.\n"
        "- Si la pregunta NO requiere graficos, respondé solo con texto, sin ---CHART---."
    )

    messages = []
    for h in history[-8:]:
        if h.get('role') and h.get('content'):
            messages.append({'role': h['role'], 'content': h['content']})
    if not messages or messages[-1]['role'] != 'user':
        messages.append({'role': 'user', 'content': message})

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 900, 'system': system, 'messages': messages},
            timeout=55
        )
        result = resp.json()
        if 'error' in result:
            conn.close()
            return jsonify({'ok': False, 'error': str(result['error'])})

        full_text = result.get('content', [{}])[0].get('text', '').strip()
        print('chat ok, len:', len(full_text))

        parts = full_text.split('---CHART---')
        text = parts[0].strip()
        instruments = []

        if len(parts) > 1:
            try:
                json_str = parts[1].strip().replace('```json','').replace('```','').strip()
                parsed = json.loads(json_str)
                instruments = parsed.get('instruments', [])
            except Exception as e:
                print('JSON parse error:', e)

        conn.close()
        return jsonify({'ok': True, 'text': text, 'instruments': instruments})

    except requests.exceptions.Timeout:
        conn.close()
        return jsonify({'ok': False, 'error': 'La IA tardó demasiado. Intentá de nuevo.'})
    except Exception as e:
        print('Exception chat:', str(e))
        conn.close()
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
