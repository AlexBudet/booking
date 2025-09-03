import os
from flask import Flask, g
from appl import db
from routes.booking import booking_bp
from flask_wtf import CSRFProtect

app = Flask(__name__)
csrf = CSRFProtect(app)

# Usa la variabile d'ambiente impostata su Azure
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('SQLALCHEMY_DATABASE_URI')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

secret = os.environ.get('SECRET_KEY')
if not secret:
    raise RuntimeError("SECRET_KEY non impostata. Imposta la variabile d'ambiente SECRET_KEY in produzione.")
app.config['SECRET_KEY'] = secret

# Cookie/session security hardening
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

db.init_app(app)
app.register_blueprint(booking_bp)

@app.before_request
def attach_db_session():
    # se non è presente (es. non è stato implementato il middleware tenant), usiamo la sessione di default
    if not getattr(g, "db_session", None):
        g.db_session = db.session

@app.teardown_appcontext
def shutdown_session(exception=None):
    # rimuove la sessione scoped (safe no-op se non necessario)
    try:
        db.session.remove()
    except Exception:
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)