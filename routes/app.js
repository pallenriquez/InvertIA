const express = require('express');
const router = express.Router();
const fetch = require('node-fetch');
const db = require('../models/db');
const { requireAuth } = require('../middleware/auth');

router.get('/app', requireAuth, (req, res) => {
  res.sendFile('app.html', { root: './public' });
});

router.get('/upgrade', requireAuth, (req, res) => {
  res.sendFile('upgrade.html', { root: './public' });
});

router.post('/api/recommend', requireAuth, async (req, res) => {
  const user = db.prepare('SELECT * FROM users WHERE id = ?').get(req.session.userId);
  if (!user) return res.json({ ok: false, error: 'Usuario no encontrado.' });

  if (user.plan !== 'paid' && user.demo_used >= 1) {
    return res.json({ ok: false, error: 'demo_limit', message: 'Límite de demo alcanzado.' });
  }

  const { profile, profileDesc, scores, capital } = req.body;
  const name = user.name;

  const prompt = `Sos un asesor financiero argentino experto, directo y cercano. Usás el voseo. Hablás como alguien que sabe mucho pero lo explica fácil.

DATOS DEL USUARIO:
- Nombre: ${name}
- Perfil: ${profile}
- Descripción del perfil: ${profileDesc}
- Distribución: Conservador ${scores[0]}%, Moderado ${scores[1]}%, Arriesgado ${scores[2]}%
- Capital disponible para invertir: ${capital || 'no especificado'}

CONTEXTO ACTUAL DEL MERCADO ARGENTINO (junio 2026):
- Inflación mensual: ~2.3% (desacelerando)
- Dólar oficial dentro de bandas cambiarias, techo en $1757
- Merval con volatilidad por decisión del MSCI de reclasificar Argentina
- Riesgo país bajando sostenidamente
- S&P 500 en tendencia positiva traccionado por sector tecnológico (IA)
- Reservas del BCRA en recuperación
- Bonos soberanos en USD con spreads comprimiéndose

Tu respuesta debe tener DOS partes separadas por "---INSTRUMENTS---":

PARTE 1: Análisis narrativo (máximo 200 palabras)
- Saludá a ${name} por nombre y validá su perfil en 1 oración
- Contá el contexto del mercado hoy en 2-3 oraciones simples, como si hablaras con un amigo
- Con el capital de ${capital || 'su disponibilidad'}, explicá cómo distribuirías la inversión (porcentajes concretos)
- Recomendá 3 instrumentos concretos con una línea de por qué cada uno encaja con su perfil y capital
- Cerrá con una frase motivadora corta
- No uses asteriscos ni markdown

PARTE 2: Después de "---INSTRUMENTS---", respondé SOLO con un JSON válido con esta estructura exacta:
{
  "instruments": [
    {
      "name": "Nombre del instrumento (ej: Plazo Fijo UVA)",
      "category": "Renta Fija / CEDEARs / Cripto / Acciones / FCI",
      "type": "conservador | moderado | arriesgado",
      "description": "1-2 oraciones simples explicando qué es y por qué lo recomendás para este perfil y capital",
      "trend": "up | down | neutral",
      "trendNote": "Texto corto sobre la tendencia del último año (ej: Subió 180% en pesos en 12 meses)",
      "labels": ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"],
      "data": [100, 108, 115, 122, 130, 128, 135, 142, 150, 158, 165, 172],
      "unit": "$"
    }
  ],
  "macro": "1-2 oraciones sobre el contexto macro global/nacional más relevante que puede impactar estas inversiones en los próximos meses. Mencioná algo concreto: Fed, MSCI, elecciones, commodities, etc."
}
Los datos del array "data" deben ser 12 números que representen la evolución aproximada del instrumento en el último año (valores relativos o absolutos según corresponda). Usá tendencias reales conocidas.`;

  try {
    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01'
      },
      body: JSON.stringify({
        model: 'claude-sonnet-4-6',
        max_tokens: 1500,
        messages: [{ role: 'user', content: prompt }]
      })
    });

    const data = await response.json();
    const fullText = data.content?.[0]?.text || '';

    // Split narrative from instruments JSON
    const parts = fullText.split('---INSTRUMENTS---');
    const text = parts[0].trim();
    let instruments = [];
    let macro = '';

    if (parts[1]) {
      try {
        const jsonStr = parts[1].trim().replace(/```json|```/g, '').trim();
        const parsed = JSON.parse(jsonStr);
        instruments = parsed.instruments || [];
        macro = parsed.macro || '';
      } catch(e) {
        console.error('JSON parse error:', e);
      }
    }

    if (user.plan !== 'paid') {
      db.prepare('UPDATE users SET demo_used = demo_used + 1 WHERE id = ?').run(user.id);
    }

    db.prepare('INSERT INTO sessions_data (user_id, profile, recommendations) VALUES (?, ?, ?)').run(user.id, profile, text);

    const updatedUser = db.prepare('SELECT demo_used, plan FROM users WHERE id = ?').get(user.id);
    res.json({ ok: true, text, instruments, macro, demoUsed: updatedUser.demo_used, plan: updatedUser.plan });

  } catch (err) {
    console.error(err);
    res.json({ ok: false, error: 'Error al conectar con la IA. Intentá de nuevo.' });
  }
});

router.post('/api/activate-paid', requireAuth, (req, res) => {
  db.prepare('UPDATE users SET plan = ? WHERE id = ?').run('paid', req.session.userId);
  res.json({ ok: true });
});

module.exports = router;
