import os
import re
import json
import uuid
import sqlite3
import shutil
import time
import logging
import threading
from logging.handlers import RotatingFileHandler
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
import pdfplumber
import stripe
import pandas as pd

load_dotenv()

app = Flask(__name__)
CORS(app)

# ---- LOGGING ----
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
log_handler = RotatingFileHandler('app.log', maxBytes=10*1024*1024, backupCount=3)
log_handler.setFormatter(log_formatter)
logger = logging.getLogger('fatturalens')
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# ---- RATE LIMITING ----
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ---- CONFIG ----
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
RESEND_API_KEY = os.getenv('RESEND_API_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'https://tuo-dominio.replit.app')

DB_PATH = 'fatturalens.db'
UPLOAD_FOLDER = 'uploads'
EXCEL_FOLDER = 'excels'
BACKUP_FOLDER = 'backups'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXCEL_FOLDER, exist_ok=True)
os.makedirs(BACKUP_FOLDER, exist_ok=True)

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

# ---- DATABASE ----
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS utenti (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            token TEXT,
            piano TEXT DEFAULT 'free',
            limite_mensile INTEGER DEFAULT 5,
            fatture_processate_mese INTEGER DEFAULT 0,
            reset_mese TEXT DEFAULT '',
            stripe_customer_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS fatture (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            utente_id INTEGER NOT NULL,
            nome_file TEXT,
            json_estratto TEXT,
            excel_path TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (utente_id) REFERENCES utenti(id)
        );
    ''')
    conn.commit()
    # Monthly reset
    today = date.today()
    this_month = today.strftime('%Y-%m')
    conn.execute(
        "UPDATE utenti SET fatture_processate_mese = 0 WHERE reset_mese != ?",
        (this_month,)
    )
    conn.execute(
        "UPDATE utenti SET reset_mese = ? WHERE reset_mese != ?",
        (this_month, this_month)
    )
    conn.commit()
    conn.close()

init_db()

# ---- BACKUP DAEMON ----
def do_backup():
    try:
        backup_date = date.today().strftime('%Y-%m-%d')
        backup_path = os.path.join(BACKUP_FOLDER, f'fatturalens_{backup_date}.db')
        if not os.path.exists(backup_path):
            shutil.copy2(DB_PATH, backup_path)
            logger.info(f"BACKUP|created {backup_path}")
        # Keep only last 7
        backups = sorted(
            [f for f in os.listdir(BACKUP_FOLDER) if f.startswith('fatturalens_') and f.endswith('.db')],
            reverse=True
        )
        for old in backups[7:]:
            os.remove(os.path.join(BACKUP_FOLDER, old))
            logger.info(f"BACKUP|pruned {old}")
    except Exception as e:
        logger.error(f"BACKUP|failed: {e}")

def backup_daemon():
    last_backup_date = None
    while True:
        now = datetime.now()
        today_str = date.today().isoformat()
        if last_backup_date != today_str and now.hour == 3:
            do_backup()
            last_backup_date = today_str
        time.sleep(60)

t = threading.Thread(target=backup_daemon, daemon=True)
t.start()

# ---- HELPERS ----
def get_or_create_user(email):
    conn = get_db()
    user = conn.execute("SELECT * FROM utenti WHERE email = ?", (email,)).fetchone()
    if user is None:
        conn.execute(
            "INSERT INTO utenti (email, piano, limite_mensile) VALUES (?, 'free', 5)",
            (email,)
        )
        conn.commit()
        user = conn.execute("SELECT * FROM utenti WHERE email = ?", (email,)).fetchone()
    else:
        today = date.today()
        this_month = today.strftime('%Y-%m')
        if user['reset_mese'] != this_month:
            conn.execute(
                "UPDATE utenti SET fatture_processate_mese = 0, reset_mese = ? WHERE email = ?",
                (this_month, email)
            )
            conn.commit()
            user = conn.execute("SELECT * FROM utenti WHERE email = ?", (email,)).fetchone()
    conn.close()
    return user

def extract_pdf_text(pdf_path):
    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception:
        return None
    return text.strip()

def to_float(val):
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace(',', '.').strip()
    try:
        return float(s)
    except ValueError:
        return 0

def postprocess_json(parsed):
    result = {}

    # p_iva_fornitore: remove spaces, dots, dashes; must be 11 digits
    p_iva = str(parsed.get('p_iva_fornitore', '') or '')
    p_iva = re.sub(r'[\s.\-]', '', p_iva)
    result['p_iva_fornitore'] = p_iva if re.match(r'^\d{11}$', p_iva) else None

    # importi: comma → dot, fallback calcolo
    imponibile = to_float(parsed.get('imponibile'))
    importo_iva = to_float(parsed.get('importo_iva'))
    totale = parsed.get('totale_fattura')
    if totale is not None:
        totale = to_float(totale)
    elif imponibile or importo_iva:
        totale = imponibile + importo_iva
    else:
        totale = 0
    result['imponibile'] = imponibile
    result['importo_iva'] = importo_iva
    result['totale_fattura'] = totale

    # data_emissione: DD/MM/YYYY or DD-MM-YYYY → YYYY-MM-DD
    data = str(parsed.get('data_emissione', '') or '').strip()
    m = re.match(r'^(\d{2})[/-](\d{2})[/-](\d{4})$', data)
    if m:
        data = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    elif not re.match(r'^\d{4}-\d{2}-\d{2}$', data):
        data = None
    result['data_emissione'] = data

    # nome_fornitore: remove multiple spaces
    nome = str(parsed.get('nome_fornitore', '') or '').strip()
    result['nome_fornitore'] = re.sub(r'\s+', ' ', nome)

    # numero_fattura: as-is
    result['numero_fattura'] = str(parsed.get('numero_fattura', '') or '')

    return result

def call_openrouter(prompt_text, temperature=0.1):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "openai/gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sei un sistema che estrae dati da fatture in formato PDF. "
                    "Rispondi SOLO con un JSON valido (nessun testo aggiuntivo, nessun markdown). "
                    "Il JSON deve avere ESATTAMENTE questi campi: "
                    "data_emissione (stringa YYYY-MM-DD), numero_fattura (stringa), "
                    "p_iva_fornitore (stringa), nome_fornitore (stringa), "
                    "imponibile (float), importo_iva (float), totale_fattura (float)."
                )
            },
            {
                "role": "user",
                "content": f"Estrai i dati dalla seguente fattura:\n\n{prompt_text}"
            }
        ],
        "temperature": temperature,
        "max_tokens": 500
    }
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        content = data['choices'][0]['message']['content'].strip()
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
        return json.loads(content)
    except Exception:
        return None

def generate_excel(invoice_data_list, excel_path):
    df = pd.DataFrame(invoice_data_list, columns=[
        'Data', 'Numero', 'Fornitore', 'P.IVA', 'Imponibile', 'IVA', 'Totale'
    ])
    df.to_excel(excel_path, index=False, engine='openpyxl')

# ---- ROUTES ----
@app.route('/upload', methods=['POST'])
@limiter.limit("10 per minute")
def upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Missing file'}), 400
    file = request.files['file']
    email = request.form.get('email', '').strip().lower()
    if not email:
        return jsonify({'success': False, 'error': 'Missing email'}), 400
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Empty filename'}), 400

    pdf_filename = secure_filename(file.filename)
    pdf_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4().hex}_{pdf_filename}")
    file.save(pdf_path)

    user = get_or_create_user(email)

    # Monthly limit check
    if user['fatture_processate_mese'] >= user['limite_mensile']:
        os.remove(pdf_path)
        logger.warning(f"UPLOAD|{email}|{pdf_filename}|monthly_limit_exceeded")
        return jsonify({
            'success': False,
            'error': 'Limite esaurito. Passa a Base o Pro.'
        }), 403

    # Pro daily limit check (500/day)
    conn_check = get_db()
    if user['piano'] == 'pro':
        today_str = date.today().isoformat()
        daily_count = conn_check.execute(
            "SELECT COUNT(*) FROM fatture WHERE utente_id = ? AND date(created_at) = ? AND status IN ('done', 'pending')",
            (user['id'], today_str)
        ).fetchone()[0]
        if daily_count >= 500:
            conn_check.close()
            os.remove(pdf_path)
            logger.warning(f"UPLOAD|{email}|{pdf_filename}|daily_limit_exceeded_pro")
            return jsonify({
                'success': False,
                'error': 'Hai raggiunto il limite giornaliero di 500 fatture.'
            }), 429
    conn_check.close()

    # Extract text from PDF
    pdf_text = extract_pdf_text(pdf_path)
    if pdf_text is None or pdf_text == '':
        os.remove(pdf_path)
        logger.warning(f"UPLOAD|{email}|{pdf_filename}|no_text_extracted")
        # Check if it might be a scanned PDF
        if pdf_text is None:
            return jsonify({
                'success': False,
                'error': 'Impossibile leggere il PDF. File corrotto o formato non supportato.'
            }), 422
        return jsonify({
            'success': False,
            'error': 'PDF senza testo estraibile. Carica un PDF con testo selezionabile.'
        }), 422

    # Try OpenRouter: first attempt t=0.1, second t=0
    parsed = None
    for attempt, temp in enumerate([0.1, 0]):
        parsed = call_openrouter(pdf_text, temperature=temp)
        if parsed is not None:
            break

    conn = get_db()
    try:
        if parsed is None:
            conn.execute(
                "INSERT INTO fatture (utente_id, nome_file, status) VALUES (?, ?, 'error')",
                (user['id'], pdf_filename)
            )
            conn.commit()
            conn.close()
            os.remove(pdf_path)
            logger.error(f"UPLOAD|{email}|{pdf_filename}|json_parse_failed")
            return jsonify({
                'success': False,
                'error': 'Impossibile estrarre dati strutturati dalla fattura. Riprova.'
            }), 422

        # Post-processing
        cleaned = postprocess_json(parsed)

        invoice_record = {
            'Data': cleaned.get('data_emissione', ''),
            'Numero': cleaned.get('numero_fattura', ''),
            'Fornitore': cleaned.get('nome_fornitore', ''),
            'P.IVA': cleaned.get('p_iva_fornitore', ''),
            'Imponibile': cleaned.get('imponibile', 0),
            'IVA': cleaned.get('importo_iva', 0),
            'Totale': cleaned.get('totale_fattura', 0),
        }

        excel_filename = f"fattura_{uuid.uuid4().hex}.xlsx"
        excel_path = os.path.join(EXCEL_FOLDER, excel_filename)
        generate_excel([invoice_record], excel_path)

        conn.execute(
            "INSERT INTO fatture (utente_id, nome_file, json_estratto, excel_path, status) VALUES (?, ?, ?, ?, 'done')",
            (user['id'], pdf_filename, json.dumps(cleaned), excel_path)
        )
        conn.commit()
        invoice_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "UPDATE utenti SET fatture_processate_mese = fatture_processate_mese + 1 WHERE id = ?",
            (user['id'],)
        )
        conn.commit()
        conn.close()
        os.remove(pdf_path)

        logger.info(f"UPLOAD|{email}|{pdf_filename}|success|invoice_{invoice_id}")

        return jsonify({
            'success': True,
            'data': invoice_record,
            'excel_url': f"/download/{invoice_id}"
        })

    except Exception as e:
        conn.rollback()
        conn.close()
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
        logger.error(f"UPLOAD|{email}|{pdf_filename}|exception: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download/<int:invoice_id>', methods=['GET'])
def download(invoice_id):
    conn = get_db()
    row = conn.execute(
        "SELECT excel_path FROM fatture WHERE id = ? AND status = 'done'",
        (invoice_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({'success': False, 'error': 'Fattura non trovata'}), 404
    excel_path = row['excel_path']
    if not os.path.exists(excel_path):
        return jsonify({'success': False, 'error': 'File non disponibile'}), 404
    return send_file(excel_path, as_attachment=True, download_name=f"fattura_{invoice_id}.xlsx")

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = (data.get('email', '') if data else '').strip().lower()
    if not email:
        return jsonify({'success': False, 'error': 'Email richiesta'}), 400

    conn = get_db()
    user = conn.execute("SELECT * FROM utenti WHERE email = ?", (email,)).fetchone()
    if user is None:
        conn.execute("INSERT INTO utenti (email, piano, limite_mensile) VALUES (?, 'free', 5)", (email,))
        conn.commit()
        user = conn.execute("SELECT * FROM utenti WHERE email = ?", (email,)).fetchone()

    token = str(uuid.uuid4())
    conn.execute("UPDATE utenti SET token = ? WHERE id = ?", (token, user['id']))
    conn.commit()
    conn.close()

    magic_link = f"{FRONTEND_URL}/dashboard?token={token}"
    email_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px;">
        <h2 style="color: #1e3a5f;">Benvenuto su FatturaLens</h2>
        <p>Clicca il pulsante qui sotto per accedere:</p>
        <a href="{magic_link}" style="display: inline-block; background: #1e3a5f; color: white; padding: 12px 28px; border-radius: 6px; text-decoration: none; font-weight: bold;">Accedi a FatturaLens</a>
        <p style="margin-top: 24px; color: #666;">Oppure copia questo link nel browser:<br>{magic_link}</p>
    </div>
    """

    if RESEND_API_KEY:
        try:
            requests.post(
                'https://api.resend.com/emails',
                headers={
                    'Authorization': f'Bearer {RESEND_API_KEY}',
                    'Content-Type': 'application/json'
                },
                json={
                    'from': 'FatturaLens <onboarding@resend.dev>',
                    'to': email,
                    'subject': 'Accedi a FatturaLens',
                    'html': email_html
                },
                timeout=15
            )
        except Exception:
            pass

    logger.info(f"LOGIN|{email}|token_sent")
    return jsonify({'success': True, 'message': 'Email inviata'})

@app.route('/dashboard', methods=['GET'])
def dashboard():
    token = request.args.get('token', '')
    if not token:
        return jsonify({'success': False, 'error': 'Token richiesto'}), 401

    conn = get_db()
    user = conn.execute(
        "SELECT id, email, piano, fatture_processate_mese, limite_mensile, created_at FROM utenti WHERE token = ?",
        (token,)
    ).fetchone()
    if user is None:
        conn.close()
        return jsonify({'success': False, 'error': 'Token non valido'}), 401

    storia = conn.execute(
        "SELECT id, nome_file, json_estratto, status, created_at FROM fatture WHERE utente_id = ? ORDER BY created_at DESC LIMIT 20",
        (user['id'],)
    ).fetchall()

    conn.close()

    return jsonify({
        'success': True,
        'user': {
            'email': user['email'],
            'piano': user['piano'],
            'fatture_processate_mese': user['fatture_processate_mese'],
            'limite_mensile': user['limite_mensile'],
            'iscritto_dal': user['created_at']
        },
        'storico': [
            {
                'id': r['id'],
                'file': r['nome_file'],
                'data': r['json_estratto'],
                'status': r['status'],
                'created_at': r['created_at']
            } for r in storia
        ]
    })

@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    if not sig_header or not STRIPE_WEBHOOK_SECRET:
        return jsonify({'error': 'Webhook secret non configurato'}), 400

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        customer_email = session.get('customer_email', '').strip().lower()
        line_items = None
        try:
            line_items = stripe.checkout.Session.list_line_items(session['id'], limit=1)
        except Exception:
            pass

        product_id = None
        if line_items and line_items['data']:
            price = line_items['data'][0].get('price', {})
            product_id = price.get('product')

        if product_id and customer_email:
            conn = get_db()
            product_map = {
                'prod_base': ('base', 100),
                'prod_pro': ('pro', 99999),
            }
            if product_id in product_map:
                piano, limite = product_map[product_id]
                conn.execute(
                    "UPDATE utenti SET piano = ?, limite_mensile = ?, stripe_customer_id = ? WHERE email = ?",
                    (piano, limite, session.get('customer', ''), customer_email)
                )
            else:
                try:
                    prod = stripe.Product.retrieve(product_id)
                    prod_name = prod.get('name', '').lower()
                    if 'pro' in prod_name:
                        conn.execute(
                            "UPDATE utenti SET piano = 'pro', limite_mensile = 99999, stripe_customer_id = ? WHERE email = ?",
                            (session.get('customer', ''), customer_email)
                        )
                    elif 'base' in prod_name:
                        conn.execute(
                            "UPDATE utenti SET piano = 'base', limite_mensile = 100, stripe_customer_id = ? WHERE email = ?",
                            (session.get('customer', ''), customer_email)
                        )
                except Exception:
                    pass
            conn.commit()
            upgraded_plan = product_map.get(product_id, ('unknown', 0))[0]
            conn.close()
            logger.info(f"STRIPE|{customer_email}|upgraded_to_{upgraded_plan}")

    return jsonify({'received': True}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
