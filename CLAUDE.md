# PyBEP

Battery OCV decomposition: given a measured full-cell OCV curve, find the
cathode/anode pair — and the four parameters — whose combination best
reconstructs it. From LICeM, University of Ljubljana.

Three front ends over one calculation core:

- `pybep/core/` — the maths and the file parsing. Shared; nothing in here knows
  which front end is calling.
- `pybep/gui_app/` — tkinter desktop app (`python -m pybep.gui_app`).
- `pybep/web/` — Flask site (`python -m pybep.web`, serves `localhost:5000`).
  Most current work is here.

`src/` holds two empty directories left over from the pre-package layout.
Nothing imports them.

## Running things

The virtualenv is not on PATH, and bare `python` has no Flask. Name it:

```
./.venv/Scripts/python.exe -m pybep.web        # dev server on :5000
./.venv/Scripts/python.exe tests/run_all.py    # all three suites
./.venv/Scripts/python.exe tests/test_web.py   # just one
```

Tests are plain scripts — no pytest. `test_web.py` and `test_gui.py` use a
`check(name, cond, detail)` helper, print `PASS`/`FAIL` per check and exit
non-zero if any failed; `test_pipeline.py` reports `N passed, M failed`.

There is no browser and no `node` in this environment. Whether a page *looks*
right, or whether client-side JS actually *runs*, cannot be checked here. Say
so plainly rather than implying otherwise.

## Git

The remote names are backwards from the usual:

- `fork` → `github.com/aj200288/PyBEP` — ours. **Push here.**
- `origin` → `github.com/JonPisek/PyBEP` — Jon's upstream. **Never push.**

Work happens on `unified-structure`, branched from `main`. Commit after every
prompt without being asked.

## Things that bite

**`SECRET_KEY` must never get a hardcoded fallback.** The session cookie names
a temp directory the server both reads files from and deletes, so a key
readable in a public repository hands an attacker both. `pybep/web/__init__.py`
generates a random per-process key and warns to stderr instead.

**`create_session_workdir()` recursively deletes what it is handed.**
`is_session_workdir()` is the guard — only a directory sitting directly in the
temp root whose name starts with `pybep_`. Never route around it, and never
hand a real path to either one in a test.

**The optimization has no timeout and no queue.** Work grows as cathodes ×
anodes × iterations. The caps in `pybep/web/__init__.py` — `MAX_ITERATIONS` 5,
`MAX_CURVE_FILES` 12, `MAX_UPLOAD_BYTES` 32 MB, `DEFAULT_N_JOBS` 2 — are the
only thing stopping one request from occupying the server indefinitely. Raise
them only alongside a real job queue.

**Screenshots go in `debug_images/`** (gitignored). When a UI problem is
described by picture, that is where the picture is.

## How the website hangs together

- **POST/Redirect/GET throughout.** A run POSTs, saves what it produced, then
  redirects to `/`. That redirect is what lets results survive a refresh, the
  back button, and a trip to another tab.
- **Results live on disk, not in the cookie** — `result_view.json` beside
  `result.json` in the session workdir, since the base64 graph alone is ~40 kB.
  See `pipeline.save_result_view` / `load_result_view`. JSON has no tuples, so
  `best_parameters` is converted back to one on the way in.
- **Two columns.** `_run_form.html` is the left-hand column of every page in the
  run flow; the right-hand one is either the results or the placeholder in
  `index.html`. `.container` is a wrapping flex row — the `min-width` on
  `.graph-results` is what makes it wrap rather than crush. Inside the run
  form, `_settings.html` is ruled off under a "Fitting" heading: the card
  asks which curves to compare, then how to fit them, and without the break
  those are six labels of one weight.
- **Refreshing means "start over", on every page.** `_reload_reset.html`,
  included from `base.html`, asks the browser how the page was loaded
  (Navigation Timing), and a reload replaces itself with `/?reset=1` — the
  same URL the logo goes to, so the two buttons do one thing. That sets
  `results_hidden`: the run is set aside, not deleted, and a link on the empty
  panel brings it back. On the pages a POST produces — Confirm columns, and
  both Format Data steps — it also replaces the browser's "send the form
  again?" dialog, and takes what is on them with it.
- **Format Data keeps its own working directory** (`format_workdir`). Sharing
  one deleted results people were still reading.
- **The six-hour idle sweep goes by directory mtime**, which does not move when
  a file inside is overwritten in place. `touch_workdir()`, called from
  `_session_workdir()`, is what keeps a long session from being swept.
- **`js-long-run` + `_running.html`.** Submitting a form with that class swaps
  its button for a charging battery, with Stop beside it. Stop calls
  `window.stop()`: it drops the browser's wait, it cannot call off the run the
  server has already started. The row the battery lands in divides its width
  the same way the buttons did — wide, then one the width of its own label —
  so nothing moves at the swap; a check compares the two `flex` values.
- **Picking a file stages it; the column question is a dialog, not a page.**
  A file input marked `js-stage-file` POSTs to `/columns` on change, which
  saves the file, answers with a preview, and stores the guess as its
  standing answer. A candidate box adds to what it holds and drops a repeat
  of the same filename; the battery box replaces, since a run has one
  measured curve. The dialog in `_run_form.html` asks over the form and
  `/columns/keep` records the reply. So "Run optimization" is the only button
  in a run. `confirm.html` and `/confirm` are the fallback for a browser with
  no `<dialog>`, no `fetch`, or a request that failed — `/upload` still
  renders that page for any raw file that arrives with the form.
- **Both ways of choosing a candidate are the same panel.** Under one group
  label sit "Built-in curves" and "Your own files" — two `.candidate-panel`
  rows, each with a count on it, so neither outweighs the other. The upload
  one was a blue link with a bordered box hanging open below it, and that box
  drew the eye harder than the list it is an alternative to. The file chips
  are neutral for the same reason: the blue wash belongs to the counts.
- **A `?` beside every field name.** `_field_help.html` writes the label, the
  dot, and — hidden next to the field — the words behind it; the dialog at the
  foot of `_run_form.html` moves a copy in on click. The dot is an `<a>` to
  the matching heading in the instructions *first*, so a browser with no
  `<dialog>` gets an answer rather than nothing; a check confirms every one of
  those anchors exists on `help.html`, since a stale one fails silently.
  Macros do not cross an `{% include %}`, so `_settings.html` imports the file
  for its three sliders as well.
- **Staged files get their own directory** (`staged_workdir`, manifest
  `staged.json`), and a run *copies* out of it. `/upload` makes a fresh run
  directory every time, and starting over empties the staged one; without the
  copy, either would pull the files out from under a result still on screen
  that `/adjust` can re-run. The manifest is a file rather than session data
  because it grows with every file picked; the cookie already carries the
  run's own list.
- **`/adjust` is unreachable from the UI on purpose.** The button that posted
  there was removed; the route stays because it is the only path that re-runs
  without re-uploading, and the tests use it.

## House style

Comments say **why**, not what: the reason a line exists, the failure it
prevents, the thing that would otherwise be re-derived. Template partials open
with a comment saying what they are and what context they expect.

User-facing copy is plain and concrete — it names what will happen, not what
the software is doing.

When adding a test, break the thing it covers and confirm it fails for the
right reason before keeping it. More than one check in `test_web.py` was
tightened only because a deliberate mis-marking still slipped past it.

Anže is not a professional developer. Explain trade-offs in plain terms, be
explicit about what was verified and what was not, and raise a real problem in
a sentence or two rather than a lecture.
