# SalubCast Live SaaS

## Doel

Deze setup is bedoeld voor een eerste betaalbare SaaS-versie van SalubCast:
- Flask app via Gunicorn
- Render als hosting
- persistent disk voor SQLite en uploads
- Cloudflare voor domein en SSL
- Stripe voor abonnementen en facturatie

## 1. Code online zetten

Zet deze map in een Git-repository en push naar GitHub.

Bestanden voor deployment:
- `requirements.txt`
- `Procfile`
- `wsgi.py`
- `render.yaml`
- `.env.example`

## 2. Deployen op Render

1. Maak een nieuwe Render Web Service.
2. Koppel je GitHub-repo.
3. Gebruik Python.
4. Laat Render `render.yaml` lezen, of gebruik handmatig:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn --bind 0.0.0.0:$PORT wsgi:app`
5. Voeg een persistent disk toe op `/var/data/salubcast`.

## 3. Verplichte environment variables

Minimaal:
- `SALUBCAST_SECRET`
- `SALUBCAST_PUBLIC_BASE_URL`
- `SALUBCAST_BOOTSTRAP_ADMIN_PASSWORD`
- `SALUBCAST_BOOTSTRAP_ADMIN_EMAIL`
- `SALUBCAST_BOOTSTRAP_ADMIN_NAME`
- `SALUBCAST_HEALTH_TOKEN`

Voor Render met disk:
- `SALUBCAST_DATA_DIR=/var/data/salubcast`
- `SALUBCAST_UPLOAD_DIR=/var/data/salubcast/uploads_clean`
- `SALUBCAST_DB_PATH=/var/data/salubcast/salubcast_v4_clean.db`

## 4. Eerste login

Na de eerste deploy:
1. Open je domein.
2. Log in via `/login`.
3. Gebruik het bootstrap admin-account uit je environment variables.

## 5. Cloudflare

Aanbevolen:
- DNS via Cloudflare
- SSL/TLS mode: `Full (strict)`
- altijd HTTPS afdwingen

## 6. Stripe verkoopflow

Aanbevolen commerciële flow:
1. Maak prijsplannen in Stripe:
   - Starter
   - Professional
   - Enterprise
2. Gebruik Stripe Checkout voor nieuwe abonnementen.
3. Gebruik Stripe Customer Portal voor abonnementbeheer.
4. Verwerk Stripe webhooks later in SalubCast voor:
   - trial -> active
   - active -> past_due
   - canceled -> blocked

De database heeft nu al basisvelden in `companies`:
- `plan_name`
- `billing_status`
- `trial_ends_at`
- `billing_email`
- `stripe_customer_id`
- `stripe_subscription_id`

## 7. Praktisch verkoopmodel

Eenvoudige start:
- Starter: 1-3 schermen
- Professional: 4-20 schermen
- Enterprise: maatwerk

Slimste model:
- maandbedrag per bedrijf
- plus meerprijs per extra scherm
- plus onboarding/installatie als losse dienst

## 8. Wat nog handmatig moet

Nog niet geautomatiseerd in code:
- wachtwoord reset via e-mail
- object storage voor media
- geautomatiseerde backups
- Postgres-migratie

## 9. Aanbevolen volgende stap

Volgende technische upgrade:
1. Stripe prijs-ID's en webhook secret invullen
2. periodieke backups
3. Postgres in plaats van SQLite
4. object storage voor uploads

## 10. Stripe in deze code

Aanwezige routes:
- `/billing`
- `/billing/checkout/<plan>`
- `/billing/portal`
- `/webhooks/stripe`

Hiervoor moet je in Stripe instellen:
- 3 recurring Prices
- webhook naar `https://jouwdomein.nl/webhooks/stripe`
- optioneel een Customer Portal configuration
