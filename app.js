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

  const { profile, profileDesc, scores } = req.body;
  const name = user.name;

  const prompt = `Sos un asesor financiero argentino, cercano y profesional. Hablás de forma cálida y directa, usando el voseo. Usás frases como "Mirá ${name}", "Lo que te sugiero es...", "Fijate que...", "Te cuento que...".

El usuario se llama "${name}" y obtuvo el perfil inversor: "${profile}".
Descripción: ${profileDesc}
Distribución: Conservador ${scores[0]}%, Moderado ${scores[1]}%, Arriesgado ${scores[2]}%.

Con el contexto actual del mercado argentino (inflación ~2.3% mensual, dólar dentro de bandas cambiarias con techo en $1757, Merval con volatilidad reciente, S&P 500 en tendencia positiva por sector tecnológico), generá una recomendación personalizada, cálida y profesional.

Estructura la respuesta así:
- Arrancá saludando por nombre y validando su perfil en 1 oración
- Contá brevemente cómo viene el mercado hoy (2-3 oraciones, como si se lo contaras a un amigo)
- Recomendá 3 activos o instrumentos concretos que encajan con su perfil, explicando brevemente por qué cada uno
- Cerrá con una frase motivadora y recordá que es info educativa

Máximo 220 palabras. No uses asteriscos ni markdown. Usá saltos de línea para separar secciones.`;

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
        max_tokens: 1000,
        messages: [{ role: 'user', content: prompt }]
      })
    });

    const data = await response.json();
    const text = data.content?.[0]?.text || 'No se pudo generar la recomendación.';

    if (user.plan !== 'paid') {
      db.prepare('UPDATE users SET demo_used = demo_used + 1 WHERE id = ?').run(user.id);
    }

    db.prepare('INSERT INTO sessions_data (user_id, profile, recommendations) VALUES (?, ?, ?)').run(user.id, profile, text);

    const updatedUser = db.prepare('SELECT demo_used, plan FROM users WHERE id = ?').get(user.id);
    res.json({ ok: true, text, demoUsed: updatedUser.demo_used, plan: updatedUser.plan });

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
