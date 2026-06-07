# EstrattoreFatture / FatturaLens — struttura progetto

Progetto: app web per estrarre dati da fatture PDF → Excel.
Stack: Flask + HTML/CSS vanilla.

## File principali

| File | Cosa fa |
|------|---------|
| `app.py` | Server Flask. Gestisce auth, upload PDF, estrazione AI (OpenRouter), export Excel, pagamenti Stripe. Backend principale. |
| `index.html` | Landing page / homepage marketing completa. Single-page: hero, problema, come funziona, vantaggi, prezzi, CTA, footer. CSS tutto inline o in <style>. |
| `landing.html` | Landing alternativa (versione più semplice/svelta). |
| `privacy.html` | Pagina Privacy Policy. |
| `terms.html` | Pagina Termini di servizio. |
| `templates/email_welcome.html` | Template email di benvenuto (dopo registrazione). |
| `templates/email_ready.html` | Template email "fattura pronta" (notifica download). |

## Come sono collegati

- `index.html` → link a `#funzionalita`, `#prezzi` (ancore interne), `/app` (dashboard), `/privacy`, `/termini`
- `app.py` → serve le pagine HTML, gestisce API upload, Stripe webhook, login/register
- Stripe: 3 prodotti (Free 0€, Base 29€, Pro 49€) con price_id mappati a piani

## Palette colori (index.html)

- Sfondo pagina: #fafbfc
- Testo: #0a192f | secondario: #334e68
- CTA: #f97316 (arancio) | hover: #ea580c
- Gradiente hero: #0c4a6e → #0a3d5c (blu scuro)
- Azzurro chiaro: #93c5fd / #bfdbfe
- Verde check: #22c55e
- Sezioni alternate: #eef2f7 / #fafbfc

## Da sapere per un'altra AI

- L'AI attuale ha scritto solo `index.html`. Gli altri file (app.py, landing.html, privacy.html, terms.html, templates/) esistono già da sessioni precedenti.
- `index.html` è un file standalone (no framework, no build). Si apre direttamente nel browser.
- Il server non è in esecuzione adesso. Per testare serve avviare `python app.py`.
- La repo è su GitHub ma non serve toccarla per modifiche locali a index.html.
