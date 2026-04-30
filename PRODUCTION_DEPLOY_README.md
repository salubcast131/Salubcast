# SalubCast Production Package

Deze package is bedoeld voor online deployment.

Belangrijk:
- de app draait niet op `127.0.0.1` als vaste bind-host
- de runner gebruikt `0.0.0.0`
- zet altijd een publieke URL in `SALUBCAST_PUBLIC_BASE_URL`
- gebruik in productie `https`

Minimaal instellen:

```env
SALUBCAST_SECRET=vervang-met-lange-random-secret
SALUBCAST_PUBLIC_BASE_URL=https://jouwdomein.nl
SALUBCAST_SESSION_SECURE=1
SALUBCAST_PREFERRED_URL_SCHEME=https
PORT=5000
```

Start lokaal/VM/server:

```bash
python salubcast_MASTER_news_weather_pro_LINUX.py
```

Of via gunicorn:

```bash
gunicorn wsgi:app --bind 0.0.0.0:5000
```

Inhoud van deze production-package:
- applicatiecode
- database
- uploads
- branding
- deploymentbestanden

Niet meegenomen:
- `__pycache__`
- losse oude tools en `.exe` bestanden
- lokale player build-rommel die niet nodig is voor hosting
