"""WSGI entry point for uwsgi: module wsgi, callable app.

Nothing here starts a server. uwsgi imports `app`; run `python app.py` for the dev server.
"""
from app import app  # noqa: F401
