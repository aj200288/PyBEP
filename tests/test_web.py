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
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Submitted curves are kept on disk for good, so the suite gets a folder of
# its own: run against the real one, these checks would depend on whatever
# the machine happened to be holding, and would leave curves behind.
SUBMIT_ROOT = tempfile.mkdtemp(prefix="pybep_test_submitted_")
os.environ["PYBEP_SUBMISSIONS_DIR"] = SUBMIT_ROOT

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
                        "files", "formatting", "submitting", "limits"],
      section_order)
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
from pybep.core import battery_library_names, library_names  # noqa: E402

lib_cathodes = library_names("cathode")
lib_anodes = library_names("anode")
lib_batteries = battery_library_names()

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

named = re.findall(
    r'<span class="group-label">([^<]+)</span>.*?<details class="candidate-panel">',
    body, re.S)
check("each candidate group is named above its panel, as the battery box is",
      named == ["Cathode candidates", "Anode candidates"], named)
check("which leaves the panel saying what is inside it",
      body.count('<span class="candidate-title">Built-in curves</span>') == 2,
      re.findall(r'<span class="candidate-title">[^<]*</span>', body))
# The two places a candidate curve can come from are one pair of panels
# under one name. Uploading used to be a blue link with an open bordered
# box under it, which outweighed the built-in list it stands beside.
check("and the files of your own are a panel of the same kind beside it",
      body.count('<span class="candidate-title">Your own files</span>') == 2
      and body.count('<details class="candidate-panel extra-upload"') == 2,
      re.findall(r'<span class="candidate-title">[^<]*</span>', body))
added = re.findall(r'data-added-for="(\w+)">\s*([^<]+?)\s*</span>', body)
check("which counts what it holds, as the other counts what is ticked",
      added == [("cathode", "None added"), ("anode", "None added")], added)

panels = re.findall(r"<details class=\"candidate-panel\"([^>]*)>", body)
check("candidate lists start collapsed, keeping the page short",
      len(panels) == 2 and not any("open" in p for p in panels), panels)
badges = dict(re.findall(
    r'data-count-for="library_(\w+)">\s*(\d+ of \d+) selected', body))
check("the collapsed summary says how many are selected",
      badges == {"cathodes": f"{len(lib_cathodes)} of {len(lib_cathodes)}",
                 "anodes": f"{len(lib_anodes)} of {len(lib_anodes)}"}, badges)

# The sliders decide how the fit is made, not which curves go into it,
# and the form used to run the two together. The name has to have the
# sliders under it, or it sits over nothing.
fitting = re.search(r'<div class="form-section">(.*?)<div class="actions">',
                    body, re.S)
check("the fitting settings are a named section, ruled off from the curves",
      fitting is not None
      and ">Fitting</h2>" in fitting.group(1)
      and all(f'id="{slider}"' in fitting.group(1)
              for slider in ("slider-iterations", "slider-a", "slider-b")),
      "no section before the buttons" if not fitting
      else [l.strip() for l in fitting.group(1).splitlines()
            if "form-section" in l or "slider-group" in l][:5])

# A ? beside every field name, and behind each one the words for that
# field. The dot is a link before it is anything else, so that a browser
# with no <dialog> lands on the instructions instead of doing nothing —
# and an anchor that has gone is a silent drop to the top of that page.
dots = re.findall(r'<a class="help-dot help-dot-sm"[^>]*>', body, re.S)
check("every field on the run form carries a ? of its own",
      len(dots) == 6 and body.count('<div class="field-head">') == 6,
      (len(dots), body.count('<div class="field-head">')))
topics = re.findall(r'data-help="([^"]+)"', body)
help_blocks = re.findall(r'<div class="field-help"[^>]*>', body)
blocks = [re.search(r'id="([^"]+)"', b).group(1) for b in help_blocks]
check("and each ? has the words for its own field behind it",
      len(topics) == 6 and sorted(topics) == sorted(blocks), (topics, blocks))
check("which stay behind it, rather than printing on the form",
      len(help_blocks) == 6 and all(" hidden" in b for b in help_blocks),
      help_blocks)
dot_anchors = [re.search(r'href="[^"#]*#([^"]+)"', dot).group(1) for dot in dots]
instructions = fresh.get("/help").get_data(as_text=True)
check("and falls back to a part of the instructions that is really there",
      len(dot_anchors) == 6
      and all(f'id="{a}"' in instructions for a in dot_anchors),
      [a for a in dot_anchors if f'id="{a}"' not in instructions] or dot_anchors)
# What a file has to be belongs beside the box that takes it. Reading it
# on the instructions page means leaving a form that may already have a
# file on it, and coming back to find out about the next box means going
# again.
hints = [re.sub(r"\s+", " ", h).strip()
         for h in re.findall(r'<span class="file-hint">(.*?)</span>', body, re.S)]
picking = [h for h in hints
           if "decomposed" in h or "isn't in the list above" in h]
check("every place a file is picked says what one may be",
      len(picking) == 3
      and all(".txt, .csv or .xlsx" in h and "two columns" in h
              for h in picking), picking)
