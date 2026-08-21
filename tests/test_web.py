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

# --- instructions -----------------------------------------------------
resp = client.get("/help")
help_page = resp.get_data(as_text=True)
check("instructions page renders", resp.status_code == 200, resp.status_code)

import re  # noqa: E402
section_order = re.findall(r'<h2 id="([\w-]+)"', help_page)
anchors = set(section_order)
check("the contents list points only at sections that exist",
      set(re.findall(r'<a href="#([\w-]+)"', help_page)) == anchors, anchors)

# Ordered for someone arriving for the first time: try it, then run it
# properly, then read the answer. Reference material comes after all that.
check("the instructions lead with doing, not with file rules",
      section_order == ["quickstart", "optimization", "results",
                        "files", "formatting", "limits"], section_order)
check("the contents list runs in the same order as the page",
      re.findall(r'<a href="#([\w-]+)"', help_page) == section_order,
      re.findall(r'<a href="#([\w-]+)"', help_page))

for page, path in (("front", "/"), ("format", "/format")):
    body = client.get(path).get_data(as_text=True)
    target = re.search(r'class="help-dot" href="/help#([\w-]+)"', body)
    check(f"the {page} page has a ? linking to a real section",
          target is not None and target.group(1) in anchors,
          target.group(1) if target else "no help-dot")
    check(f"the {page} page's ? opens in a new tab, so a chosen file survives",
          'class="help-dot"' in body
          and 'target="_blank"' in body.split('class="help-dot"')[1][:200])

check("instructions appear in the nav on every page",
      all('>Instructions</a>' in client.get(p).get_data(as_text=True)
          for p in ("/", "/format", "/help")))
check("the nav marks the instructions tab as current",
      re.search(r'href="/help"\s+class="is-active"', help_page) is not None,
      re.findall(r'<a href="/\w*"[^>]*class="[^"]*"', help_page))


def active_tab(html):
    """Which nav tab the page marks as current."""
    m = re.search(r'<a href="([^"]*)"\s+class="is-active"', html)
    return m.group(1) if m else None


check("the format tab stays lit through its own sub-pages",
      active_tab(client.get("/format").get_data(as_text=True)) == "/format")

# --- hardening for a public deployment --------------------------------
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from pybep.web import pipeline  # noqa: E402
from pybep.web.routes import _safe_join  # noqa: E402

check("the session key is never a fixed literal",
      app.config["SECRET_KEY"] != "dev-secret-key-change-me"
      and len(app.config["SECRET_KEY"]) >= 32,
      "a key committed to a public repo lets anyone forge a session cookie")

# The cookie names a directory that the server reads files from AND
# deletes wholesale, so it must never be taken at face value. Everything
# below is checked without ever handing a real path to the deleter.
for hostile in (ROOT, os.path.dirname(ROOT), "C:\\Windows", "/etc",
                os.path.join(tempfile.gettempdir(), ".."),
                os.path.join(tempfile.gettempdir(), "notours"), "", None, 5):
    check(f"a workdir of {str(hostile)[:28]!r} is not treated as ours",
          not pipeline.is_session_workdir(hostile), hostile)

own = tempfile.mkdtemp(prefix=pipeline.WORKDIR_PREFIX)
check("a directory we really made is recognised",
      pipeline.is_session_workdir(own), own)
shutil.rmtree(own, ignore_errors=True)

# create_session_workdir deletes what it is given; prove it refuses.
guard_dir = tempfile.mkdtemp(prefix="not_pybep_")
open(os.path.join(guard_dir, "precious.txt"), "w").write("keep me")
made = pipeline.create_session_workdir(guard_dir)
check("create_session_workdir will not delete a directory it did not make",
      os.path.isfile(os.path.join(guard_dir, "precious.txt")), guard_dir)
shutil.rmtree(guard_dir, ignore_errors=True)
shutil.rmtree(made, ignore_errors=True)

# The download routes must refuse a workdir outside the temp root. Point
# the cookie at a path that does not exist, so nothing can be harmed.
with client.session_transaction() as sess:
    sess["workdir"] = os.path.join(ROOT, "no-such-dir")
    sess["format_outputs"] = ["../setup.cfg"]
check("a workdir outside the temp root serves nothing",
      client.get("/format/file/0").status_code == 404
      and client.get("/download").status_code == 404
      and client.get("/format/download").status_code == 404)

