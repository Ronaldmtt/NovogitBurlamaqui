from app import app, db
from models import User, Process
from sqlalchemy import text

with app.app_context():
    print("DB URL:", db.engine.url)
    rows = db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    print("Tabelas:", [r[0] for r in rows])
    print("Users:", db.session.query(User).count())
    print("Process:", db.session.query(Process).count())
