import os
from flask import Flask, request, g
from appl import db
from routes.booking import booking_bp
from flask_wtf import CSRFProtect
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from flask import abort

app = Flask(__name__)
csrf = CSRFProtect(app)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')

# Dizionario tenant → URI (puoi caricarlo da file)
TENANT_DATABASES = {
    "salone1": os.environ.get("SQLALCHEMY_DATABASE_URI_1"),
}

@app.url_value_preprocessor
def pull_tenant(endpoint, values):
    if not values or "tenant" not in values:
        return
    tenant = values.pop("tenant")
    print("URL tenant:", tenant)  # Debug
    print("DB URI:", TENANT_DATABASES.get(tenant))  # Debug
    if not tenant or tenant not in TENANT_DATABASES or not TENANT_DATABASES[tenant]:
        abort(404, description="Tenant non trovato")
    engine = create_engine(TENANT_DATABASES[tenant])
    session_factory = sessionmaker(bind=engine)
    g.db_session = scoped_session(session_factory)
    g.tenant = tenant

@app.teardown_request
def remove_session(exception=None):
    db_session = getattr(g, 'db_session', None)
    if db_session:
        db_session.remove()

app.register_blueprint(booking_bp, url_prefix="/<tenant>/booking")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)