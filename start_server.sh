#!/bin/bash
# Startup script for Flask legal process management system
# Uses custom Gunicorn configuration with extended timeout

echo "[START] Iniciando servidor com configuração estendida de timeout..."
gunicorn -c gunicorn_config.py --reuse-port main:app
