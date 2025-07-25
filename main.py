import os
from flask import Flask
from appl import db
from routes.booking import booking_bp
from flask_wtf import CSRFProtect

app = Flask(__name__)

csrf = CSRFProtect(app)

# Usa la variabile d'ambiente impostata su Azure
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('SQLALCHEMY_DATABASE_URI')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'scegli-una-chiave-sicura')

db.init_app(app)
app.register_blueprint(booking_bp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)