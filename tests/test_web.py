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

from pybep.web import create_app, MAX_ITERATIONS  # noqa: E402

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
    from pybep.web.routes import _clamp_settings
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

# The reported ID must be the user's own filename, exactly as the desktop
# app shows it - not the uuid-prefixed name the file is stored under.
cathode_stem = os.path.splitext(os.path.basename(cathode))[0]
anode_stem = os.path.splitext(os.path.basename(anode))[0]
check("results page reports the cathode by its filename",
      cathode_stem in body, cathode_stem)
check("results page reports the anode by its filename",
      anode_stem in body, anode_stem)
check("no uuid prefix leaks into the results page",
      re.search(r"\b[0-9a-f]{32}_", body) is None,
      (re.search(r"\b[0-9a-f]{32}_\S*", body) or [""])[0])

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
check("JSON reports the same clean IDs as the page",
      payload["Best Cathode Data ID"] == cathode_stem
      and payload["Best Anode Data ID"] == anode_stem,
      (payload["Best Cathode Data ID"], payload["Best Anode Data ID"]))

# --- naming helpers ---------------------------------------------------
from pybep.web.pipeline import curve_label, original_name  # noqa: E402
from pybep.web.routes import _unique_key  # noqa: E402

stored = "b44e5aff282a41ddb982a0e096742424_NMC523-10.1149_2.1701713jes.txt"
check("curve_label strips the uuid but keeps underscores in the real name",
      curve_label("/tmp/pybep_x/cathode/" + stored) == "NMC523-10.1149_2.1701713jes",
      curve_label(stored))
check("original_name leaves an unprefixed name alone",
      original_name("LFP-10.1149_1.3567007.txt") == "LFP-10.1149_1.3567007.txt",
      original_name("LFP-10.1149_1.3567007.txt"))
check("original_name ignores a non-hex 32-char prefix",
      original_name("z" * 32 + "_data.txt") == "z" * 32 + "_data.txt")
check("duplicate filenames get distinct keys, not silent overwrites",
      _unique_key("LFP", {"LFP": 1}) == "LFP (2)"
      and _unique_key("LFP", {"LFP": 1, "LFP (2)": 1}) == "LFP (3)"
      and _unique_key("LFP", {}) == "LFP")

# --- built-in curve library -------------------------------------------
from pybep.core import library_names  # noqa: E402

lib_cathodes = library_names("cathode")
lib_anodes = library_names("anode")

body = client.get("/").get_data(as_text=True)
check("front page lists every built-in cathode",
      all(n in body for n in lib_cathodes), lib_cathodes)
check("front page lists every built-in anode",
      all(n in body for n in lib_anodes), lib_anodes)
ticked = re.findall(r'<input type="checkbox" name="library_(\w+)" value="[^"]+" checked/>', body)
check("every built-in curve has a box, ticked by default",
      ticked.count("cathodes") == len(lib_cathodes)
      and ticked.count("anodes") == len(lib_anodes),
      (ticked.count("cathodes"), ticked.count("anodes")))

panels = re.findall(r"<details class=\"candidate-panel\"([^>]*)>", body)
check("candidate lists start collapsed, keeping the page short",
      len(panels) == 2 and not any("open" in p for p in panels), panels)
badges = dict(re.findall(
    r'data-count-for="library_(\w+)">\s*(\d+ of \d+) selected', body))
check("the collapsed summary says how many are selected",
      badges == {"cathodes": f"{len(lib_cathodes)} of {len(lib_cathodes)}",
                 "anodes": f"{len(lib_anodes)} of {len(lib_anodes)}"}, badges)

file_inputs = {re.search(r'name="(\w+)"', tag).group(1): tag
               for tag in re.findall(r"<input[^>]*type=\"file\"[^>]*>", body)}
check("the battery file is still required",
      "required" in file_inputs["battery_file"], file_inputs["battery_file"])
check("candidate uploads are optional now",
      "required" not in file_inputs["cathode_files"]
      and "required" not in file_inputs["anode_files"],
      file_inputs["cathode_files"])

resp = client.get(f"/curve/cathode/{lib_cathodes[0]}")
check("curve preview returns a PNG",
      resp.status_code == 200 and resp.data[:8] == b"\x89PNG\r\n\x1a\n",
      (resp.status_code, resp.data[:8]))
check("curve preview is cacheable", "max-age" in resp.headers.get("Cache-Control", ""))
check("unknown curve name 404s", client.get("/curve/cathode/nope").status_code == 404)
check("unknown curve type 404s",
      client.get(f"/curve/battery/{lib_cathodes[0]}").status_code == 404)
check("curve preview refuses to walk out of data/",
      client.get("/curve/cathode/..%2f..%2fsetup.cfg").status_code == 404)

# A run using only the built-in curves: the battery file is the sole upload.
resp = client.post("/upload", data={
    "battery_file": [upload_file(battery)],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0], "not-a-real-curve"],
    "iterations": "1",
    "slider_a": "1",
}, content_type="multipart/form-data")
check("library-only run reaches the confirm page", resp.status_code == 200, resp.status_code)
body = resp.get_data(as_text=True)
lib_ids = re.findall(r'name="soc_([0-9a-f]{32})"', body)
check("only the uploaded battery file needs confirming", len(lib_ids) == 1, len(lib_ids))

