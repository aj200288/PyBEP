"""
PyBEP website (Flask). Run the development server with::

    python -m pybep.web

All calculation and file parsing comes from ``pybep.core`` — the same
code the desktop app runs. See pipeline.py.
"""
import os
import secrets
import sys

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


def _secret_key():
    """
    The key that signs the session cookie.

    Never fall back to a fixed string. The cookie carries the session's
    working directory, and the server both reads files from it and deletes
    it, so a key an attacker can read — and a literal in a public
    repository is exactly that — hands them both. A random per-process key
    removes the possibility.

    The cost is that sessions do not survive a restart and cannot be shared
    between worker processes, so a real deployment should still set
    SECRET_KEY.
    """
    key = os.environ.get('SECRET_KEY')
    if key:
        return key
    print("PyBEP: SECRET_KEY is not set — using a random key for this "
          "process. Sessions will be lost on restart and will not work "
          "across multiple workers. Set SECRET_KEY before serving real "
          "users.", file=sys.stderr)
    return secrets.token_hex(32)


def _n_jobs():
    """Worker count from the environment, ignoring anything unusable."""
    raw = os.environ.get('PYBEP_N_JOBS')
    if raw is None:
        return DEFAULT_N_JOBS
    try:
        value = int(raw)
    except ValueError:
        print(f"PyBEP: ignoring PYBEP_N_JOBS={raw!r} (not a whole number), "
              f"using {DEFAULT_N_JOBS}.", file=sys.stderr)
        return DEFAULT_N_JOBS
    if value == 0:  # joblib rejects 0; -1 legitimately means "all cores"
        print(f"PyBEP: PYBEP_N_JOBS=0 is not valid, using {DEFAULT_N_JOBS}.",
              file=sys.stderr)
        return DEFAULT_N_JOBS
    return value


def create_app():
    app = Flask(__name__)

    app.config['SECRET_KEY'] = _secret_key()
    app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES
    app.config['MAX_ITERATIONS'] = MAX_ITERATIONS
    app.config['MIN_ITERATIONS'] = MIN_ITERATIONS
    app.config['MAX_CURVE_FILES'] = MAX_CURVE_FILES
    app.config['N_JOBS'] = _n_jobs()

    from . import routes
    app.register_blueprint(routes.bp)
    routes.register_error_handlers(app)

    return app