# /format/download rebuilds the path from the session directory, so a
# cookie naming some other file must get nowhere. The bait is a file we
# made ourselves, and the route only ever reads.
bait_dir = tempfile.mkdtemp(prefix="not_pybep_")
bait = os.path.join(bait_dir, "bait.zip")
open(bait, "w").write("should never be served")
empty_workdir = pipeline.create_session_workdir()
with client.session_transaction() as sess:
    sess["workdir"] = empty_workdir
    sess["format_zip"] = bait          # the key the route used to trust
check("the zip route ignores a path named by the cookie",
      client.get("/format/download").status_code == 404)
shutil.rmtree(bait_dir, ignore_errors=True)
shutil.rmtree(empty_workdir, ignore_errors=True)

for escape in ("../../setup.cfg", "..\\..\\setup.cfg", "/etc/passwd"):
    joined = _safe_join(tempfile.gettempdir(), escape)
    check(f"_safe_join contains {escape!r}",
          joined is None
          or joined.startswith(os.path.realpath(tempfile.gettempdir()) + os.sep),
          joined)
check("_safe_join allows an ordinary name",
      _safe_join(tempfile.gettempdir(), "result.json") is not None)

# Abandoned sessions must not pile up: a session only cleans up its own
# predecessor, so nothing else would ever remove them.
stale = tempfile.mkdtemp(prefix=pipeline.WORKDIR_PREFIX)
fresh = tempfile.mkdtemp(prefix=pipeline.WORKDIR_PREFIX)
os.utime(stale, (0, 0))
pipeline.purge_stale_workdirs()
check("abandoned working directories are swept up", not os.path.isdir(stale), stale)
check("directories still in use are left alone", os.path.isdir(fresh), fresh)
shutil.rmtree(fresh, ignore_errors=True)

# Error pages a real visitor meets.
for code, path in ((404, "/no-such-page"), (405, "/upload")):
    r = client.get(path)
    body = r.get_data(as_text=True)
    check(f"{code} shows the site's own page with a way back",
          r.status_code == code and "PyBEP" in body and "Run optimization" in body,
          r.status_code)

r = client.post("/upload", content_type="multipart/form-data", data={
    "battery_file": [(io.BytesIO(b"x" * (33 * 1024 * 1024)), "huge.txt")]})
body = r.get_data(as_text=True)
check("an oversized upload explains the limit instead of showing a bare 413",
      r.status_code == 413 and "PyBEP" in body and "MB" in body, r.status_code)

client.get("/")  # clear any flash the probes left behind

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
check("confirm redirects rather than answering with the results",
      resp.status_code == 302 and resp.headers["Location"].endswith("/"),
      (resp.status_code, resp.headers.get("Location")))
body = client.get("/").get_data(as_text=True)
check("the results are waiting at / afterwards",
      'src="data:image/png;base64,' in body)
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

# Its own client: `client` has results by now, and / shows them.
fresh = app.test_client()
body = fresh.get("/").get_data(as_text=True)
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

check("the front page keeps the results half of the page ready and empty",
      'class="placeholder"' in body and "results appear here" in body.lower(),
      [l.strip() for l in body.splitlines() if "placeholder" in l])
check("nothing is offered for re-running before anything has run",
      "formaction" not in body and "carried-note" not in body,
      [l.strip() for l in body.splitlines()
       if "formaction" in l or "carried-note" in l])

file_inputs = {re.search(r'name="(\w+)"', tag).group(1): tag
               for tag in re.findall(r"<input[^>]*type=\"file\"[^>]*>", body)}
check("the battery file is still required",
      "required" in file_inputs["battery_file"], file_inputs["battery_file"])
check("candidate uploads are optional now",
      "required" not in file_inputs["cathode_files"]
      and "required" not in file_inputs["anode_files"],
      file_inputs["cathode_files"])

resp = fresh.get(f"/curve/cathode/{lib_cathodes[0]}")
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
                                     f"ocv_{lib_ids[0]}": "1"},
                   follow_redirects=True)
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
home = fresh.get("/").get_data(as_text=True)
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
      resp.status_code == 302, resp.status_code)
