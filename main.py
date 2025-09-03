import os
from flask import Flask, g, request, render_template, abort
from appl import db
from routes.booking import booking_bp
from flask_wtf import CSRFProtect
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.ext.automap import automap_base

app = Flask(__name__)
csrf = CSRFProtect(app)

# 1. Carica le stringhe di connessione per ogni negozio dalle variabili d'ambiente
#    Assicurati di impostarle nel tuo hosting (Railway, Azure, etc)
TENANT_DATABASES = {
    'negozio1': os.environ.get('DATABASE_URL_NEGOZIO1'),
    'negozio2': os.environ.get('DATABASE_URL_NEGOZIO2')
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
    base.prepare(db_engines[tenant], reflect=True)


# 4. Registra il blueprint con un prefisso dinamico
#    Questo renderà le tue routes accessibili tramite /negozio1/booking, /negozio2/booking, etc.
app.register_blueprint(booking_bp, url_prefix='/<tenant_id>')

@app.route('/')
def index():
    links = []
    for tenant_id in TENANT_DATABASES.keys():
        session = db_sessions.get(tenant_id)
        base = db_bases.get(tenant_id)
        if session and base:
            # Usa la classe riflessa
            BusinessInfo = getattr(base.classes, 'business_info', None)
            business_info = session.query(BusinessInfo).first() if BusinessInfo else None
            nome = business_info.business_name if business_info and hasattr(business_info, 'business_name') else tenant_id
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

@app.teardown_appcontext
def shutdown_session(exception=None):
    # Rimuove la sessione del database alla fine della richiesta
    if hasattr(g, 'db_session'):
        g.db_session.remove()

# --- FINE MODIFICHE ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)