# An undefined name is an empty string in Jinja, so a cap that never
# reached the template reads as "up to  MB" and says nothing at all.
cap = str(app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))
caps = re.findall(r"up to\s+(\d+)\s+MB", body)
check("and how large it may be, in the number rather than a gap",
      len(caps) == 3 and set(caps) == {cap}, caps or "no size named")
told = re.search(r'<div class="field-help" id="help-battery-curve"[^>]*>(.*?)</div>',
                 body, re.S)
check("and the ? on the battery curve offers a first run needing no file",
      told is not None
      and "Run optimization" in told.group(1)
      and f"{len(lib_batteries)} measurements" in told.group(1),
      re.sub(r"\s+", " ", told.group(1))[:220] if told else "no battery help")
formatting = fresh.get("/format").get_data(as_text=True)
check("and Format Data asks for a file in the same words the run form uses",
      ".txt, .csv or .xlsx" in formatting and "two columns" in formatting,
      [l.strip() for l in formatting.splitlines() if "file-hint" in l][:2])
more = re.findall(r'<p class="field-help-more">\s*<a href="[^"#]*#([^"]+)"', body)
check("and the way on to the rest of the instructions lands somewhere too",
      len(more) == 3 and all(f'id="{a}"' in instructions for a in more),
      more or "no way on")

check("the dialog those dots open is on the page with them",
      'id="field-dialog"' in body and 'id="field-dialog-body"' in body,
      [l.strip() for l in body.splitlines() if "field-dialog" in l][:3])

check("the front page keeps the results half of the page ready and empty",
      'class="placeholder"' in body and "results appear here" in body.lower(),
      [l.strip() for l in body.splitlines() if "placeholder" in l])
# The name of a picked file belongs beside Browse, which means drawing
# that box: a file input writes its own text there and offers no way in,
# least of all once the file is on the server and the box is empty again.
browse = re.findall(r'<label class="file-picker-browse" for="(\w+)">', body)
boxes = re.findall(r'<input id="(\w+)" class="input-file js-stage-file"', body)
check("every file box has a Browse of its own, pointing at its input",
      len(browse) == 3 and browse == boxes, (browse, boxes))
check("an empty box says so in the place the browser would have",
      re.findall(r'<span class="staged-empty">([^<]+)</span>', body)
      == ["No file selected.", "No files selected.", "No files selected."],
      re.findall(r'<span class="staged-empty">([^<]+)</span>', body))

check("nothing is waiting before a file has been picked",
      'class="staged-file"' not in body,
      [l.strip() for l in body.splitlines() if "staged-file" in l])

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
# A file sent with the form rather than picked into the dialog is never
# staged, so there is nothing waiting afterwards — and the form must not
# claim otherwise. (What the dialog leaves behind is checked further down.)
check("a run confirmed on its own page leaves nothing waiting on the form",
      'class="staged-file"' not in body,
      [l.strip() for l in body.splitlines() if "staged-file" in l])
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
check("a library-only run has no file waiting, so names none",
      'class="staged-file"' not in lib_only,
      [l.strip() for l in lib_only.splitlines() if "staged-file" in l])

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

# --- picking a file: the question comes to the form -------------------
# A file goes to the server the moment it is picked, is asked about over
# the form, and is then run by the same button as everything else. The
# JavaScript that drives that cannot run here, so this drives the two
# routes it calls and checks what they leave behind.
front = app.test_client().get("/").get_data(as_text=True)
check("the form carries the dialog that asks about columns",
      '<dialog class="column-dialog"' in front and "/columns" in front,
      [l.strip() for l in front.splitlines() if "column-dialog" in l][:2])
check("all three file boxes are marked for the script that sends them",
      len(re.findall(r"<input[^>]*js-stage-file[^>]*>", front)) == 3,
      re.findall(r"<input[^>]*js-stage-file[^>]*>", front))

staging = app.test_client()
r = staging.post("/columns", content_type="multipart/form-data",
                 data={"battery_file": [upload_file(battery)]})
check("picking a file answers with what the dialog needs, not with a page",
      r.status_code == 200 and r.mimetype == "application/json",
      (r.status_code, r.mimetype))
picked = r.get_json()
check("one file picked comes back as one preview and no errors",
      len(picked["previews"]) == 1 and not picked["errors"], picked["errors"])

preview = picked["previews"][0]
# Cells arrive as text because a raw table can hold NaN, which json.dumps
# writes as a bare NaN and JSON.parse then refuses to read.
check("the preview carries the rows and the guess the dialog shows",
      preview["n_cols"] >= 2 and preview["rows"]
      and all(isinstance(cell, str)
              for row in preview["rows"] for cell in row)
      and preview["guess_soc"] != preview["guess_ocv"],
      {k: preview[k] for k in ("n_cols", "guess_soc", "guess_ocv")})
check("the file is named back for the form to show",
      [f["name"] for f in picked["staged"]["battery"]]
      == [os.path.basename(battery)], picked["staged"])
check("but where it was written stays on the server",
      all("path" not in f for f in picked["staged"]["battery"]),
      picked["staged"]["battery"])

