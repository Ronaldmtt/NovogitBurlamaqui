#!/bin/bash
# Script de inicialização que garante as variáveis de ambiente

# Exportar variáveis de ambiente do PostgreSQL se existirem
if [ -n "$DATABASE_URL" ]; then
    echo "[START] DATABASE_URL encontrada, usando PostgreSQL"
    export SQLALCHEMY_DATABASE_URI="$DATABASE_URL"
else
    echo "[START] DATABASE_URL não encontrada, usando SQLite"
fi

# Iniciar gunicorn com as variáveis de ambiente
exec gunicorn --bind 0.0.0.0:5000 --reuse-port --reload main:app
