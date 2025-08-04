import os
from flask import Flask, request, g
from appl import db
from routes.booking import booking_bp
from flask_wtf import CSRFProtect
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

app = Flask(__name__)
csrf = CSRFProtect(app)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')

# Dizionario tenant → URI (puoi caricarlo da file)
TENANT_DATABASES = {
    "salone1": os.environ.get("SQLALCHEMY_DATABASE_URI_1"),
    "salone2": os.environ.get("SQLALCHEMY_DATABASE_URI_2"),
    "salone3": os.environ.get("SQLALCHEMY_DATABASE_URI_3"),
    # aggiungi altri tenant
}

@app.before_request
def set_tenant_db():
    tenant = request.path.strip("/").split("/")[0]
    db_uri = TENANT_DATABASES.get(tenant)
    if not db_uri:
        return "Tenant non trovato", 404
    engine = create_engine(db_uri)
    session_factory = sessionmaker(bind=engine)
    g.db_session = scoped_session(session_factory)
    g.tenant = tenant

@app.teardown_request
def remove_session(exception=None):
    db_session = getattr(g, 'db_session', None)
    if db_session:
        db_session.remove()

app.register_blueprint(booking_bp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)