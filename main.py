from app import create_app
from flask import jsonify, Response
from pathlib import Path

app = create_app()

@app.get("/api/ultimo-processo")
def api_ultimo_processo():
    p = Path("instance/rpa_current.json")
    if p.exists():
        txt = p.read_text(encoding="utf-8")
        return Response(txt, status=200, mimetype="application/json; charset=utf-8")
    return jsonify({}), 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
