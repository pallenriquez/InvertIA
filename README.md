# InvertIA 🚀

Asesor financiero con IA para el mercado argentino.

## Estructura del proyecto

```
invertia/
├── server.js              # Servidor principal
├── package.json
├── .env.example           # Copiá esto a .env
├── public/
│   ├── index.html         # Landing page
│   ├── register.html      # Registro
│   ├── login.html         # Login
│   ├── app.html           # App del test
│   └── upgrade.html       # Página de pago
├── routes/
│   ├── auth.js            # Registro, login, logout
│   └── app.js             # Test, recomendaciones
├── models/
│   └── db.js              # Base de datos SQLite
└── middleware/
    └── auth.js            # Protección de rutas
```

## Instalación paso a paso

### 1. Instalar Node.js
Descargá Node.js desde https://nodejs.org (versión 18 o superior)

### 2. Instalar dependencias
Abrí una terminal en la carpeta del proyecto y ejecutá:
```bash
npm install
```

### 3. Configurar variables de entorno
Copiá el archivo de ejemplo:
```bash
cp .env.example .env
```

Abrí el archivo `.env` y completá:
```
ANTHROPIC_API_KEY=tu-api-key-de-anthropic
SESSION_SECRET=cualquier-texto-random-largo
PORT=3000
```

### 4. Levantar el servidor
```bash
npm start
```

O en modo desarrollo (se reinicia automáticamente al editar):
```bash
npm run dev
```

### 5. Abrir en el navegador
Entrá a: http://localhost:3000

---

## Deploy en Render (gratis para empezar)

1. Subí el proyecto a GitHub
2. Entrá a https://render.com y creá una cuenta
3. Nuevo proyecto → Web Service → conectá tu repo de GitHub
4. Configuración:
   - Build Command: `npm install`
   - Start Command: `npm start`
5. En "Environment Variables" agregá:
   - `ANTHROPIC_API_KEY` = tu clave
   - `SESSION_SECRET` = texto random
6. Deploy!

---

## Integrar pagos con MercadoPago

En `public/upgrade.html`, la función `activatePaid()` actualmente simula el pago.
Para integrarlo con MercadoPago:

1. Creá una cuenta en https://www.mercadopago.com.ar/developers
2. Obtenés tu Access Token
3. Instalá el SDK: `npm install mercadopago`
4. Creás una preferencia de pago en el backend
5. Redirigís al usuario a la URL de pago de MercadoPago
6. Configurás el webhook para que cuando se apruebe el pago, llame a `/api/activate-paid`

---

## Funcionalidades incluidas

- ✅ Landing page con hero, cómo funciona, perfiles, precios y testimonios
- ✅ Registro e inicio de sesión con contraseña encriptada
- ✅ Sesiones persistentes con SQLite
- ✅ Test de 7 preguntas con cálculo de perfil
- ✅ Recomendaciones personalizadas via Claude API
- ✅ Demo limitada a 1 uso por usuario
- ✅ Página de upgrade al plan Pro
- ✅ Control de plan (free vs paid) en base de datos

## Notas importantes

- El archivo `.env` NUNCA se sube a GitHub (está en .gitignore)
- La base de datos `database.db` se crea automáticamente al iniciar
- Para producción, considerá migrar a PostgreSQL