resp = client.post("/confirm", data={f"soc_{lib_ids[0]}": "0",
                                     f"ocv_{lib_ids[0]}": "1"})
check("library-only run produces results", resp.status_code == 200, resp.status_code)
body = resp.get_data(as_text=True)
check("library-only run reports a built-in cathode as the winner",
      lib_cathodes[0] in body, lib_cathodes[0])
compared = re.search(r"(\d+) cathode\(s\) (?:&times;|×) (\d+) anode\(s\)", body)
check("the invented library name was dropped, not compared",
      compared is not None and compared.groups() == ("1", "1"),
      compared.groups() if compared else "no 'candidates compared' line")

payload = json.loads(client.get("/download").get_data())
check("library-only JSON names the built-in curves",
      payload["Best Cathode Data ID"] == lib_cathodes[0]
      and payload["Best Anode Data ID"] == lib_anodes[0],
      (payload["Best Cathode Data ID"], payload["Best Anode Data ID"]))

# --- downloading the result in each format -----------------------------
import pandas as pd  # noqa: E402
from pybep.core import RESULT_FORMATS  # noqa: E402

offered = re.findall(r'<option value="(\w+)"', body)
check("the results page offers every format the server accepts",
      set(offered) == set(RESULT_FORMATS), (offered, RESULT_FORMATS))

resp = client.get("/download?format=csv")
check("CSV download succeeds",
      resp.status_code == 200 and "text/csv" in resp.headers["Content-Type"],
      (resp.status_code, resp.headers.get("Content-Type")))
check("CSV is offered under a .csv filename",
      "pybep_result.csv" in resp.headers.get("Content-Disposition", ""),
      resp.headers.get("Content-Disposition"))

csv_text = resp.get_data(as_text=True)
check("CSV carries the summary as comment lines",
      csv_text.startswith("# Best Cathode Data ID:")
      and "# Lowest RMSD:" in csv_text,
      csv_text[:120])

csv_frame = pd.read_csv(io.StringIO(csv_text), comment="#")
check("CSV reloads as a table with every curve",
      {"Battery SOC", "Battery OCV", "Calculated Battery OCV",
       "Cathode SOC", "Cathode OCP", "Cathode SOC full",
       "Anode SOC", "Anode OCP", "Anode OCP full"} <= set(csv_frame.columns),
      sorted(csv_frame.columns))
check("CSV pads the ragged curves instead of truncating them",
      len(csv_frame) == max(len(payload[c]) for c in csv_frame.columns)
      and csv_frame["Battery SOC"].notna().sum() == len(payload["Battery SOC"])
      and csv_frame["Anode OCP full"].notna().sum() == len(payload["Anode OCP full"]),
      (len(csv_frame), len(payload["Battery SOC"]), len(payload["Anode OCP full"])))
check("CSV numbers survive the round trip",
      abs(csv_frame["Battery OCV"][0] - payload["Battery OCV"][0]) < 1e-9,
      (csv_frame["Battery OCV"][0], payload["Battery OCV"][0]))

resp = client.get("/download?format=xlsx")
check("XLSX download succeeds",
      resp.status_code == 200 and "spreadsheet" in resp.headers["Content-Type"],
      (resp.status_code, resp.headers.get("Content-Type")))
check("XLSX is a real zip-based workbook", resp.data[:2] == b"PK", resp.data[:4])

book = pd.read_excel(io.BytesIO(resp.data), sheet_name=None)
check("XLSX has a Summary and a Curves sheet",
      set(book) == {"Summary", "Curves"}, sorted(book))
check("XLSX summary names the winning curves",
      dict(zip(book["Summary"]["Result"], book["Summary"]["Value"]))
      ["Best Cathode Data ID"] == payload["Best Cathode Data ID"],
      book["Summary"].to_dict("records"))
check("XLSX curves match the JSON",
      abs(book["Curves"]["Battery OCV"][0] - payload["Battery OCV"][0]) < 1e-9
      and book["Curves"]["Anode OCP full"].notna().sum()
      == len(payload["Anode OCP full"]),
      book["Curves"].shape)

resp = client.get("/download?format=json")
check("JSON download still works and is unchanged",
      json.loads(resp.get_data()) == payload, resp.status_code)
check("the default format is still JSON",
      json.loads(client.get("/download").get_data()) == payload)
check("an unknown format is refused",
      client.get("/download?format=exe").status_code == 400,
      client.get("/download?format=exe").status_code)
check("every advertised format downloads",
      all(client.get(f"/download?format={f}").status_code == 200
          for f in RESULT_FORMATS), RESULT_FORMATS)

# --- a run with no uploads at all --------------------------------------
from pybep.core import battery_library_names  # noqa: E402

lib_batteries = battery_library_names()
home = client.get("/").get_data(as_text=True)
options = re.findall(r'<option value="([^"]*)"', home)
check("the battery dropdown offers upload first, then every bundled curve",
      options[0] == "" and options[1:] == lib_batteries,
      options)

