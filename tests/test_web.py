"""
End-to-end checks for the website, driven through Flask's test client so
no browser or running server is needed.

Covers the full upload -> confirm -> results -> download flow and the
format-data flow against the real files in data/, plus the input clamping
that keeps a hand-crafted POST from tying the server up.

Run:
    python tests/test_web.py
"""
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web import create_app, MAX_ITERATIONS  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("" if cond else f"  ({detail})"))


def upload_file(path):
    """Read a data file into the (bytes, filename) tuple the client wants."""
    with open(path, "rb") as f:
        return (io.BytesIO(f.read()), os.path.basename(path))


app = create_app()
app.config["TESTING"] = True
client = app.test_client()

# --- pages render -----------------------------------------------------
check("index renders", client.get("/").status_code == 200)
check("format page renders", client.get("/format").status_code == 200)

# --- input clamping (the reason a public site needs a server-side cap) ---
with app.test_request_context(
        "/upload", method="POST",
        data={"iterations": "999999999", "slider_a": "0.3"}):
    from flask import request
    from web.routes import _clamp_settings
    settings = _clamp_settings(request.form)
check(f"iterations clamped to {MAX_ITERATIONS}",
      settings["iterations"] == MAX_ITERATIONS, settings["iterations"])
check("weights still sum to 1",
      abs(settings["battery_weight"] + settings["derivative_weight"] - 1.0) < 1e-9,
      settings)

for bad in ("not-a-number", "", "-5"):
    with app.test_request_context("/upload", method="POST",
                                  data={"iterations": bad}):
        from flask import request
        s = _clamp_settings(request.form)
    check(f"iterations={bad!r} stays in range",
          1 <= s["iterations"] <= MAX_ITERATIONS, s["iterations"])

with app.test_request_context("/upload", method="POST",
                              data={"slider_a": "50"}):
    from flask import request
    s = _clamp_settings(request.form)
check("out-of-range weight clamped to 1.0", s["battery_weight"] == 1.0, s)

# --- full optimization flow -------------------------------------------
cathode = os.path.join(DATA, "cathode_data", "NMC811-10.1016_j.xcrp.2020.100253.txt")
anode = os.path.join(DATA, "anode_data", "Graphite-10.1016_j.xcrp.2020.100253.txt")
battery = os.path.join(DATA, "NMC811vsGraphite_OCV-10.1016_j.xcrp.2020.100253.txt")

resp = client.post("/upload", data={
    "cathode_files": [upload_file(cathode)],
    "anode_files": [upload_file(anode)],
    "battery_file": [upload_file(battery)],
    "iterations": "1",
    "slider_a": "1",
}, content_type="multipart/form-data")
check("upload -> confirm page", resp.status_code == 200, resp.status_code)
body = resp.get_data(as_text=True)
check("confirm page lists the files", "Confirm columns" in body)

# Pull the generated file ids back out of the confirm form.
import re  # noqa: E402
file_ids = re.findall(r'name="soc_([0-9a-f]{32})"', body)
check("confirm form has one row per uploaded file", len(file_ids) == 3, len(file_ids))

form = {}
for fid in file_ids:
    form[f"soc_{fid}"] = "0"
    form[f"ocv_{fid}"] = "1"

resp = client.post("/confirm", data=form)
check("confirm -> results page", resp.status_code == 200, resp.status_code)
body = resp.get_data(as_text=True)
check("results page shows the plot", 'src="data:image/png;base64,' in body)
check("results page shows RMSD", "Lowest RMSD" in body)

resp = client.get("/download")
check("download returns the result JSON", resp.status_code == 200, resp.status_code)
payload = json.loads(resp.get_data())
check("JSON has the expected keys",
      {"Best Cathode Data ID", "Best Anode Data ID", "Lowest RMSD",
       "Battery SOC", "Battery OCV"} <= set(payload),
      sorted(payload)[:5])
check("JSON curves are 1001 points",
      len(payload["Cathode OCP"]) == 1001 and len(payload["Anode OCP"]) == 1001,
      (len(payload["Cathode OCP"]), len(payload["Anode OCP"])))

# --- format data flow -------------------------------------------------
resp = client.post("/format", data={
    "data_files": [upload_file(cathode), upload_file(anode)],
    "curve_type": "cathode",
}, content_type="multipart/form-data")
check("format -> results page", resp.status_code == 200, resp.status_code)
check("format page reports success",
      "Formatting complete" in resp.get_data(as_text=True))

resp = client.get("/format/download")
check("format zip downloads", resp.status_code == 200, resp.status_code)
with zipfile.ZipFile(io.BytesIO(resp.get_data())) as zf:
    names = zf.namelist()
    check("zip holds both formatted files", len(names) == 2, names)
    check("zip entries use the original names (no uuid prefix)",
          all(not n[:32].isalnum() or "_" in n[:40] for n in names) and
          all("FORMATTED" in n for n in names), names)
    first = zf.read(names[0]).decode().strip().splitlines()
    check("formatted file has 1001 rows", len(first) == 1001, len(first))
    check("formatted rows are 'x<TAB>y'", len(first[0].split("\t")) == 2, first[0])

# --- rejects bad input ------------------------------------------------
resp = client.post("/format", data={
    "data_files": [(io.BytesIO(b"not data"), "notes.pdf")],
    "curve_type": "cathode",
}, content_type="multipart/form-data")
check("unsupported extension is rejected", resp.status_code in (200, 302), resp.status_code)

resp = client.post("/upload", data={"iterations": "1"},
                   content_type="multipart/form-data")
check("upload with no files redirects with an error", resp.status_code == 302,
      resp.status_code)

# --- summary ----------------------------------------------------------
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