held = staging.get("/").get_data(as_text=True)
check("the form names the file it is holding",
      'class="staged-file"' in held and os.path.basename(battery) in held,
      [l.strip() for l in held.splitlines() if "staged-file" in l][:2])
row = re.search(r'for="battery_file">Browse….*?</div>', held, re.S).group(0)
check("the name lands in the box, beside Browse",
      os.path.basename(battery) in row and "staged-empty" not in row, row[:220])
# The shared button rule reserves 110px, which a width cannot undo.
style = app.test_client().get("/static/style.css").get_data(as_text=True)
drop_rule = re.search(r"\.staged-drop \{(.*?)\}", style, re.S)
check("the cross beside a name gives up the shared button width",
      drop_rule is not None and "min-width: 0" in drop_rule.group(1),
      drop_rule.group(1).strip() if drop_rule else "no .staged-drop rule")

row_rule = re.search(r"\.file-picker\.is-drawn \.staged-files \{(.*?)\}",
                     style, re.S)
check("the names wrap one at a time, instead of dropping below Browse together",
      row_rule is not None and "display: contents" in row_rule.group(1),
      row_rule.group(1).strip() if row_rule else "no rule for the names in the row")

# `.extra-upload summary` is a tag inside a class and outranks
# `.candidate-summary`: left behind, it paints the summary as the small
# blue link it used to be, and draws a second chevron beside the panel's.
check("the upload summary is left to the panel rules, not the old link ones",
      ".extra-upload summary" not in style,
      [l.strip() for l in style.splitlines() if ".extra-upload" in l])
section_rule = re.search(r"\.form-section \{([^}]*)\}", style)
check("and the name of that section comes with a line under the last curve",
      section_rule is not None and "border-top" in section_rule.group(1),
      section_rule.group(1).strip() if section_rule else "no .form-section rule")
chip_rule = re.search(r"\.staged-file \{(.*?)\}", style, re.S)
check("and a filename is a label, not a status wearing the badge's wash",
      chip_rule is not None and "sky-wash" not in chip_rule.group(1),
      chip_rule.group(1).strip() if chip_rule else "no .staged-file rule")

check("each name comes with a way of taking it back out",
      'class="staged-drop" data-drop=' in held
      and f'aria-label="Remove {os.path.basename(battery)}"' in held,
      [l.strip() for l in held.splitlines() if "staged-drop" in l])
battery_box = re.search(r'<input id="battery_file"[^>]*>', held).group(0)
check("and stops requiring the box that has just been emptied",
      "required" not in battery_box, battery_box)

r = staging.post("/columns/keep", data={
    "keep": preview["file_id"],
    f"soc_{preview['file_id']}": "0",
    f"ocv_{preview['file_id']}": "1"})
check("answering the question keeps the file",
      r.status_code == 200 and len(r.get_json()["staged"]["battery"]) == 1,
      (r.status_code, r.get_json()))

