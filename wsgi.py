"""WSGI entry point for uwsgi: module wsgi, callable app."""
from app import app

if __name__ == "__main__":
    app.run()