body = client.get("/").get_data(as_text=True)
check("an all-library run goes straight to results",
      "Confirm columns" not in body
      and 'src="data:image/png;base64,' in body and "Lowest RMSD" in body)
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
    # Deliberately not the form's defaults (1 iteration, weight 1.00), so
    # that a form which ignored the last run would fail the check below.
    "iterations": "2",
    "slider_a": "0.35",
}, content_type="multipart/form-data")
fid = re.findall(r'name="soc_([0-9a-f]{32})"', resp.get_data(as_text=True))[0]
body = client.post("/confirm", data={f"soc_{fid}": "0", f"ocv_{fid}": "1"},
                   follow_redirects=True) \
             .get_data(as_text=True)
check("the whole run form comes back beside the results",
      'action="/upload"' in body and 'id="battery_choice"' in body,
      [l.strip() for l in body.splitlines() if "<form" in l])
check("it comes back holding the choices that produced them",
      f'value="{lib_cathodes[0]}" checked/>' in body
      and f'value="{lib_cathodes[1]}" checked/>' not in body,
      re.findall(r'name="library_cathodes" value="[^"]+" ?(?:checked)?/>', body))
check("the sliders start from the settings just used",
      'value="2" name="iterations"' in body
      and 'value="0.35" name="slider_a"' in body,
      re.findall(r'<input id="slider-\w+"[^>]*value="[^"]*"[^>]*>', body))
check("the form carries the iteration ceiling with it",
      f'max="{MAX_ITERATIONS}" step="1"' in body,
      re.findall(r'<input id="slider-iterations"[^>]*>', body))
# A browser will not refill a file input, so pressing Continue would run
# without the uploaded curve. Both halves of the way out have to be there.
check("the uploaded file the form cannot show is named, not silently dropped",
      "carried-note" in body
      and os.path.splitext(os.path.basename(battery))[0] in body,
      [l.strip() for l in body.splitlines() if "carried-note" in l])
check("and a re-run that keeps it is offered alongside",
      'formaction="/adjust"' in body and "formnovalidate" in body,
      [l.strip() for l in body.splitlines() if "formaction" in l])
check("no settings page of its own is left to navigate to",
      client.get("/adjust").status_code == 405 and 'href="/adjust"' not in body)

with client.session_transaction() as sess:
    workdir = sess["workdir"]
check("the uploaded file survives so it can be reused",
      os.path.isdir(os.path.join(workdir, "battery")), workdir)

resp = client.post("/adjust", data={"iterations": "3", "slider_a": "0.4"},
                   follow_redirects=True)
body = resp.get_data(as_text=True)
check("re-running produces fresh results", resp.status_code == 200
      and 'src="data:image/png;base64,' in body, resp.status_code)
check("the new settings were applied, not the old ones",
      "3 iteration(s)" in body and "battery 0.40" in body
      and "diff. capacity 0.60" in body,
      [l.strip() for l in body.splitlines() if "iteration(s)" in l])
check("the same curves were reused",
      re.search(r"1 cathode\(s\) (?:&times;|×) 1 anode\(s\)", body) is not None)
check("the downloadable result survives",
      os.path.exists(os.path.join(workdir, "result.json")), workdir)

