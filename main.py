import os
import threading
import time as time_mod
from flask import Flask, g, request, abort
from appl import db
from routes.booking import booking_bp
from flask_wtf import CSRFProtect
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.ext.automap import automap_base
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
csrf = CSRFProtect(app)

# 1. Carica le stringhe di connessione per ogni negozio dalle variabili d'ambiente
#    Assicurati di impostarle nel tuo hosting (Railway, Azure, etc)
TENANT_DATABASES = {
    't1': os.environ.get('DATABASE_URL_NEGOZIO1'),
    't2': os.environ.get('DATABASE_URL_NEGOZIO2'),
    't3': os.environ.get('DATABASE_URL_NEGOZIO3'),
}

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

secret = os.environ.get('SECRET_KEY')
if not secret:
    raise RuntimeError("SECRET_KEY non impostata.")
app.config['SECRET_KEY'] = secret

app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# 3. Crea un "motore" e una "sessione" per ogni database
#    Questo permette di scegliere a quale database connettersi
db_engines = {
    tenant: create_engine(url)
    for tenant, url in TENANT_DATABASES.items() if url
}
db_sessions = {
    tenant: scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    for tenant, engine in db_engines.items()
}
# Riflette la struttura del database per ogni tenant
db_bases = {
    tenant: automap_base()
    for tenant in db_engines
}
for tenant, base in db_bases.items():
    base.prepare(autoload_with=db_engines[tenant])

# Espone i riferimenti per uso altrove (es. job schedulati)
app.config['DB_SESSIONS'] = db_sessions
app.config['DB_BASES'] = db_bases
app.config['TENANT_DATABASES'] = TENANT_DATABASES
app.config['DB_ENGINES'] = db_engines

def _start_morning_scheduler_once(app):
    # evita multi-avvio in ambienti con più worker
    if app.config.get('MORNING_SCHEDULER_STARTED'):
        return
    app.config['MORNING_SCHEDULER_STARTED'] = True

    def worker():
        from flask import current_app
        import importlib
        # importa il modulo una volta e leggi attributi
        booking_mod = importlib.import_module('routes.booking')
        poll_seconds = getattr(booking_mod, 'MORNING_POLL_SECONDS', 60)
        process_morning_tick = getattr(booking_mod, 'process_morning_tick')

        while True:
            try:
                with app.app_context():
                    sessions = current_app.config.get('DB_SESSIONS', {})
                    for tenant_id in sessions.keys():
                        try:
                            process_morning_tick(app, tenant_id)
                        except Exception as e:
                            print(f"[WA-MORNING][{tenant_id}] tick error: {repr(e)}")
            except Exception as e:
                print(f"[WA-MORNING] loop error: {repr(e)}")
            time_mod.sleep(poll_seconds)

    t = threading.Thread(target=worker, name="wa_morning_scheduler", daemon=True)
    t.start()

def _start_operator_scheduler_once(app):
    if app.config.get('OP_SCHEDULER_STARTED'):
        return
    app.config['OP_SCHEDULER_STARTED'] = True

    def worker():
        import importlib
        booking_mod = importlib.import_module('routes.booking')
        poll_seconds = getattr(booking_mod, 'MORNING_POLL_SECONDS', 60)
        process_operator_tick = getattr(booking_mod, 'process_operator_tick')
        while True:
            try:
                with app.app_context():
                    sessions = app.config.get('DB_SESSIONS', {})
                    for tenant_id in sessions.keys():
                        try:
                            process_operator_tick(app, tenant_id)
                        except Exception as e:
                            print(f"[WA-OP][{tenant_id}] tick error: {repr(e)}")
            except Exception as e:
                print(f"[WA-OP] loop error: {repr(e)}")
            time_mod.sleep(poll_seconds)

    t = threading.Thread(target=worker, name="wa_operator_scheduler", daemon=True)
    t.start()

# 4. Registra il blueprint con un prefisso dinamico
#    Questo renderà le tue routes accessibili tramite /negozio1/booking, /negozio2/booking, etc.
app.register_blueprint(booking_bp, url_prefix='/<tenant_id>')

@app.route('/')
def index():
    links = []
    for tenant_id in TENANT_DATABASES.keys():
        Session = db_sessions.get(tenant_id)
        Base = db_bases.get(tenant_id)
        if Session and Base:
            BusinessInfo = getattr(Base.classes, 'business_info', None)
            nome = tenant_id
            if BusinessInfo:
                s = Session()
                try:
                    bi = s.query(BusinessInfo).first()
                    if bi and getattr(bi, 'business_name', None):
                        nome = bi.business_name
                finally:
                    s.close()
                    Session.remove()
            links.append(f'<li><a href="/{tenant_id}/booking">{nome}</a></li>')
        else:
            links.append(f'<li><a href="/{tenant_id}/booking">{tenant_id}</a></li>')
    return f"""
    <h1>Portale Negozi</h1>
    <ul>
        {''.join(links)}
    </ul>
    """

@app.before_request
def attach_db_session():
    tenant_id = request.view_args.get('tenant_id') if request.view_args else None
    if tenant_id and tenant_id in db_sessions:
        g.db_session = db_sessions[tenant_id]
        g.db_base = db_bases[tenant_id]
        g.tenant_id = tenant_id  # Aggiungi per filtrare query
    elif tenant_id:
        abort(404, description="Negozio non trovato.")

@app.after_request
def set_security_headers(response):
    # Prevent MIME type sniffing
    response.headers['X-Content-Type-Options'] = 'nosniff'

    # Clickjacking: allow same-origin embedding for internal widgets
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'

    # Build CSP with optional extra hosts from env
    extra_hosts = os.environ.get('CSP_TRUSTED_HOSTS', '').strip()
    extra = (' ' + extra_hosts) if extra_hosts else ''

    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https: " + extra + "; "
        "style-src 'self' 'unsafe-inline' https: " + extra + "; "
        "img-src 'self' data: https: " + extra + "; "
        "connect-src 'self' https: " + extra + "; "
        "font-src 'self' data: https: " + extra + "; "
        "object-src 'none'; "
        "frame-ancestors 'self';"
    )

    # If set, send report-only header first to detect violations without breaking layout
    if os.environ.get('CSP_REPORT_ONLY', '0') == '1':
        response.headers['Content-Security-Policy-Report-Only'] = csp
    else:
        response.headers['Content-Security-Policy'] = csp

    # Cross-Origin Resource Policy
    response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    # Referrer policy
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # HSTS: rollout-safe default (1 day). Increase after verification.
    response.headers['Strict-Transport-Security'] = 'max-age=86400; includeSubDomains; preload'
    return response

@app.teardown_appcontext
def shutdown_session(exception=None):
    # Rimuove la sessione del database alla fine della richiesta
    if hasattr(g, 'db_session'):
        g.db_session.remove()

_start_morning_scheduler_once(app)
_start_operator_scheduler_once(app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)