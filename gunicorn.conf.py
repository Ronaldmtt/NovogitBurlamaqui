"""
Gunicorn configuration for Flask legal process management system
Increases timeout for long-running AI PDF processing operations
"""

# Server socket
bind = "0.0.0.0:5000"

# Worker processes
workers = 1
worker_class = "sync"

# Timeout configurations
# Increased from default 30s to 300s (5 minutes) to handle large PDF processing
timeout = 300
graceful_timeout = 300
keepalive = 5

# Server mechanics
daemon = False
pidfile = None
user = None
group = None
tmp_upload_dir = None

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Process naming
proc_name = "legal_process_app"

# Server hooks
def on_starting(server):
    print("[GUNICORN] Starting with timeout: 300s")

def on_reload(server):
    print("[GUNICORN] Reloading configuration")

def worker_int(worker):
    print(f"[GUNICORN] Worker {worker.pid} received INT or QUIT signal")

# Reload
reload = True
reload_engine = "auto"
