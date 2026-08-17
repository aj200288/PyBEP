"""
Smoke checks for the desktop app.

The point of these is not to drive the UI, it's to catch the two things
that silently break when core/gui_app/web are refactored:

  * importing gui_app must not open a window (it used to build a Tk root at
    import time, which made the module unusable from anywhere else), and
  * the window must still construct with all its widgets after core
    changes shape.

Skipped automatically where there is no display (e.g. CI).

Run:
    python tests/test_gui.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("" if cond else f"  ({detail})"))


try:
    import tkinter as tk
    tk.Tk().destroy()
except Exception as e:
    print(f"No usable display ({e}) - skipping GUI checks.")
    sys.exit(0)

# Importing must not create a window. If it did, the Tk default root would
# already exist by the time we get here.
import gui_app.main as gui_main  # noqa: E402

check("importing gui_app.main opens no window",
      tk._default_root is None, tk._default_root)
check("gui_app.main exposes main()", callable(gui_main.main))

root = tk.Tk()
root.withdraw()  # build the widgets without flashing a window on screen
gui = gui_main.OCVBatteryDecompositionGUI(root)
root.update()

check("run/download/format buttons exist",
      all([gui.run_button, gui.download_button, gui.format_data_button]))
check("iterations defaults to 1", gui.iterations_var.get() == 1,
      gui.iterations_var.get())
check("battery weight defaults to 1", gui.battery_var.get() == 1,
      gui.battery_var.get())

# The two weight sliders are two views of one split. Tk only fires a
# Scale's command on real interaction (not on .set() of an unmapped
# widget), so call the handlers the way a mouse drag would.
gui.update_derivative_weight('0.4')
check("moving battery weight to 0.40 sets differential capacity to 0.60",
      abs(gui.derivative_var.get() - 0.6) < 1e-9, gui.derivative_var.get())
gui.update_battery_weight('0.25')
check("moving differential capacity to 0.25 sets battery weight to 0.75",
      abs(gui.battery_var.get() - 0.75) < 1e-9, gui.battery_var.get())

root.destroy()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
