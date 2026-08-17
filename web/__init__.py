"""
PyBEP website (Flask). Run the development server with::

    python -m web

All calculation and file parsing comes from ``core`` — the same code the
desktop app runs. See web/pipeline.py.
"""
import os

from flask import Flask

# --- Limits -----------------------------------------------------------
# Every one of these exists because the optimization is expensive and the
# site is public: work grows as (cathodes x anodes x iterations), so
# without caps a single request can occupy the server indefinitely and
# lock everyone else out. Raise them only alongside a real job
# queue/timeout.

# Matches the desktop app's iterations slider (1-5).
MIN_ITERATIONS = 1
MAX_ITERATIONS = 5

# Candidate curves per electrode. 12 x 12 pairs x 5 iterations is already
# a long wait; this is the ceiling, not a recommendation.
MAX_CURVE_FILES = 12

# Total upload size across all files in one request.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024  # 32 MB

# Worker processes for the optimization. Small hosts report far more CPUs
# than they actually let a container use, so joblib's default of "all
# cores" (-1) oversubscribes and slows everything down. Override with
# PYBEP_N_JOBS; -1 restores the desktop behaviour.
DEFAULT_N_JOBS = 2


def create_app():
    app = Flask(__name__)

    # SECRET_KEY signs the session cookie (workdir path + settings + file
    # metadata). Set a real one via the environment in production; the
    # fallback below is only so local development runs without setup.
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
    app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES
    app.config['MAX_ITERATIONS'] = MAX_ITERATIONS
    app.config['MIN_ITERATIONS'] = MIN_ITERATIONS
    app.config['MAX_CURVE_FILES'] = MAX_CURVE_FILES
    app.config['N_JOBS'] = int(os.environ.get('PYBEP_N_JOBS', DEFAULT_N_JOBS))

    from . import routes
    app.register_blueprint(routes.bp)

    return app
