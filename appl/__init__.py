from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

def init_app(app):
    """
    Chiamare in main.py dopo aver caricato la config:
      from appl import init_app
      init_app(app)
    Questo initializza SQLAlchemy con l'app.
    """
    db.init_app(app)