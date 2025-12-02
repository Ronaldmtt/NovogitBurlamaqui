from app import app, db
from sqlalchemy import text

with app.app_context():
    # Tenta adicionar as colunas; se já existirem, apenas mostra a mensagem e segue.
    try:
        db.session.execute(text("ALTER TABLE process ADD COLUMN advogado_autor TEXT"))
        print("Coluna 'advogado_autor' adicionada.")
    except Exception as e:
        print("advogado_autor:", e)

    try:
        db.session.execute(text("ALTER TABLE process ADD COLUMN advogado_reu TEXT"))
        print("Coluna 'advogado_reu' adicionada.")
    except Exception as e:
        print("advogado_reu:", e)

    db.session.commit()
    print("OK - migração leve aplicada.")
