const express = require('express');
const router = express.Router();
const bcrypt = require('bcryptjs');
const db = require('../models/db');

router.get('/register', (req, res) => {
  if (req.session.userId) return res.redirect('/app');
  res.sendFile('register.html', { root: './public' });
});

router.post('/register', async (req, res) => {
  const { name, email, password } = req.body;
  if (!name || !email || !password)
    return res.json({ ok: false, error: 'Completá todos los campos.' });

  const existing = db.prepare('SELECT id FROM users WHERE email = ?').get(email);
  if (existing)
    return res.json({ ok: false, error: 'Ya existe una cuenta con ese email.' });

  const hash = await bcrypt.hash(password, 10);
  const result = db.prepare('INSERT INTO users (name, email, password) VALUES (?, ?, ?)').run(name, email, hash);
  req.session.userId = result.lastInsertRowid;
  req.session.userName = name;
  res.json({ ok: true });
});

router.get('/login', (req, res) => {
  if (req.session.userId) return res.redirect('/app');
  res.sendFile('login.html', { root: './public' });
});

router.post('/login', async (req, res) => {
  const { email, password } = req.body;
  if (!email || !password)
    return res.json({ ok: false, error: 'Completá todos los campos.' });

  const user = db.prepare('SELECT * FROM users WHERE email = ?').get(email);
  if (!user)
    return res.json({ ok: false, error: 'Email o contraseña incorrectos.' });

  const valid = await bcrypt.compare(password, user.password);
  if (!valid)
    return res.json({ ok: false, error: 'Email o contraseña incorrectos.' });

  req.session.userId = user.id;
  req.session.userName = user.name;
  res.json({ ok: true });
});

router.get('/logout', (req, res) => {
  req.session.destroy();
  res.redirect('/');
});

router.get('/me', (req, res) => {
  if (!req.session.userId) return res.json({ loggedIn: false });
  const user = db.prepare('SELECT id, name, email, plan, demo_used FROM users WHERE id = ?').get(req.session.userId);
  res.json({ loggedIn: true, user });
});

module.exports = router;
