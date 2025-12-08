from app import create_app, db
from models import User, Process
from sqlalchemy import inspect

app = create_app()

with app.app_context():
    print("DB URL:", db.engine.url)
    
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    print("Tabelas:", tables)
    
    print("Users:", db.session.query(User).count())
    print("Process:", db.session.query(Process).count())
