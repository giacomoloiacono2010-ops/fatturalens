# FatturaLens

Trasforma fatture PDF in Excel in 10 secondi con AI. SaaS per commercialisti italiani.

## Tech Stack

- **Backend:** Python Flask + SQLite + pdfplumber + OpenRouter AI
- **Frontend:** HTML/CSS/JS vanilla (dark mode)
- **Payments:** Stripe
- **Email:** Resend (magic link login)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # configura le API key
python app.py
```

## Struttura

- `app.py` — backend Flask completo
- `index.html` — dashboard frontend
- `landing.html` — landing page marketing
