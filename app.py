from flask import Flask, request, session, jsonify, send_from_directory, redirect
from flask_session import Session
import sqlite3, bcrypt, requests, os, json, re
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
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            currency TEXT DEFAULT 'USD',
            status TEXT DEFAULT 'paid',
            plan TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
    ''')
    for col in ['objetivo_monto REAL','objetivo_plazo_meses INTEGER','objetivo_plazo_deseado_meses INTEGER','capital_mensual REAL']:
        try: conn.execute(f'ALTER TABLE user_profile ADD COLUMN {col}')
        except: pass
    conn.commit()
    conn.close()

init_db()

def call_claude(messages, system, model='claude-haiku-4-5-20251001', max_tokens=1000):
    resp = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={'Content-Type':'application/json','x-api-key':ANTHROPIC_KEY,'anthropic-version':'2023-06-01'},
        json={'model':model,'max_tokens':max_tokens,'system':system,'messages':messages},
        timeout=55
    )
    return resp.json()

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
@app.route('/upgrade')
def upgrade_page():
    if not session.get('user_id'): return redirect('/login')
    return send_from_directory('public','upgrade.html')
@app.route('/logout')
def logout(): session.clear(); return redirect('/')

# AUTH
@app.route('/register', methods=['POST'])
def register():
    d = request.json or {}
    name,email,pw = d.get('name','').strip(),d.get('email','').strip().lower(),d.get('password','')
    if not name or not email or not pw: return jsonify({'ok':False,'error':'Completá todos los campos.'})
    if len(pw)<6: return jsonify({'ok':False,'error':'La contraseña debe tener al menos 6 caracteres.'})
    conn = get_db()
    if conn.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone():
        conn.close(); return jsonify({'ok':False,'error':'Ya existe una cuenta con ese email.'})
    hashed = bcrypt.hashpw(pw.encode(),bcrypt.gensalt()).decode()
    cur = conn.execute('INSERT INTO users (name,email,password) VALUES (?,?,?)',(name,email,hashed))
    conn.commit(); session['user_id']=cur.lastrowid; session['user_name']=name; conn.close()
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

@app.route('/me')
def me():
    if not session.get('user_id'): return jsonify({'loggedIn':False})
    conn = get_db()
    user = conn.execute('SELECT id,name,email,plan,demo_used,created_at FROM users WHERE id=?',(session['user_id'],)).fetchone()
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
        }
    if fin:
        result['financialData'] = {'ingresos':fin['ingresos'],'egresos':fin['egresos'],'categorias':json.loads(fin['categorias']) if fin['categorias'] else {}}
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
    prompt = (f"Sos un asesor financiero argentino experto. Usas el voseo. Directo, profesional y claro. Sin asteriscos ni markdown.\n"
        f"El usuario se llama {name}, perfil {profile} (Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%).\n"
        f"Mercado julio 2026: inflacion ~2% mensual bajando, dolar estable en bandas (~1700 ARS/USD), bolsa AR volatil, S&P500 alcista por tech, riesgo pais bajando.\n\n"
        f"Responde:\n- 1 oracion saludando a {name} y validando su perfil\n- 2 oraciones sobre el mercado hoy\n"
        f"- 3 instrumentos numerados con nombre y por que encaja con su perfil\nMaximo 160 palabras. Sin preguntas al final.")
    try:
        result = call_claude([{'role':'user','content':prompt}],'',model='claude-haiku-4-5-20251001',max_tokens=600)
        text = result.get('content',[{}])[0].get('text','').strip()
        if user['plan'] not in ('paid','advanced'):
            conn.execute('UPDATE users SET demo_used=demo_used+1 WHERE id=?',(session['user_id'],))
            conn.commit()
        save_msg(session['user_id'],'assistant',text)
        updated = conn.execute('SELECT demo_used,plan FROM users WHERE id=?',(session['user_id'],)).fetchone()
        conn.close()
        return jsonify({'ok':True,'text':text,'demoUsed':updated['demo_used'],'plan':updated['plan']})
    except Exception as e:
        conn.close(); return jsonify({'ok':False,'error':str(e)})

CHART_SYSTEM_SUFFIX = """

===== INSTRUCCION OBLIGATORIA SOBRE GRAFICOS =====

CUANDO PRESENTAS UNA CARTERA CON 2+ INSTRUMENTOS, la respuesta DEBE terminar con:

---CHART---
{"instruments":[...], "objetivoUpdate":{...}}

NO es opcional. Si presentas instrumentos con porcentajes y NO incluís ---CHART---, la respuesta es incompleta.

FORMATO EXACTO (copiar estructura):
---CHART---
{"instruments":[{"name":"CEDEARs S&P500","pct":45,"description":"Acciones tech via CEDEAR en dolares","trend":"up","trendNote":"+12% anual","labels":["2026","2027","2027","2028","2028","2029"],"data":[1000,1120,1254,1405,1574,1763]},{"name":"Acciones AR","pct":35,"description":"Bolsa argentina con upside local","trend":"up","trendNote":"+15% anual","labels":["2026","2027","2027","2028","2028","2029"],"data":[1000,1150,1322,1521,1749,2011]},{"name":"Bonos USD","pct":20,"description":"Renta fija en dolares, ancla de cartera","trend":"up","trendNote":"+7% anual","labels":["2026","2027","2027","2028","2028","2029"],"data":[1000,1070,1145,1225,1311,1403]}],"objetivoUpdate":{"monto":10000,"plazoMeses":30,"plazoDeseadoMeses":30,"capitalMensual":294}}

REGLAS:
- pct debe sumar exactamente 100
- Minimo 2 instrumentos con nombres reales (no "Proyeccion acumulada")
- data: rendimiento proyectado del instrumento empezando en 1000
- objetivoUpdate: siempre incluir cuando tenes monto + plazo + capital confirmados
- El separador ---CHART--- va en su propia linea, despues del texto

=====
"""

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

    capital_usd = capital
    if capital:
        # Remove thousand separators (dots used in Argentine format: 1.000.000)
        cleaned = re.sub(r'\.(?=\d{3})', '', capital)  # remove dots used as thousands separator
        cleaned = cleaned.replace(',', '.')  # convert comma decimal to dot
        nums = re.findall(r'[\d.]+', cleaned)
        try:
            num = float(nums[0]) if nums else 0
        except (ValueError, IndexError):
            num = 0
        if any(w in capital.lower() for w in ['peso','ars','pesos','argentino',' p ']):
            capital_usd = f"{capital} (~USD {num/1700:,.0f})"

    high_ticket = any(w in objetivo.lower() for w in ['casa','departamento','auto','viaje','retiro','jubilacion','inmueble']) if objetivo else False
    history = get_history(session['user_id'],limit=12)

    ctx = []
    if capital_usd: ctx.append(f"Capital mensual disponible: {capital_usd}")
    if objetivo: ctx.append(f"Objetivo: {objetivo}")
    if obj_monto: ctx.append(f"Monto objetivo: USD {obj_monto:,.0f}")
    if obj_plazo: ctx.append(f"Plazo proyectado: {obj_plazo} meses")
    if cap_mensual: ctx.append(f"Capital mensual a invertir: USD {cap_mensual:,.0f}")

    system = (
        f"INSTRUCCION CRITICA: Cuando presentes una cartera con instrumentos y porcentajes, SIEMPRE termina tu respuesta con ---CHART--- seguido del JSON. Sin excepcion.\n\n"
        f"Sos un asesor financiero argentino experto y personal. Voseo. Directo, profesional. Sin asteriscos ni markdown.\n"
        f"Perfil: {profile}. Conservador {scores[0]}%, Moderado {scores[1]}%, Arriesgado {scores[2]}%.\n"
        + ('\n'.join(ctx)+'\n' if ctx else '')
        + ("IMPORTANTE: objetivo de alto valor, siempre en USD.\n" if high_ticket else "")
        + ("Plan Advanced: acceso a control financiero personal.\n" if is_advanced else
           "Plan Pro: si pregunta por control financiero/gastos/presupuesto, decile que es parte del plan Advanced.\n")
        + "Mercado julio 2026: inflacion ~2% mensual, dolar estable ~1700 ARS/USD, bolsa AR volatil, S&P500 alcista, riesgo pais bajando.\n\n"
        "FLUJO:\n"
        "1. Si menciona objetivo (casa/auto/viaje/retiro) y no tenemos plazo → dar contexto del plazo habitual y preguntar cuando lo quiere:\n"
        "   - Casa/depto: 'Para una vivienda lo habitual es planificar entre 5 y 15 anos. ¿Vos para cuando lo tenias en mente?'\n"
        "   - Auto: 'Un auto generalmente se planifica entre 1 y 3 anos. ¿Cuando lo querias tener?'\n"
        "   - Viaje: 'Un viaje suele planificarse entre 6 meses y 2 anos. ¿Cuando lo tenias pensado?'\n"
        "   - Retiro: 'El retiro suele planificarse a 10-30 anos. ¿En que horizonte estas pensando?'\n"
        "2. Cuando tenes capital + objetivo + plazo → calculá si es realista, si no alcanza sugerir aumentar inversion O estirar plazo\n"
        "3. Si acepta aumentar inversion → mostrar proyeccion comparativa con data2 en el JSON\n"
        "4. Cuando alineas expectativas → presentar cartera personalizada CON grafico obligatorio\n"
        "5. Despues de presentar la cartera → siempre cerrar con UNA pregunta concreta: '¿Querés que te explique cómo empezar con alguno de estos instrumentos?' o '¿Tenés alguna duda sobre la distribución?'\n"
        + CHART_SYSTEM_SUFFIX
    )

    msgs = [{'role':h['role'],'content':h['content']} for h in history if h.get('role') and h.get('content')]
    msgs.append({'role':'user','content':message})

    try:
        result = call_claude(msgs,system,model='claude-sonnet-4-6',max_tokens=3000)
        if 'error' in result: conn.close(); return jsonify({'ok':False,'error':str(result['error'])})
        full = result.get('content',[{}])[0].get('text','').strip()
        parts = full.split('---CHART---')
        text = parts[0].strip()
        instruments,obj_update,fin_update = [],[],None

        if len(parts)>1:
            try:
                js = parts[1].strip().replace('```json','').replace('```','').strip()
                parsed = json.loads(js)
                instruments = parsed.get('instruments',[])
                obj_update = parsed.get('objetivoUpdate')
                fin_update = parsed.get('financialUpdate')
            except Exception as e:
                print('Chart JSON parse error:',e,parts[1][:100])

        if obj_update:
            conn.execute('''INSERT INTO user_profile (user_id,profile_key,profile_label,scores,capital,objetivo,objetivo_monto,objetivo_plazo_meses,objetivo_plazo_deseado_meses,capital_mensual,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                objetivo_monto=COALESCE(excluded.objetivo_monto,objetivo_monto),
                objetivo_plazo_meses=COALESCE(excluded.objetivo_plazo_meses,objetivo_plazo_meses),
                objetivo_plazo_deseado_meses=COALESCE(excluded.objetivo_plazo_deseado_meses,objetivo_plazo_deseado_meses),
                capital_mensual=COALESCE(excluded.capital_mensual,capital_mensual),updated_at=excluded.updated_at''',
                (session['user_id'],profile,profile,json.dumps(scores),capital,objetivo,
                 obj_update.get('monto'),obj_update.get('plazoMeses'),obj_update.get('plazoDeseadoMeses'),
                 obj_update.get('capitalMensual'),datetime.now().isoformat()))
            conn.commit()

        if fin_update and is_advanced:
            conn.execute('''INSERT INTO financial_data (user_id,ingresos,egresos,categorias,updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET ingresos=COALESCE(excluded.ingresos,ingresos),
                egresos=COALESCE(excluded.egresos,egresos),categorias=COALESCE(excluded.categorias,categorias),updated_at=excluded.updated_at''',
                (session['user_id'],fin_update.get('ingresos'),fin_update.get('egresos'),
                 json.dumps(fin_update.get('categorias',{})),datetime.now().isoformat()))
            conn.commit()

        save_msg(session['user_id'],'user',message)
        save_msg(session['user_id'],'assistant',text)
        conn.close()
        return jsonify({'ok':True,'text':text,'instruments':instruments,'objetivoUpdate':obj_update,'financialUpdate':fin_update})
    except Exception as e:
        print('Chat error:',e); conn.close(); return jsonify({'ok':False,'error':str(e)})

@app.route('/api/returning-greeting', methods=['POST'])
def returning_greeting():
    if not session.get('user_id'): return jsonify({'ok':False})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    prof = conn.execute('SELECT * FROM user_profile WHERE user_id=?',(session['user_id'],)).fetchone()
    conn.close()
    if not prof: return jsonify({'ok':False})
    days = days_since(user['id'])
    returning = f"Vuelve despues de {days} dias. Preguntale como le fue con sus inversiones y si quiere actualizar el plan." if days and days>=25 else ""
    prompt = (f"Sos el asesor personal de {user['name']}. Voseo. Sin asteriscos.\n"
        f"Perfil: {prof['profile_label']}. Capital: {prof['capital']}. Objetivo: {prof['objetivo']}.\n{returning}\n"
        f"Dale bienvenida personalizada mencionando algo de su contexto. {'Preguntale como le fue con sus inversiones el ultimo mes.' if days and days>=25 else 'Preguntale en que lo podes ayudar hoy.'} Maximo 2 oraciones.")
    try:
        result = call_claude([{'role':'user','content':prompt}],'',model='claude-haiku-4-5-20251001',max_tokens=150)
        text = result.get('content',[{}])[0].get('text','').strip()
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
        text = result.get('content',[{}])[0].get('text','').strip()
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

@app.route('/api/activate-paid', methods=['POST'])
def activate_paid():
    if not session.get('user_id'): return jsonify({'ok':False})
    d = request.json or {}
    plan = d.get('plan','paid')
    if plan not in ('paid','advanced'): plan = 'paid'
    conn = get_db()
    conn.execute('UPDATE users SET plan=? WHERE id=?',(plan,session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT',3000))
    app.run(host='0.0.0.0',port=port,debug=False)
