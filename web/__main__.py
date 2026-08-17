"""
Entry point for ``python -m web`` — starts a local development server on
http://localhost:5000.

This is Flask's built-in server: fine for development and for trying the
site out, but not what you should serve real users with. For that, run a
production WSGI server against the same app factory, e.g.::

    waitress-serve --port=8000 --call web:create_app     # Windows
    gunicorn 'web:create_app()' -b 0.0.0.0:8000          # Linux hosts
"""
from . import create_app

if __name__ == '__main__':
    create_app().run(debug=True, port=5000)
