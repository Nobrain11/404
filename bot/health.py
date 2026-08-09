from __future__ import annotations
import logging, threading
from flask import Flask, jsonify

log = logging.getLogger(__name__)
app = Flask(__name__)

@app.get("/ping")
def ping(): return "OK", 200

@app.get("/")
def index(): return jsonify({"service": "Error404 Bot", "status": "running", "chain": "Robinhood Chain (4663)", "token": "$ERROR"})

def start_health_server(port: int) -> None:
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False), daemon=True, name="health").start()
    log.info("Health server on :%s/ping", port)
