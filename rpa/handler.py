"""Ponto de entrada da AWS Lambda (EventBridge Scheduler -> Lambda).

Evento opcional: {"dry_run": true, "sem_geocode": true, "retreinar_ia": true}
As variáveis de ambiente são as mesmas do .env.example (configure na Lambda ou via SSM).
"""

import logging

from rpa.sync.main import run

logging.getLogger().setLevel(logging.INFO)


def lambda_handler(event, context):
    event = event or {}
    return run(
        dry_run=bool(event.get("dry_run", False)),
        sem_geocode=bool(event.get("sem_geocode", False)),
        permitir_vazio=bool(event.get("permitir_vazio", False)),
        retreinar_ia=event.get("retreinar_ia"),
    )
