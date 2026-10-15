"""PT-Helper web service: a searchable reference for configuration review,
vulnerability analysis and penetration-test methodology.

Run:  python app.py            (serves on http://127.0.0.1:5055)
Env:  PT_HELP_HOST, PT_HELP_PORT, PT_HELP_DEBUG=1
"""
import json
import os
import re
from pathlib import Path

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
CONTENT_FILE = BASE_DIR / "content" / "entries.json"

app = Flask(__name__)

# Phases follow the order an assessment normally moves through.
PHASES = [
    {"id": "recon", "name": "Reconnaissance"},
    {"id": "enumeration", "name": "Enumeration"},
    {"id": "vuln-analysis", "name": "Vulnerability analysis"},
    {"id": "exploitation", "name": "Exploitation"},
    {"id": "post-exploitation", "name": "Post-exploitation"},
    {"id": "config-review", "name": "Configuration review"},
]
PHASE_NAMES = {p["id"]: p["name"] for p in PHASES}
PHASE_ORDER = {p["id"]: i for i, p in enumerate(PHASES)}

# Where a match counts most. Title and tag hits should outrank a stray word in the notes.
FIELD_WEIGHTS = {
    "title": 6,
    "tags": 5,
    "phase": 3,
    "summary": 2,
    "commands": 2,
    "notes": 1,
}

_cache = {"mtime": None, "entries": []}


def tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def _index(entry):
    commands = " ".join(f"{c.get('label', '')} {c['cmd']}" for c in entry.get("commands", []))
    fields = {
        "title": entry["title"],
        "tags": " ".join(entry.get("tags", [])),
        "phase": PHASE_NAMES.get(entry["phase"], entry["phase"]),
        "summary": entry.get("summary", ""),
        "commands": commands,
        "notes": f"{entry.get('notes', '')} {entry.get('caution', '')}",
    }
    return {name: set(tokenize(value)) for name, value in fields.items()}


def load_entries():
    """Load entries.json, reloading when the file changes so edits show up without a restart."""
    mtime = CONTENT_FILE.stat().st_mtime
    if _cache["mtime"] != mtime:
        with CONTENT_FILE.open(encoding="utf-8") as fh:
            entries = json.load(fh)
        for entry in entries:
            entry["_tokens"] = _index(entry)
            entry["phase_name"] = PHASE_NAMES.get(entry["phase"], entry["phase"])
        _cache.update(mtime=mtime, entries=entries)
    return _cache["entries"]


def score(entry, query_tokens):
    """Every query word has to match somewhere (exact or prefix); the score ranks the hits."""
    total = 0
    for token in query_tokens:
        word_score = 0
        for field, weight in FIELD_WEIGHTS.items():
            words = entry["_tokens"][field]
            if token in words:
                word_score += weight * 2
            elif any(w.startswith(token) for w in words):
                word_score += weight
        if word_score == 0:
            return 0
        total += word_score
    return total


def public(entry):
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def search(query, phase=None):
    entries = load_entries()
    tokens = tokenize(query)

    if tokens:
        scored = [(score(e, tokens), e) for e in entries]
        matches = [(s, e) for s, e in scored if s > 0]
    else:
        matches = [(0, e) for e in entries]

    # Phase counts reflect the query but ignore the phase filter,
    # so the sidebar shows where the matches are.
    counts = {p["id"]: 0 for p in PHASES}
    for _, e in matches:
        counts[e["phase"]] = counts.get(e["phase"], 0) + 1
    phases = [{**p, "count": counts[p["id"]]} for p in PHASES]

    if phase in PHASE_NAMES:
        matches = [(s, e) for s, e in matches if e["phase"] == phase]

    matches.sort(key=lambda m: (-m[0], PHASE_ORDER.get(m[1]["phase"], 99), m[1]["title"]))
    return {
        "query": query,
        "phase": phase if phase in PHASE_NAMES else "",
        "count": len(matches),
        "total": len(entries),
        "results": [public(e) for _, e in matches],
        "phases": phases,
    }


@app.route("/")
def index():
    q = request.args.get("q", "").strip()[:200]
    phase = request.args.get("phase", "")
    return render_template("index.html", boot=search(q, phase))


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()[:200]
    return jsonify(search(q, request.args.get("phase", "")))


@app.route("/healthz")
def healthz():
    return jsonify(status="ok", entries=len(load_entries()))


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:"
    )
    return resp


if __name__ == "__main__":
    # Loopback by default: this tool holds attack commands, so it should not
    # be reachable from the network unless you deliberately change PT_HELP_HOST.
    app.run(
        host=os.environ.get("PT_HELP_HOST", "127.0.0.1"),
        port=int(os.environ.get("PT_HELP_PORT", "5055")),
        debug=os.environ.get("PT_HELP_DEBUG") == "1",
    )
