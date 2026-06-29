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

    data = request.json or {}
    profile = data.get('profile', '')
    scores = data.get('scores', [0,0,0])
    capital = data.get('capital', 'no especificado')
    name = user['name']

    prompt = f"""Sos un asesor financiero argentino. Usás el voseo. Breve y directo.

Usuario: {name}, perfil {profile}, capital {capital}.
Distribución: Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%.
Mercado actual (jun 2026): inflación ~2.3% mensual, dólar en bandas ($1757 techo), Merval volátil por MSCI, S&P500 alcista por IA, riesgo país bajando.

Respondé en DOS partes separadas por ---INSTRUMENTS---

PARTE 1 (máximo 150 palabras, sin asteriscos):
Saludá a {name}, validá su perfil, contá brevemente el mercado, recomendá 3 instrumentos concretos con porcentajes según capital {capital}, cerrá con frase motivadora.

PARTE 2: solo JSON válido:
{{"instruments":[{{"name":"nombre","category":"tipo","type":"conservador|moderado|arriesgado","description":"1 oración","trend":"up|down|neutral","labels":["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"],"data":[100,105,110,108,115,120,118,125,130,128,135,140],"unit":"$"}}],"macro":"1 oración sobre riesgo macro relevante."}}
Incluí 3 instrumentos en el array. Los datos deben reflejar tendencias reales del último año."""

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_KEY,
                'anthropic-version': '2023-06-01'
            },
            json={
                'model': 'claude-haiku-4-5-20251001',
                'max_tokens': 1200,
                'messages': [{'role': 'user', 'content': prompt}]
            },
            timeout=55
        )
        result = resp.json()

        if 'error' in result:
            print('Anthropic API error:', result['error'])
            conn.close()
            return jsonify({'ok': False, 'error': 'Error en la API: ' + str(result['error'].get('message',''))})

        full_text = result.get('content', [{}])[0].get('text', '')
        print('API response ok, length:', len(full_text))

        parts = full_text.split('---INSTRUMENTS---')
        text = parts[0].strip()
        instruments = []
        macro = ''

        if len(parts) > 1:
            try:
                json_str = parts[1].strip().replace('```json','').replace('```','').strip()
                parsed = json.loads(json_str)
                instruments = parsed.get('instruments', [])
                macro = parsed.get('macro', '')
            except Exception as e:
                print('JSON parse error:', e)

        if user['plan'] != 'paid':
            conn.execute('UPDATE users SET demo_used = demo_used + 1 WHERE id=?', (session['user_id'],))
            conn.commit()

        conn.execute('INSERT INTO history (user_id, profile, recommendations) VALUES (?,?,?)',
                     (session['user_id'], profile, text))
        conn.commit()
        updated = conn.execute('SELECT demo_used, plan FROM users WHERE id=?', (session['user_id'],)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'text': text, 'instruments': instruments, 'macro': macro,
                        'demoUsed': updated['demo_used'], 'plan': updated['plan']})

    except requests.exceptions.Timeout:
        print('Timeout calling Anthropic API')
        conn.close()
        return jsonify({'ok': False, 'error': 'La IA tardó demasiado. Intentá de nuevo.'})
    except Exception as e:
        print('Exception in /api/recommend:', str(e))
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
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=False)
