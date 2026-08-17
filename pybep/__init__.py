"""
PyBEP — battery electrode potentials calculator.

Two front ends over one shared calculation core:

    pybep.core      calculation and file parsing, no user interface
    pybep.gui_app   the desktop app (Tkinter)  ─┬─ both build on core
    pybep.web       the website (Flask)        ─┘

Run the desktop app with ``python -m pybep.gui_app``, or the website with
``python -m pybep.web``. See the README for the rule that keeps the two
front ends sharing one implementation instead of drifting apart.
"""
