from __future__ import annotations

import logging
import threading

from flask import Flask, jsonify

log = logging.getLogger(__name__)
app = Flask(__name__)


@app.get("/ping")
def ping():
    return "OK", 200


@app.get("/")
def index():
    return jsonify({"service": "Error404 Bot", "status": "running",
                    "chain": "Robinhood Chain (4663)", "token": "$ERROR404"})


def start_health_server(port: int) -> None:
    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True, name="health-server"
    )
    thread.start()
    log.info("Health server listening on :%s/ping", port)
