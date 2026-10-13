import logging
import time

from .config import Settings
from .db import Database
from .model_client import ModelClient
from .service import IndexWorker
from .store import Store


def main():
    settings = Settings.from_env()
    db = Database(settings)
    db.ready()
    model = ModelClient(settings)
    worker = IndexWorker(settings, Store(db, settings), model)
    try:
        while True:
            try:
                if not worker.run_once():
                    time.sleep(2)
            except Exception as exc:
                logging.error("index_worker_error type=%s", type(exc).__name__)
                time.sleep(5)
    finally:
        model.close()


if __name__ == "__main__":
    main()