# Starting over must wipe the previous session's uploads.
old_workdir = workdir
lib_only = client.post("/upload", data={
    "battery_choice": lib_batteries[0],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1",
}, content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
check("a new run wipes the previous run's uploads",
      not os.path.isdir(old_workdir), old_workdir)
check("a library-only run has nothing to carry, so offers no extra re-run",
      "carried-note" not in lib_only and "formaction" not in lib_only,
      [l.strip() for l in lib_only.splitlines()
       if "carried-note" in l or "formaction" in l])

client.get("/")  # drop any flash left over before the checks below
resp = client.post("/adjust", data={"iterations": "1", "slider_a": "0.5"},
                   follow_redirects=True)
check("a library-only run re-runs after the old uploads were wiped",
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
check("format -> column confirmation", resp.status_code == 200, resp.status_code)
fmt_page = resp.get_data(as_text=True)
check("formatting asks which column is which, instead of guessing silently",
      "Confirm columns" in fmt_page
      and 'name="soc_0"' in fmt_page and 'name="soc_1"' in fmt_page,
      [l.strip() for l in fmt_page.splitlines() if 'name="soc_' in l])
check("each file gets its own preview rows to check the guess against",
      fmt_page.count('class="preview-table"') == 2,
      fmt_page.count('class="preview-table"'))
check("the detected guess is pre-selected",
      fmt_page.count("selected") >= 4, fmt_page.count("selected"))

resp = client.post("/format/confirm", data={
    "soc_0": "0", "ocv_0": "1", "soc_1": "0", "ocv_1": "1"})
check("confirm -> results page", resp.status_code == 200, resp.status_code)
done = resp.get_data(as_text=True)
check("format page reports success", "Formatting complete" in done)

# Each converted file is downloadable on its own.
links = re.findall(r'href="(/format/file/\d+)"', done)
check("every converted file has its own download link", len(links) == 2, links)
single = client.get(links[0])
check("a single formatted file downloads directly, not as a zip",
      single.status_code == 200
      and "zip" not in single.headers.get("Content-Type", ""),
      (single.status_code, single.headers.get("Content-Type")))
check("the download keeps the user's own filename",
      "FORMATTED" in single.headers.get("Content-Disposition", "")
      and not re.search(r"[0-9a-f]{32}_",
                        single.headers.get("Content-Disposition", "")),
      single.headers.get("Content-Disposition"))
rows = single.get_data(as_text=True).strip().splitlines()
check("the single file has 1001 rows of 'x<TAB>y'",
      len(rows) == 1001 and len(rows[0].split("	")) == 2, len(rows))

check("a file index outside this session's output 404s",
      client.get("/format/file/99").status_code == 404)

resp = client.get("/format/download")
check("format zip still downloads for the whole batch",
      resp.status_code == 200, resp.status_code)
with zipfile.ZipFile(io.BytesIO(resp.get_data())) as zf:
    names = zf.namelist()
    check("zip holds both formatted files", len(names) == 2, names)
    check("zip entries use the original names (no uuid prefix)",
          not any(re.match(r"[0-9a-f]{32}_", n) for n in names)
          and all("FORMATTED" in n for n in names), names)
    first = zf.read(names[0]).decode().strip().splitlines()
    check("formatted file has 1001 rows", len(first) == 1001, len(first))
    check("formatted rows are 'x<TAB>y'", len(first[0].split("	")) == 2, first[0])

# The columns the user picked must actually be used, not re-guessed.
swapped = client.post("/format", data={
    "data_files": [upload_file(cathode)],
    "curve_type": "cathode",
}, content_type="multipart/form-data")
check("single-file batch also asks first",
      'name="soc_0"' in swapped.get_data(as_text=True))
resp = client.post("/format/confirm", data={"soc_0": "1", "ocv_0": "0"})
body = resp.get_data(as_text=True)
link = re.search(r'href="(/format/file/\d+)"', body)
check("a deliberately swapped choice is honoured, not overridden",
      link is not None, [l for l in body.splitlines() if "banner" in l][:3])
if link:
    swapped_rows = client.get(link.group(1)).get_data(as_text=True).strip().splitlines()
    normal_first = rows[0].split("	")
    swapped_first = swapped_rows[0].split("	")
    check("swapping SOC/OCV really produces different numbers",
          swapped_first != normal_first, (normal_first, swapped_first))

check("only one file offered, so no zip button is shown",
      "as zip" not in body, [l for l in body.splitlines() if "zip" in l])
# A real multi-column export: the picker must offer every column, and the
# file must convert using the two the user names.
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
indexed = os.path.join(FIXTURES, "indexed_three_column.txt")
if os.path.exists(indexed):
    resp = client.post("/format", data={
        "data_files": [upload_file(indexed)],
        "curve_type": "cathode",
    }, content_type="multipart/form-data")
    page = resp.get_data(as_text=True)
    check("a 3-column file offers all three columns in the picker",
          page.count('<option value="2"') == 2, page.count('<option value="2"'))
    preselected = re.findall(r'<option value="(\d+)" selected>', page)
    check("the index column is not pre-selected; SOC and OCV are",
          preselected == ["1", "2"], preselected)

    resp = client.post("/format/confirm", data={"soc_0": "1", "ocv_0": "2"})
    body3 = resp.get_data(as_text=True)
    link = re.search(r'href="(/format/file/\d+)"', body3)
    check("a 3-column file converts once the columns are named",
          link is not None, [l for l in body3.splitlines() if "banner" in l][:3])
    if link:
        rows3 = client.get(link.group(1)).get_data(as_text=True).strip().splitlines()
        first, last = rows3[0].split("\t"), rows3[-1].split("\t")
        check("the converted 3-column file is 1001 SOC/OCV pairs",
              len(rows3) == 1001 and abs(float(first[0])) < 1e-6
              and abs(float(last[0]) - 1) < 1e-6 and 2.4 < float(first[1]) < 2.6,
              (len(rows3), first, last))

# --- results stay put while you look at the other tabs -----------------
# One run, then nothing but navigation. None of it is allowed to lose the
# answer, which is why a run redirects to a page that reads the results
# back off disk instead of rendering them into the POST response.
sticky = app.test_client()
r = sticky.post("/upload", content_type="multipart/form-data", data={
    "battery_choice": lib_batteries[0],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1", "slider_a": "0.5"})
check("a run answers with a redirect, not with the page itself",
      r.status_code == 302, r.status_code)


def has_results(a_client):
    body = a_client.get("/").get_data(as_text=True)
    return 'src="data:image/png;base64,' in body and "Lowest RMSD" in body


check("the results are at / straight after the run", has_results(sticky))
check("and again on a plain refresh", has_results(sticky))
sticky.get("/help")
check("the instructions tab does not take them away", has_results(sticky))
sticky.get("/format")
check("nor does opening Format Data", has_results(sticky))

# Format Data used to share, and wipe, the directory the results live in.
r = sticky.post("/format", content_type="multipart/form-data",
                data={"data_files": [upload_file(cathode)],
                      "curve_type": "cathode"})
check("a format job still runs alongside a result", r.status_code == 200,
      r.status_code)
r = sticky.post("/format/confirm", data={"soc_0": "0", "ocv_0": "1"})
fmt_links = re.findall(r'href="(/format/file/\d+)"', r.get_data(as_text=True))
check("the formatted file is offered for download", len(fmt_links) == 1, fmt_links)
check("running a format job leaves the results alone", has_results(sticky))
check("and the two flows keep their own directories",
      sticky.get(fmt_links[0]).status_code == 200
      and sticky.get("/download").status_code == 200,
      (sticky.get(fmt_links[0]).status_code, sticky.get("/download").status_code))

# The stored view is JSON, and JSON has no tuples: without a conversion on
# the way back the page would read [1, 701, 82, 982] where PyBEP has
# always read (1, 701, 82, 982).
# Not `body`: a check further down still wants the format page held there.
sticky_page = sticky.get("/").get_data(as_text=True)
params = re.search(r'Best parameters:.*?<span class="value">([^<]+)',
                   sticky_page, re.S)
check("the best parameters still read as a tuple after the round trip",
      params is not None and params.group(1).strip().startswith("("),
      params.group(1).strip() if params else "no 'Best parameters' row")

# --- recovering instead of crashing -----------------------------------
# Both of these were 500s: a run whose battery file failed to parse, and a
# session whose temp directory has since been cleaned away.
recov = app.test_client()
recov.post("/upload", content_type="multipart/form-data", data={
    "battery_file": [(io.BytesIO(b"not data\nnor this\n"), "bad.txt")],
    "cathode_files": [upload_file(cathode)],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1"})
r = recov.post("/adjust", data={"iterations": "1", "slider_a": "0.5"},
               follow_redirects=True)
check("re-running a run with no usable battery curve redirects, not crashes",
      r.status_code == 200
      and "readable battery OCV file" in r.get_data(as_text=True),
      r.status_code)

gone = app.test_client()
r = gone.post("/upload", content_type="multipart/form-data", data={
    "battery_file": [upload_file(battery)],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1"})
fid_gone = re.findall(r'name="soc_([0-9a-f]{32})"', r.get_data(as_text=True))[0]
with gone.session_transaction() as sess:
    doomed = sess["workdir"]
check("the doomed path really is one of ours before removing it",
      pipeline.is_session_workdir(doomed), doomed)
shutil.rmtree(doomed, ignore_errors=True)
r = gone.post("/confirm", follow_redirects=True,
              data={f"soc_{fid_gone}": "0", f"ocv_{fid_gone}": "1"})
check("confirming after the temp directory vanished redirects, not crashes",
      r.status_code == 200 and "session expired" in r.get_data(as_text=True),
      r.status_code)

check("the format tab is still lit on the confirm and results pages",
      active_tab(body) == "/format"
      and active_tab(swapped.get_data(as_text=True)) == "/format",
      (active_tab(body), active_tab(swapped.get_data(as_text=True))))

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