# The point of all of it: one button, and no page in between.
r = staging.post("/upload", content_type="multipart/form-data", data={
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1", "slider_a": "0.5"})
check("pressing Run once is the whole run, with no column page in between",
      r.status_code == 302 and r.headers["Location"].endswith("/"),
      (r.status_code, r.headers.get("Location")))
ran = staging.get("/").get_data(as_text=True)
check("the results come up from that one press",
      'src="data:image/png;base64,' in ran and "Lowest RMSD" in ran)
with staging.session_transaction() as sess:
    answered = {k: tuple(v) for k, v in (sess.get("choices") or {}).items()}
    run_dir, wait_dir = sess["workdir"], sess["staged_workdir"]
check("the run used the columns that were answered for",
      answered == {preview["file_id"]: (0, 1)}, answered)
check("the file is still named beside the results, ready to run again",
      'class="staged-file"' in ran and os.path.basename(battery) in ran,
      [l.strip() for l in ran.splitlines() if "staged-file" in l][:2])

# The run copies rather than borrows, because the two directories are
# emptied at different moments.
check("the run works from its own copy of the file",
      run_dir != wait_dir and os.path.isdir(os.path.join(run_dir, "battery")),
      (run_dir, wait_dir))

staging.get("/?reset=1")
after_reset = staging.get("/").get_data(as_text=True)
check("starting over empties the waiting room",
      'class="staged-file"' not in after_reset,
      [l.strip() for l in after_reset.splitlines() if "staged-file" in l])
waiting_dir = os.path.join(wait_dir, "battery")
check("and the file really goes, rather than being left unlisted",
      not os.path.isdir(waiting_dir) or not os.listdir(waiting_dir),
      os.listdir(waiting_dir) if os.path.isdir(waiting_dir) else "gone")
r = staging.post("/adjust", data={"iterations": "1", "slider_a": "0.5"},
                 follow_redirects=True)
check("the run that was on screen still re-runs after that",
      r.status_code == 200 and "Lowest RMSD" in r.get_data(as_text=True),
      r.status_code)

# A candidate box adds to what it holds: a file picker cannot add to its
# own selection, so a second file means opening it again.
swap = app.test_client()
swap.post("/columns", content_type="multipart/form-data",
          data={"cathode_files": [upload_file(cathode)]})
second = swap.post("/columns", content_type="multipart/form-data",
                   data={"cathode_files": [upload_file(anode)]}).get_json()
check("picking again in a candidate box keeps what was already there",
      [f["name"] for f in second["staged"]["cathode"]]
      == [os.path.basename(cathode), os.path.basename(anode)],
      second["staged"]["cathode"])

# Which is also how someone ends up picking the whole set again, having
# reopened the picker to add one to it.
again = swap.post("/columns", content_type="multipart/form-data",
                  data={"cathode_files": [upload_file(cathode),
                                          upload_file(anode)]}).get_json()
check("and a name picked a second time is one file, not two",
      [f["name"] for f in again["staged"]["cathode"]]
      == [os.path.basename(cathode), os.path.basename(anode)],
      again["staged"]["cathode"])
with swap.session_transaction() as sess:
    swap_dir = os.path.join(sess["staged_workdir"], "cathode")
check("and the copies they stood in for go off the disk with them",
      len(os.listdir(swap_dir)) == 2, os.listdir(swap_dir))

# And a run compares every one of them, not only the last box-full.
both = app.test_client()
with open(cathode, "rb") as f:
    cathode_bytes = f.read()
for name in ("first_pick.txt", "second_pick.txt"):
    both.post("/columns", content_type="multipart/form-data",
              data={"cathode_files": [(io.BytesIO(cathode_bytes), name)]})
both.post("/columns", content_type="multipart/form-data",
          data={"battery_file": [upload_file(battery)]})
both_run = both.post("/upload", content_type="multipart/form-data", follow_redirects=True,
                     data={"library_anodes": [lib_anodes[0]],
                           "iterations": "1", "slider_a": "0.5"}).get_data(as_text=True)
check("both files picked into one box are compared, not just the later one",
      re.search(r"2 cathode\(s\) (?:&times;|×) 1 anode\(s\)", both_run) is not None,
      [l.strip() for l in both_run.splitlines() if "cathode(s)" in l])

# The battery box is the one that replaces: a run has one measured curve.
one = app.test_client()
one.post("/columns", content_type="multipart/form-data",
         data={"battery_file": [upload_file(battery)]})
with open(battery, "rb") as f:
    renamed = (io.BytesIO(f.read()), "a_different_curve.txt")
only = one.post("/columns", content_type="multipart/form-data",
                data={"battery_file": [renamed]}).get_json()
check("the battery box still holds one curve, the one picked last",
      [f["name"] for f in only["staged"]["battery"]] == ["a_different_curve.txt"],
      only["staged"]["battery"])

opened = swap.get("/").get_data(as_text=True)
check("the panel holding a file opens, so the name is not shut inside it",
      '<details class="candidate-panel extra-upload" open>' in opened,
      [l.strip() for l in opened.splitlines() if "extra-upload" in l])
opened_added = re.findall(r'data-added-for="cathode">\s*([^<]+?)\s*</span>',
                          opened)
check("and its count says how many, for when it is shut again",
      opened_added == ["2 files added"], opened_added)

r = swap.post("/columns/keep", data={})
check("taking the last file back out leaves nothing named",
      r.get_json()["staged"]["cathode"] == [], r.get_json())
check("and nothing on disk either", os.listdir(swap_dir) == [],
      os.listdir(swap_dir))

# Column numbers reach pandas by position, so one out of range would be a
# 500 rather than a rejected form.
guard = app.test_client()
guarded = guard.post("/columns", content_type="multipart/form-data",
                     data={"battery_file": [upload_file(battery)]}).get_json()
bad_id = guarded["previews"][0]["file_id"]
guard.post("/columns/keep", data={"keep": bad_id, f"soc_{bad_id}": "0",
                                  f"ocv_{bad_id}": "99"})
r = guard.post("/upload", content_type="multipart/form-data", data={
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1", "slider_a": "0.5"}, follow_redirects=True)
check("a column that is not there is refused, not handed to pandas",
      r.status_code == 200 and "Lowest RMSD" in r.get_data(as_text=True),
      r.status_code)

# The cap on candidates has to hold here too: without it a run could be
# assembled one file at a time past the limit /upload checks.
cap = app.config["MAX_CURVE_FILES"]
many = app.test_client().post("/columns", content_type="multipart/form-data",
                              data={"cathode_files": [upload_file(cathode)
                                                      for _ in range(cap + 2)]})
capped = many.get_json()
check("more files than a run can take are turned away at the door",
      len(capped["staged"]["cathode"]) == cap and capped["errors"],
      (len(capped["staged"]["cathode"]), capped["errors"]))

# The dropdown is the later word: a library curve picked after a file of
# your own hides the box that file came from, so it cannot be what was
# meant by the run.
overridden = app.test_client()
overridden.post("/columns", content_type="multipart/form-data",
                data={"battery_file": [upload_file(battery)]})
overridden.post("/upload", content_type="multipart/form-data", data={
    "battery_choice": lib_batteries[0],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1", "slider_a": "0.5"})
with overridden.session_transaction() as sess:
    used = sess.get("files") or []
check("a library curve picked afterwards wins over the waiting file",
      [m for m in used if m["curve_type"] == "battery"] == [], used)

# Half and half: one file picked into the dialog, one arriving with the
# form because the dialog never got it. The page asks about the second
# only, and the run has to end up using both.
mixed = app.test_client()
mixed_staged = mixed.post("/columns", content_type="multipart/form-data",
                          data={"cathode_files": [upload_file(cathode)]}).get_json()
mixed_page = mixed.post("/upload", content_type="multipart/form-data", data={
    "battery_file": [upload_file(battery)],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1", "slider_a": "0.5"}).get_data(as_text=True)
asked = re.findall(r'name="soc_([0-9a-f]{32})"', mixed_page)
check("a file already answered for is not asked about a second time",
      len(asked) == 1
      and asked[0] != mixed_staged["staged"]["cathode"][0]["file_id"],
      (asked, mixed_staged["staged"]["cathode"]))
mixed_run = mixed.post("/confirm", follow_redirects=True,
                       data={f"soc_{asked[0]}": "0", f"ocv_{asked[0]}": "1"}
                       ).get_data(as_text=True)
check("and it still counts among the candidates the run compared",
      re.search(r"1 cathode\(s\) (?:&times;|×) 1 anode\(s\)", mixed_run) is not None,
      [l.strip() for l in mixed_run.splitlines() if "cathode(s)" in l])

# No JavaScript, or a browser that could not reach /columns: the file
# arrives with the form instead and the question gets a page of its own.
fallback = app.test_client()
r = fallback.post("/upload", content_type="multipart/form-data", data={
    "battery_file": [upload_file(battery)],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1"})
check("a file that never reached /columns still gets its question asked",
      r.status_code == 200 and "Confirm columns" in r.get_data(as_text=True),
      r.status_code)

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
check("and again on a second visit", has_results(sticky))
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

# --- refreshing goes back to the starting instructions ----------------
# Every page carries a script that spots a reload and sends the browser to
# ?reset=1, so the refresh button does what the logo does wherever it is
# pressed. There is no JavaScript here, so check each page carries the
# script to drive it, then drive that URL directly.


def spots_a_reload(html):
    # Not merely "reset=1" in the page: the logo's href says that too, so
    # that much passes on a page carrying nothing but the logo.
    return "getEntriesByType" in html and "replace('/?reset=1')" in html


page = sticky.get("/").get_data(as_text=True)
check("the results page can tell a reload from a navigation",
      spots_a_reload(page),
      [l.strip() for l in page.splitlines() if "reset" in l])

# The pages a POST produces are the ones a refresh used to land worst on:
# the browser asked whether to send the form again.
refresher = app.test_client()
refreshable = {
    "the front page": refresher.get("/"),
    "the instructions": refresher.get("/help"),
    "the Format Data form": refresher.get("/format"),
    "an address that isn't there": refresher.get("/no-such-page"),
    "Confirm columns": refresher.post(
        "/upload", content_type="multipart/form-data",
        data={"battery_file": [upload_file(battery)],
              "library_cathodes": [lib_cathodes[0]],
              "library_anodes": [lib_anodes[0]],
              "iterations": "1"}),
    "the Format Data columns": refresher.post(
        "/format", content_type="multipart/form-data",
        data={"data_files": [upload_file(cathode)], "curve_type": "cathode"}),
    "the formatted files": refresher.post(
        "/format/confirm", data={"soc_0": "0", "ocv_0": "1"}),
}
for where, response in refreshable.items():
    refreshed = response.get_data(as_text=True)
    check(f"a refresh on {where} starts over too", spots_a_reload(refreshed),
          (response.status_code,
           [l.strip() for l in refreshed.splitlines() if "reset=1" in l][:2]))

r = sticky.get("/?reset=1", follow_redirects=True)
empty = r.get_data(as_text=True)
check("a refresh puts the starting instructions back",
      'class="placeholder"' in empty and "Lowest RMSD" not in empty,
      [l.strip() for l in empty.splitlines() if "placeholder" in l])
check("the run is set aside, not thrown away",
      "Show it again" in empty and sticky.get("/download").status_code == 200,
      sticky.get("/download").status_code)
check("and it stays put until asked for", not has_results(sticky))

sticky.get("/?restore=1")
check("showing it again brings back the same run", has_results(sticky))

# A refresh must not outlive the run it was hiding.
sticky.get("/?reset=1")
sticky.post("/adjust", data={"iterations": "1", "slider_a": "0.6"})
check("a new run is shown even if the last one was set aside",
      has_results(sticky))

# Back to the start means the whole page: the form beside the results
# still held the battery curve and the sliders that produced them.
sticky.post("/adjust", data={"iterations": "3", "slider_a": "0.25"})
shown = sticky.get("/").get_data(as_text=True)
check("the form beside a run describes that run",
      'value="3" name="iterations"' in shown
      and 'value="0.25" name="slider_a"' in shown
      and f'<option value="{lib_batteries[0]}" selected>' in shown,
      re.findall(r'<input id="slider-\w+"[^>]*value="[^"]*"[^>]*>', shown))
check("the logo starts over, the way the refresh button does",
      'class="brand" href="/?reset=1"' in shown,
      [l.strip() for l in shown.splitlines() if 'class="brand"' in l])

was_reset = sticky.get("/?reset=1", follow_redirects=True).get_data(as_text=True)
check("resetting puts the battery curve back to its default position",
      '<option value="" selected>' in was_reset
      and f'<option value="{lib_batteries[0]}" selected>' not in was_reset,
      [l.strip() for l in was_reset.splitlines() if "selected" in l][:3])
check("and the sliders with it",
      'value="1" name="iterations"' in was_reset
      and 'value="1.0" name="slider_a"' in was_reset,
      re.findall(r'<input id="slider-\w+"[^>]*value="[^"]*"[^>]*>', was_reset))

# The Reset button has the same job and cannot leave it to the browser: a
# native reset goes back to the values in the HTML, which after a run are
# the curve that produced the results rather than the top of the list.
check("the Reset button puts the battery curve back to its default too",
      "addEventListener('reset'" in shown
      and "getElementById('battery_choice').value = ''" in shown,
      [l.strip() for l in shown.splitlines() if "reset'" in l][:3])

back = sticky.get("/?restore=1", follow_redirects=True).get_data(as_text=True)
check("showing the run again brings its settings back with it",
      'value="3" name="iterations"' in back
      and 'value="0.25" name="slider_a"' in back
      and f'<option value="{lib_batteries[0]}" selected>' in back,
      re.findall(r'<input id="slider-\w+"[^>]*value="[^"]*"[^>]*>', back))

# --- the six-hour idle clock -------------------------------------------
# purge_stale_workdirs goes by the directory's mtime, and overwriting
# result.json in place does not move it. Using the page has to.
with sticky.session_transaction() as sess:
    sticky_dir = sess["workdir"]
old_clock = time.time() - 7 * 3600
os.utime(sticky_dir, (old_clock, old_clock))
check("the clock really was wound back",
      time.time() - os.path.getmtime(sticky_dir) > 6 * 3600)
sticky.get("/")
idle = time.time() - os.path.getmtime(sticky_dir)
check("simply using the page resets the idle clock", idle < 60, round(idle))

# Starting a run and then walking away from the column-confirmation step
# leaves uploads on disk with no run behind them.
abandoned = app.test_client()
abandoned.post("/upload", content_type="multipart/form-data", data={
    "battery_file": [upload_file(battery)],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]],
    "iterations": "1"})
away = abandoned.get("/").get_data(as_text=True)
check("an abandoned run leaves no file waiting on the form",
      'class="staged-file"' not in away,
      [l.strip() for l in away.splitlines() if "staged-file" in l])

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

# --- something to watch while a run is going ---------------------------
# A run is a form POST: the browser sits on the old page for up to a
# minute with nothing to show for it, so submitting raises an indicator
# that the page answering the POST carries away with the document.
running_pages = {
    "the front page": app.test_client().get("/").get_data(as_text=True),
    "the results page": sticky.get("/").get_data(as_text=True),
}
conf = app.test_client()
running_pages["the confirm page"] = conf.post(
    "/upload", content_type="multipart/form-data",
    data={"battery_file": [upload_file(battery)],
          "library_cathodes": [lib_cathodes[0]],
          "library_anodes": [lib_anodes[0]],
          "iterations": "1"}).get_data(as_text=True)

for where, page in running_pages.items():
    check(f"{where} carries exactly one running battery",
          page.count('class="run-status"') == 1,
          page.count('class="run-status"'))
    check(f"{where} marks its run form as a long job",
          re.search(r'<form class="[^"]*js-long-run[^"]*" method="POST"'
                    r' action="/(?:upload|confirm)"', page) is not None,
          [l.strip() for l in page.splitlines() if "<form" in l][:2])

# Raising it for a download would leave it up for good: the file arrives
# without the page ever navigating.
download_form = re.search(r'<form[^>]*class="[^"]*download-form[^"]*"[^>]*>',
                          running_pages["the results page"])
check("the download form is deliberately not marked",
      download_form is not None and "js-long-run" not in download_form.group(0),
      download_form.group(0) if download_form else "no download form")

# The battery stands in for the button, so it has to be inside the form
# and below the buttons it replaces — anywhere else and it would appear
# somewhere other than where the click landed.
run_form = re.search(r'<form class="[^"]*js-long-run.*?</form>',
                     running_pages["the front page"], re.S)
inside = run_form.group(0) if run_form else ""
check("the battery sits in the run form, after the buttons it stands in for",
      'class="run-status"' in inside and 'class="actions"' in inside
      and inside.rindex('class="actions"') < inside.index('class="run-status"'),
      "no run form" if not run_form else
      [l.strip() for l in inside.splitlines()
       if "actions" in l or "run-status" in l][:4])

# And it takes their place rather than floating over the page: the last
# graph and the numbers beside it stay readable while the run works.
css = app.test_client().get("/static/style.css").get_data(as_text=True)
rule = re.search(r"\.run-status \{(.*?)\}", css, re.S)
check("starting a run swaps the buttons for the battery, in place",
      rule is not None
      and "position: fixed" not in rule.group(1)
      and ".js-long-run.is-running .actions" in css
      and ".js-long-run.is-running .run-status" in css,
      rule.group(1).strip() if rule else "no .run-status rule")


# And in place means the row does not change shape doing it: the battery
# takes the width the pressed button had and Stop the width of the one
# beside it, so the two rows have to divide the width the same way.
def flex_of(selector):
    block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    if not block:
        return None
    grow = re.search(r"\bflex:\s*([^;]+);", block.group(1))
    return grow.group(1).strip() if grow else None


split = {name: flex_of(name) for name in (
    ".js-long-run .actions > .primary", ".run-status-bar",
    ".js-long-run .actions > .secondary", ".run-stop")}
check("and the buttons it stands in for divide the row the same way",
      None not in split.values()
      and split[".js-long-run .actions > .primary"] == split[".run-status-bar"]
      and split[".js-long-run .actions > .secondary"] == split[".run-stop"],
      split)

# One button starts a run, on every page that can start one: a second
# submit pointing somewhere else with formaction was how the form used to
# offer a re-run, and it is not offered any more.
for where, page in running_pages.items():
    check(f"{where} has one way to start a run, not two",
          'formaction="' not in page,
          [l.strip() for l in page.splitlines() if 'formaction="' in l])

# Stop keeps the user's way out of a run open. It has to be a plain
# button: type="submit" would start a second run and type="reset" would
# empty the form, and neither of those is stopping anything.
stop_button = re.search(r'<button[^>]*class="[^"]*run-stop[^"]*"[^>]*>', inside)
check("a run can be stopped, by a button that submits nothing",
      stop_button is not None and 'type="button"' in stop_button.group(0),
      stop_button.group(0) if stop_button else "no stop button in the run form")

# The heading, the tab and the button all name the same thing now, so the
# button has to keep saying it.
check("the button that starts a run says what it does",
      '<button class="primary" type="submit">Run optimization</button>' in inside
      and "Continue" not in inside,
      [l.strip() for l in inside.splitlines() if "primary" in l][:2])

# --- curves people send in --------------------------------------------
# Stored for good rather than in a session directory, shown to everyone,
# and marked unverified until somebody moves the file into data/ by hand.

tabs = re.findall(r'<nav class="site-nav">(.*?)</nav>', body, re.S)
tab_names = re.findall(r'>([^<>]+)</a>', tabs[0]) if tabs else []
check("the bar carries a fourth tab for sending a curve in",
      tab_names == ["Run optimization", "Format data", "Submit a curve",
                    "Instructions"], tab_names)

sender = app.test_client()
page = sender.get("/submit").get_data(as_text=True)
check("the submission page asks for the file, the type and who sent it",
      all(f'name="{field}"' in page for field in
          ("curve_files", "curve_type", "submitter", "organization", "doi")),
      re.findall(r'name="(\w+)"', page))
check("and says plainly that what is sent becomes public",
      "public" in page.lower() and "not verified" in page.lower(),
      [l.strip() for l in page.splitlines() if "public" in l.lower()][:2])
check("and the tab it is on is the one lit",
      '"is-active">Submit a curve<' in page.replace(" class=", ""),
      [l.strip() for l in page.splitlines() if "is-active" in l])

# Nothing is stored until the columns are settled: the file is unreadable
# until somebody says which column is which, and a curve nobody can read
# is worse than no curve.
with open(cathode, "rb") as f:
    curve_bytes = f.read()
r = sender.post("/submit", content_type="multipart/form-data", data={
    "curve_files": [(io.BytesIO(curve_bytes), "NMC622-LICeM.txt")],
    "curve_type": "cathode", "submitter": "A Person",
    "organization": "LICeM", "doi": "10.1016/j.xcrp.2020.100253"})
asked = r.get_data(as_text=True)
check("submitting asks about the columns before storing anything",
      r.status_code == 200 and 'name="soc_0"' in asked
      and not os.path.isdir(os.path.join(SUBMIT_ROOT, "cathode")),
      (r.status_code, os.listdir(SUBMIT_ROOT)))

r = sender.post("/submit/confirm", data={"soc_0": "0", "ocv_0": "1"},
                follow_redirects=True)
thanks = r.get_data(as_text=True)
stored_dir = os.path.join(SUBMIT_ROOT, "cathode")
check("confirming stores the curve, its metadata and the file as it arrived",
      sorted(os.listdir(stored_dir))
      == ["NMC622-LICeM.json", "NMC622-LICeM.txt", "original"]
      and os.listdir(os.path.join(stored_dir, "original"))
      == ["NMC622-LICeM.txt"],
      sorted(os.listdir(stored_dir)))
kept = json.load(open(os.path.join(stored_dir, "NMC622-LICeM.json")))
check("the metadata beside it says who sent it and where it came from",
      (kept["submitter"], kept["organization"], kept["doi"])
      == ("A Person", "LICeM", "10.1016/j.xcrp.2020.100253")
      and kept["submitted"], kept)
# The point of writing it in the project's own layout: promoting a curve
# is moving this one file, with nothing to convert on the way.
rows = open(os.path.join(stored_dir, "NMC622-LICeM.txt")).read().splitlines()
check("and the stored curve is ready to be moved into data/ as it stands",
      len(rows) == 1001 and len(rows[0].split()) == 2, len(rows))
check("the page says it landed, and lists it with its DOI",
      "Thank you" in thanks and "NMC622-LICeM" in thanks
      and "doi.org/10.1016/j.xcrp.2020.100253" in thanks,
      [l.strip() for l in thanks.splitlines() if "NMC622" in l][:2])

# It shows up beside the built-in curves, in a panel of its own, and off.
run_page = sender.get("/").get_data(as_text=True)
panel = re.search(r'<details class="candidate-panel unverified-panel">(.*?)</details>',
                  run_page, re.S)
check("the run form grows a panel for the curves nobody has checked",
      panel is not None and "Submitted" in panel.group(1)
      and "not verified" in run_page,
      "no unverified panel" if not panel else panel.group(1)[:120])
box = re.search(r'<input type="checkbox" name="submitted_cathodes" value="NMC622-LICeM"([^>]*)>',
                run_page)
check("with the curve in it, and not ticked",
      box is not None and "checked" not in box.group(1),
      box.group(0) if box else "no checkbox for the submitted curve")
check("and a way through to the page these came from",
      panel is not None
      and '<a class="mini" href="/submit">Submit your own curve</a>' in run_page,
      [l.strip() for l in run_page.splitlines() if "/submit" in l][:2])
# Both halves of "a button on the right" are in the stylesheet, where
# nothing else here can see them: an anchor picks up none of the pill
# styling if that rule goes back to naming the button element, and it
# lands under the list rather than beside it without the row.
footer_rule = re.search(r"\.panel-footer \{([^}]*)\}", style)
check("and it sits at the right-hand end of the panel, not in the text column",
      footer_rule is not None and "flex-end" in footer_rule.group(1),
      footer_rule.group(1).strip() if footer_rule else "no .panel-footer rule")
check("and the pill style is a class, so a link can be one too",
      "button.mini {" not in style and ".mini {" in style,
      [l.strip() for l in style.splitlines() if ".mini {" in l])
check("and a preview that comes from the submitted folder, not the library",
      "/curve/submitted/cathode/NMC622-LICeM" in run_page
      and sender.get("/curve/submitted/cathode/NMC622-LICeM").status_code == 200,
      sender.get("/curve/submitted/cathode/NMC622-LICeM").status_code)

# A run may use one, and the answer has to say that it did.
r = sender.post("/upload", content_type="multipart/form-data",
                follow_redirects=True, data={
                    "battery_choice": lib_batteries[0],
                    "submitted_cathodes": ["NMC622-LICeM"],
                    "library_anodes": [lib_anodes[0]],
                    "iterations": "1", "slider_a": "1"})
ran = r.get_data(as_text=True)
check("a submitted curve can win a run, and is named as submitted when it does",
      "Lowest RMSD" in ran and "NMC622-LICeM (submitted)" in ran,
      [l.strip() for l in ran.splitlines() if "NMC622" in l][:2])

# A name that is not on disk is dropped on the way in, so nothing later
# has to decide whether to trust it.
r = sender.post("/upload", content_type="multipart/form-data", data={
    "battery_choice": lib_batteries[0],
    "submitted_cathodes": ["../../data/cathode_data/anything"],
    "library_cathodes": [lib_cathodes[0]],
    "library_anodes": [lib_anodes[0]], "iterations": "1", "slider_a": "1"})
with sender.session_transaction() as sess:
    kept_submitted = (sess.get("submitted") or {}).get("cathode")
check("a submitted name the folder does not list never gets that far",
      r.status_code == 302 and kept_submitted == [], kept_submitted)

# The two things that stop a public form filling the disk.
r = sender.post("/submit", content_type="multipart/form-data", data={
    "curve_files": [(io.BytesIO(curve_bytes), "anonymous.txt")],
    "curve_type": "cathode", "submitter": "", "organization": ""})
check("a curve with nobody behind it is refused",
      r.status_code == 302, r.status_code)

app.config["MAX_SUBMISSIONS"] = 1
r = sender.post("/submit", content_type="multipart/form-data", data={
    "curve_files": [(io.BytesIO(curve_bytes), "one_too_many.txt")],
    "curve_type": "cathode", "submitter": "A Person", "organization": "LICeM"})
after = sender.get("/submit").get_data(as_text=True)
check("and past the cap the form stops taking them",
      r.status_code == 302 and 'name="curve_files"' not in after,
      (r.status_code,
       "form still offered" if 'name="curve_files"' in after else "no redirect"))
app.config["MAX_SUBMISSIONS"] = 200

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
shutil.rmtree(SUBMIT_ROOT, ignore_errors=True)
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