resp = client.post("/upload", data={
    "battery_choice": "NMC811vsGraphite_OCV-10.1016_j.xcrp.2020.100253",
    "library_cathodes": lib_cathodes,      # all seven
    "library_anodes": lib_anodes,          # both
    "iterations": "1",
    "slider_a": "1",
}, content_type="multipart/form-data")
check("an all-library run skips the confirm step entirely",
      resp.status_code == 200 and "Confirm columns" not in resp.get_data(as_text=True),
      resp.status_code)
body = resp.get_data(as_text=True)
check("an all-library run goes straight to results",
      'src="data:image/png;base64,' in body and "Lowest RMSD" in body)
compared = re.search(r"(\d+) cathode\(s\) (?:&times;|×) (\d+) anode\(s\)", body)
check("the whole library was compared",
      compared.groups() == (str(len(lib_cathodes)), str(len(lib_anodes))),
      compared.groups() if compared else None)
check("the full library picks the physically right cathode for this cell",
      "NMC811" in json.loads(client.get("/download").get_data())["Best Cathode Data ID"],
      json.loads(client.get("/download").get_data())["Best Cathode Data ID"])

resp = client.post("/upload", data={
    "battery_choice": "../../setup.cfg",
    "library_cathodes": lib_cathodes[:1],
    "library_anodes": lib_anodes[:1],
}, content_type="multipart/form-data", follow_redirects=True)
check("an invented battery name is refused",
      "one of the available files" in resp.get_data(as_text=True),
      re.findall(r"<li>(.*?)</li>", resp.get_data(as_text=True)))

# --- adjust settings and re-run ----------------------------------------
# Re-run the uploaded-battery flow so there is an upload to reuse.
resp = client.post("/upload", data={
    "battery_file": [upload_file(battery)],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1",
    "slider_a": "1",
}, content_type="multipart/form-data")
fid = re.findall(r'name="soc_([0-9a-f]{32})"', resp.get_data(as_text=True))[0]
body = client.post("/confirm", data={f"soc_{fid}": "0", f"ocv_{fid}": "1"}) \
             .get_data(as_text=True)
check("the results page offers a re-run without re-picking files",
      'href="/adjust"' in body, [l for l in body.splitlines() if "adjust" in l])

resp = client.get("/adjust")
adjust_page = resp.get_data(as_text=True)
check("the adjust page loads", resp.status_code == 200, resp.status_code)
check("it says what is being reused",
      "1 cathode(s) &times; 1 anode(s)" in adjust_page
      and os.path.splitext(os.path.basename(battery))[0] in adjust_page,
      [l.strip() for l in adjust_page.splitlines() if "result-item" in l][:2])
check("the sliders start from the settings just used",
      'value="1" name="iterations"' in adjust_page
      and 'value="1.0" name="slider_a"' in adjust_page,
      re.findall(r'<input id="slider-\w+"[^>]*value="[^"]*"[^>]*>', adjust_page))

with client.session_transaction() as sess:
    workdir = sess["workdir"]
check("the uploaded file survives so it can be reused",
      os.path.isdir(os.path.join(workdir, "battery")), workdir)

resp = client.post("/adjust", data={"iterations": "2", "slider_a": "0.4"})
body = resp.get_data(as_text=True)
check("re-running produces fresh results", resp.status_code == 200
      and 'src="data:image/png;base64,' in body, resp.status_code)
check("the new settings were applied, not the old ones",
      "2 iteration(s)" in body and "battery 0.40" in body
      and "diff. capacity 0.60" in body,
      [l.strip() for l in body.splitlines() if "iteration(s)" in l])
check("the same curves were reused",
      re.search(r"1 cathode\(s\) (?:&times;|×) 1 anode\(s\)", body) is not None)
check("the downloadable result survives",
      os.path.exists(os.path.join(workdir, "result.json")), workdir)

# Starting over must wipe the previous session's uploads.
old_workdir = workdir
client.post("/upload", data={
    "battery_choice": lib_batteries[0],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1",
}, content_type="multipart/form-data")
check("a new run wipes the previous run's uploads",
      not os.path.isdir(old_workdir), old_workdir)

client.get("/")  # drop any flash left over before the checks below
resp = client.get("/adjust", follow_redirects=True)
check("adjust after the files are gone redirects instead of erroring",
      resp.status_code == 200, resp.status_code)

# --- deselecting everything is refused --------------------------------
resp = client.post("/upload", data={
    "battery_file": [upload_file(battery)],
    "iterations": "1",
}, content_type="multipart/form-data", follow_redirects=True)
check("a run with no candidates at all is rejected",
      "at least one cathode candidate" in resp.get_data(as_text=True))

resp = client.post("/upload", data={
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1",
}, content_type="multipart/form-data", follow_redirects=True)
check("a run with no battery curve at all is rejected",
      "choose a battery OCV curve" in resp.get_data(as_text=True),
      re.findall(r"<li>(.*?)</li>", resp.get_data(as_text=True)))

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
