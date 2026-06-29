from flask import Flask, request, session, jsonify, send_from_directory, redirect
from flask_session import Session
import sqlite3, bcrypt, requests, os

app = Flask(__name__, static_folder='public')
app.secret_key = os.environ.get('SESSION_SECRET', 'invertia-dev-secret-2026')
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = '/tmp/flask_sessions'
os.makedirs('/tmp/flask_sessions', exist_ok=True)
Session(app)

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_PATH = '/tmp/invertia.db'

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
    if session.get('user_id'):
        return redirect('/app')
    return send_from_directory('public', 'register.html')

@app.route('/login')
def login_page():
    if session.get('user_id'):
        return redirect('/app')
    return send_from_directory('public', 'login.html')

@app.route('/app')
def app_page():
    if not session.get('user_id'):
        return redirect('/login')
    return send_from_directory('public', 'app.html')

@app.route('/upgrade')
def upgrade_page():
    if not session.get('user_id'):
        return redirect('/login')
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
    if not session.get('user_id'):
        return jsonify({'loggedIn': False})
    conn = get_db()
    user = conn.execute('SELECT id, name, email, plan, demo_used FROM users WHERE id=?', (session['user_id'],)).fetchone()
    conn.close()
    if not user:
        return jsonify({'loggedIn': False})
    return jsonify({'loggedIn': True, 'user': dict(user)})

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
    data = request.json
    profile = data.get('profile', '')
    profile_desc = data.get('profileDesc', '')
    scores = data.get('scores', [0,0,0])
    name = user['name']
    prompt = f"""Sos un asesor financiero argentino, cercano y profesional. Hablás de forma cálida y directa, usando el voseo. Usás frases como "Mirá {name}", "Lo que te sugiero es...", "Fijate que...", "Te cuento que...".

El usuario se llama "{name}" y obtuvo el perfil inversor: "{profile}".
Descripción: {profile_desc}
Distribución: Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%.

Con el contexto actual del mercado argentino (inflación ~2.3% mensual, dólar dentro de bandas cambiarias con techo en $1757, Merval con volatilidad reciente, S&P 500 en tendencia positiva), generá una recomendación personalizada, cálida y profesional.

Estructura:
- Saludá por nombre y validá su perfil en 1 oración
- Contá cómo viene el mercado hoy en 2-3 oraciones
- Recomendá 3 activos concretos explicando brevemente cada uno
- Cerrá con frase motivadora y aclaración de que es info educativa

Máximo 220 palabras. Sin asteriscos ni markdown. Usá saltos de línea entre secciones."""
    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-sonnet-4-6', 'max_tokens': 1000, 'messages': [{'role': 'user', 'content': prompt}]},
            timeout=30
        )
        result = resp.json()
        text = result.get('content', [{}])[0].get('text', 'No se pudo generar la recomendación.')
        if user['plan'] != 'paid':
            conn.execute('UPDATE users SET demo_used = demo_used + 1 WHERE id=?', (session['user_id'],))
            conn.commit()
        conn.execute('INSERT INTO history (user_id, profile, recommendations) VALUES (?,?,?)', (session['user_id'], profile, text))
        conn.commit()
        updated = conn.execute('SELECT demo_used, plan FROM users WHERE id=?', (session['user_id'],)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'text': text, 'demoUsed': updated['demo_used'], 'plan': updated['plan']})
    except Exception as e:
        conn.close()
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/activate-paid', methods=['POST'])
def activate_paid():
    if not session.get('user_id'):
        return jsonify({'ok': False})
    conn = get_db()
    conn.execute("UPDATE users SET plan='paid' WHERE id=?", (session['user_id'],))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
