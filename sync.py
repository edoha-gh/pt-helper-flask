#!/usr/bin/env python3
"""Sync PT Helper code and data between machines over SSH.

Runs on any machine with Python 3.8+ and the OpenSSH client (Linux, macOS,
Windows 10+). The other machine needs sshd and python3, nothing else. Login is
key-based only: ssh runs in BatchMode, so it never stops to ask for a password.

  python sync.py init       write a sync.config.json to edit
  python sync.py check      test the connection and the remote folder
  python sync.py status     preview what `data` and `deploy` would change
  python sync.py data       two-way sync of the data folders (content/)
  python sync.py deploy     push the application code to the server

This same file is streamed to the remote side and run there for hashing and file
transfer, so there is nothing to install on the server and both ends always run
the same version.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path, PurePosixPath

MARKER = b"--PTSYNC-BEGIN--"
BACKUP_DIR = ".sync-backup"
STATE_FILE = ".sync-state.json"
CONFIG_FILE = "sync.config.json"
IGNORE_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", BACKUP_DIR}
IGNORE_GLOBS = ("*.pyc", "*.swp", "*.tmp", "*~", ".DS_Store", "Thumbs.db")
CHUNK = 1 << 20
UNSAFE_BASES = {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/media", "/mnt", "/opt",
                "/proc", "/root", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var", "/var/www"}

DEFAULTS = {
    "host": "",
    "port": None,
    "identity_file": "",
    "remote_path": "",
    "remote_base": "",
    "remote_group": "www-data",
    "remote_python": "python3",
    "remote_venv": "",
    "remote_staging": "",
    "data_paths": ["content"],
    "code_paths": ["app.py", "wsgi.py", "requirements.txt", "templates", "static"],
    "post_deploy": "",
    "server_files": [],
}

EXAMPLE = dict(
    DEFAULTS,
    host="pt-server",
    identity_file="~/.ssh/id_ed25519",
    remote_base="/var/www/flask",
    remote_path="/var/www/flask/app",
    remote_venv="/var/www/flask/venv",
    post_deploy="sudo systemctl restart flask",
    server_files=[
        {"local": "deploy/wsgi.ini", "remote": "/var/www/flask/wsgi.ini"},
        {"local": "deploy/flask.service", "remote": "/etc/systemd/system/flask.service",
         "sudo": True, "after": "sudo systemctl daemon-reload"},
        {"local": "deploy/nginx-flask.conf", "remote": "/etc/nginx/conf.d/flask.conf",
         "sudo": True, "after": "sudo nginx -t && sudo systemctl reload nginx"},
    ],
)


class SyncError(Exception):
    pass


# ---------------------------------------------------------------------------
# File helpers shared by both ends
# ---------------------------------------------------------------------------

def is_ignored(name):
    return any(fnmatch.fnmatch(name, g) for g in IGNORE_GLOBS)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def scan(root, rels):
    """Return {posix_relative_path: sha256} for every file under the given paths."""
    out = {}
    for rel in rels:
        base = root / rel
        if base.is_symlink():
            continue
        if base.is_file():
            if not is_ignored(base.name):
                out[PurePosixPath(rel).as_posix()] = sha256_file(base)
        elif base.is_dir():
            for dirpath, dirnames, filenames in os.walk(str(base)):
                dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
                for name in sorted(filenames):
                    full = Path(dirpath) / name
                    if is_ignored(name) or full.is_symlink():
                        continue
                    out[full.relative_to(root).as_posix()] = sha256_file(full)
    return out


def safe_rel(rel, allowed):
    """Reject anything that could write outside the synced folders."""
    p = PurePosixPath(rel)
    if (not rel or p.is_absolute() or ".." in p.parts or "\\" in rel or ":" in rel
            or any(part in IGNORE_DIRS for part in p.parts)):
        raise SyncError("refusing unsafe path: %r" % rel)
    if not any(rel == a or rel.startswith(a + "/") for a in allowed):
        raise SyncError("path is outside the synced folders: %r" % rel)
    return p


def backup_file(path, rel, backup_root):
    if backup_root is None or not path.is_file():
        return
    target = backup_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(path), str(target))


def send_files(stream, root, rels, names=None):
    """Write files as frames: a JSON header line, then exactly n raw bytes.

    `names` optionally maps a local relative path to the name used on the wire."""
    sent = {}
    for rel in rels:
        path = root / rel
        size = path.stat().st_size
        stream.write(json.dumps({"p": (names or {}).get(rel, rel), "n": size}).encode("utf-8") + b"\n")
        h = hashlib.sha256()
        left = size
        with open(path, "rb") as fh:
            while left:
                block = fh.read(min(CHUNK, left))
                if not block:
                    raise SyncError("file changed while sending: %s" % rel)
                stream.write(block)
                h.update(block)
                left -= len(block)
        sent[rel] = h.hexdigest()
    stream.write(b"{}\n")
    stream.flush()
    return sent


def receive_files(stream, root, allowed, backup_root=None):
    """Read frames written by send_files. Returns {path: sha256 of what was written}."""
    received = {}
    while True:
        line = stream.readline()
        if not line:
            raise SyncError("transfer ended early")
        head = json.loads(line.decode("utf-8"))
        if not head:
            return received
        rel, size = head["p"], int(head["n"])
        safe_rel(rel, allowed)
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".ptsync.tmp")
        h = hashlib.sha256()
        left = size
        try:
            with open(str(tmp), "wb") as fh:
                while left:
                    block = stream.read(min(CHUNK, left))
                    if not block:
                        raise SyncError("transfer ended early")
                    fh.write(block)
                    h.update(block)
                    left -= len(block)
            backup_file(dest, rel, backup_root)
            os.replace(str(tmp), str(dest))
        finally:
            if tmp.exists():
                tmp.unlink()
        received[rel] = h.hexdigest()


def remove_files(root, rels, allowed, backup_root=None):
    for rel in rels:
        safe_rel(rel, allowed)
        path = root / rel
        if not path.is_file() or path.is_symlink():
            continue
        backup_file(path, rel, backup_root)
        path.unlink()
        parent = path.parent
        while parent != root and parent.relative_to(root).as_posix() not in allowed \
                and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


# ---------------------------------------------------------------------------
# Remote side: runs on the server when this file is streamed over ssh
# ---------------------------------------------------------------------------

def _nearest_existing(path):
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _folder_state(label, path, hint_missing):
    """Report whether a folder exists / can be written, on the server."""
    path = Path(os.path.expanduser(path))
    if path.is_dir():
        if os.access(str(path), os.W_OK):
            return ("ok", label, "%s exists and is writable" % path)
        return ("warn", label, "%s exists but is not writable by this login; %s" % (path, hint_missing))
    if os.access(str(_nearest_existing(path)), os.W_OK):
        return ("ok", label, "%s is missing and will be created on the first sync" % path)
    return ("warn", label, "%s is missing and this login cannot create it; %s" % (path, hint_missing))


def remote_probe(spec):
    """Server-side checks for `test`. Returns [(status, name, detail), ...]."""
    import shutil
    results = []
    add = lambda status, name, detail: results.append((status, name, detail))

    try:
        import getpass
        user = getpass.getuser()
    except Exception:
        user = str(os.getuid())
    add("ok", "server login", "logged in as %s" % user)
    add("ok" if sys.version_info >= (3, 8) else "fail", "server python",
        "Python %s" % sys.version.split()[0])

    fix = "deploy will fix it with sudo" if spec.get("base") else \
        "set remote_base so deploy can create it with sudo, or fix the owner by hand"
    if spec.get("base"):
        results.append(_folder_state("remote_base", spec["base"], fix))
    results.append(_folder_state("remote_path", spec["path"], fix))

    group = spec.get("group")
    if group:
        try:
            import grp
            gid = grp.getgrnam(group).gr_gid
            if gid in os.getgroups():
                add("ok", "remote_group", "group %s exists and %s is a member" % (group, user))
            else:
                add("warn", "remote_group",
                    "group %s exists but %s is not a member (fine if the folders are owned by %s)"
                    % (group, user, user))
        except KeyError:
            add("fail", "remote_group", "group %s does not exist on the server" % group)
        except ImportError:
            pass

    venv = spec.get("venv")
    if venv:
        venv = os.path.expanduser(venv)
        py, pip, uwsgi = (os.path.join(venv, "bin", n) for n in ("python", "pip", "uwsgi"))
        if not os.path.isdir(venv):
            add("warn", "remote_venv", "%s is missing; deploy will create it" % venv)
        elif not (os.access(py, os.X_OK) and os.access(pip, os.X_OK)):
            add("fail", "remote_venv", "%s exists but has no working python/pip; run deploy --pip "
                "or delete the folder so deploy recreates it" % venv)
        else:
            ver = subprocess.run([py, "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT).stdout.decode().strip()
            add("ok", "remote_venv", "%s (%s)" % (venv, ver))
            for mod in ("flask",):
                bad = subprocess.call([py, "-c", "import " + mod],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                add("ok" if bad == 0 else "warn", "venv: " + mod,
                    "installed" if bad == 0 else "not installed; run deploy --pip")
            add("ok" if os.access(uwsgi, os.X_OK) else "warn", "venv: uwsgi",
                "installed" if os.access(uwsgi, os.X_OK) else "not installed; run deploy --pip")

    if spec.get("server_files"):
        results.append(_folder_state("remote_staging", spec["staging"],
                                     "deploy will create it (sudo if remote_base is set)"))
    for sf in spec.get("server_files") or []:
        dest = sf["remote"]
        parent = Path(dest).parent
        label = "server file " + sf["name"]
        if not parent.is_dir():
            add("fail", label, "folder %s does not exist on the server" % parent)
            continue
        if not sf.get("sudo"):
            if os.access(str(parent), os.W_OK):
                add("ok", label, "can be written to %s" % dest)
            else:
                add("fail", label, "%s is not writable by %s; set \"sudo\": true for this file" % (parent, user))
            continue
        install = shutil.which("install") or "/usr/bin/install"
        staged = os.path.join(os.path.expanduser(spec["staging"]), sf["name"])
        argv = [install, "-m", sf["mode"], "-o", "root", "-g", "root", staged, dest]
        if not shutil.which("sudo"):
            add("fail", label, "sudo is not installed")
        elif subprocess.call(["sudo", "-n", "-l"] + argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL) == 0:
            add("ok", label, "sudo install to %s is allowed without a password" % dest)
        else:
            add("warn", label, "deploy will ask for the sudo password. To make it passwordless "
                "add to /etc/sudoers.d/ (via visudo): %s ALL=(root) NOPASSWD: %s" % (user, " ".join(argv)))

    hook = spec.get("post_deploy") or ""
    if hook:
        import re
        import shlex as _shlex
        segments = [x.strip() for x in re.split(r"&&|\|\||;", hook) if x.strip()]
        for seg in segments:
            try:
                tokens = _shlex.split(seg)
            except ValueError:
                add("warn", "post_deploy", "could not parse: %s" % seg)
                continue
            if tokens and tokens[0] == "sudo":
                rest = [t for t in tokens[1:] if t != "-n"]
                if not shutil.which("sudo"):
                    add("fail", "post_deploy sudo", "sudo is not installed")
                    continue
                code = subprocess.call(["sudo", "-n", "-l"] + rest,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if code == 0:
                    add("ok", "post_deploy sudo", "allowed without a password: %s" % " ".join(rest))
                else:
                    add("fail", "post_deploy sudo",
                        "sudo -n is not allowed for `%s`; add a NOPASSWD line with visudo" % " ".join(rest))
        for verb, unit in re.findall(r"systemctl\s+(restart|reload|start|stop)\s+([\w@.\-]+)", hook):
            if not shutil.which("systemctl"):
                add("warn", "service", "systemctl not found on the server")
                break
            load = subprocess.run(["systemctl", "show", "-p", "LoadState", "--value", unit],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()
            if load in ("", "not-found"):
                add("fail", "service " + unit, "no such systemd unit; create /etc/systemd/system/%s.service" % unit)
                continue
            act = subprocess.run(["systemctl", "is-active", unit], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL).stdout.decode().strip()
            add("ok" if act == "active" else "warn", "service " + unit,
                "unit exists, state: %s%s" % (act or "unknown",
                                               "" if act == "active" else " (start it with systemctl)"))
    return results


def remote_main(argv):
    op, root = argv[0], Path(os.path.expanduser(argv[1]))
    rest = argv[2:]
    out = sys.stdout.buffer
    out.write(b"\n" + MARKER + b"\n")
    try:
        if op == "check":
            probe = root
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            info = {
                "python": sys.version.split()[0],
                "root": str(root),
                "exists": root.is_dir(),
                "writable": os.access(str(probe), os.W_OK),
            }
            out.write(json.dumps(info).encode("utf-8"))
        elif op == "filehash":
            res = {}
            for item in rest:
                path = Path(os.path.expanduser(item))
                res[item] = sha256_file(path) if path.is_file() else None
            out.write(json.dumps(res).encode("utf-8"))
        elif op == "stage":
            staging = Path(os.path.expanduser(rest[0]))
            staging.mkdir(parents=True, exist_ok=True)
            os.chmod(str(staging), 0o700)  # only the login user may stage files
            got = receive_files(sys.stdin.buffer, staging, rest[1:], None)
            out.write(json.dumps({"written": len(got)}).encode("utf-8"))
        elif op == "test":
            out.write(json.dumps(remote_probe(json.loads(rest[0]))).encode("utf-8"))
        elif op == "manifest":
            out.write(json.dumps(scan(root, rest)).encode("utf-8"))
        elif op == "put":
            stamp, allowed = rest[0], rest[1:]
            root.mkdir(parents=True, exist_ok=True)
            got = receive_files(sys.stdin.buffer, root, allowed, root / BACKUP_DIR / stamp)
            out.write(json.dumps({"written": len(got)}).encode("utf-8"))
        elif op == "get":
            allowed = rest
            rels = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            for rel in rels:
                safe_rel(rel, allowed)
            send_files(out, root, rels)
        elif op == "rm":
            stamp, allowed = rest[0], rest[1:]
            rels = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            remove_files(root, rels, allowed, root / BACKUP_DIR / stamp)
            out.write(json.dumps({"removed": len(rels)}).encode("utf-8"))
        else:
            raise SyncError("unknown operation: %s" % op)
        out.flush()
    except Exception as exc:  # keep the failure short; a traceback would echo the bootstrap
        sys.stderr.write("ptsync remote error: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(3)


# ---------------------------------------------------------------------------
# Local side
# ---------------------------------------------------------------------------

class Remote:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ssh_bin = os.environ.get("PTSYNC_SSH") or "ssh"

    def _ssh_argv(self, tty=False):
        argv = [self.ssh_bin, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15"]
        if self.cfg["port"]:
            argv += ["-p", str(self.cfg["port"])]
        if self.cfg["identity_file"]:
            argv += ["-i", os.path.expanduser(self.cfg["identity_file"]),
                     "-o", "IdentitiesOnly=yes"]
        if tty:
            argv.append("-t")  # lets sudo ask for a password in your terminal
        argv.append(self.cfg["host"])
        return argv

    def _command(self, op, args):
        blob = base64.b64encode(zlib.compress(Path(__file__).read_bytes(), 9)).decode("ascii")
        code = ('exec(compile(__import__("zlib").decompress(__import__("base64")'
                '.b64decode("%s")),"ptsync","exec"),{"__name__":"__ptsync_remote__"})' % blob)
        parts = [shlex.quote(self.cfg["remote_python"]), "-c", "'" + code + "'",
                 shlex.quote(op), shlex.quote(self.cfg["remote_path"])]
        parts += [shlex.quote(a) for a in args]
        return " ".join(parts)

    def _open(self, op, args):
        try:
            return subprocess.Popen(self._ssh_argv() + [self._command(op, args)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except FileNotFoundError:
            raise SyncError("could not run %r. Install the OpenSSH client first." % self.ssh_bin)

    def _skip_banner(self, proc):
        """Discard anything a login script printed before our marker line."""
        while True:
            line = proc.stdout.readline()
            if not line:
                self._finish(proc, "connecting")  # explains a failed login
                raise SyncError("no reply from the server. Run `ssh %s` by hand to see why."
                                % self.cfg["host"])
            if line.strip() == MARKER:
                return

    def _finish(self, proc, what):
        code = proc.wait()
        if code != 0:
            hint = ""
            if code == 255:
                hint = (" Run `ssh %s` by hand once to accept the host key and confirm "
                        "that key login works." % self.cfg["host"])
            raise SyncError("%s failed (exit %d); see the message above.%s" % (what, code, hint))

    def _call(self, op, args, payload=b""):
        proc = self._open(op, args)
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self._skip_banner(proc)
            data = proc.stdout.read()
        finally:
            proc.stdout.close()
        self._finish(proc, op)
        return json.loads(data.decode("utf-8")) if data.strip() else {}

    def check(self):
        return self._call("check", [])

    def filehash(self, paths):
        return self._call("filehash", paths) if paths else {}

    def stage(self, root, items):
        """Upload [(local_rel, wire_name)] into the server's staging folder."""
        if not items:
            return
        proc = self._open("stage", [self.cfg["remote_staging"]] + [name for _, name in items])
        try:
            send_files(proc.stdin, root, [rel for rel, _ in items], dict(items))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self._skip_banner(proc)
            proc.stdout.read()
        finally:
            proc.stdout.close()
        self._finish(proc, "upload of server files")

    def test(self, spec):
        return self._call("test", [json.dumps(spec)])

    def manifest(self, rels):
        return self._call("manifest", rels)

    def put(self, root, rels, stamp, allowed):
        if not rels:
            return {}
        proc = self._open("put", [stamp] + allowed)
        sent = {}
        try:
            sent = send_files(proc.stdin, root, rels)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self._skip_banner(proc)
            proc.stdout.read()
        finally:
            proc.stdout.close()
        self._finish(proc, "upload")
        return sent

    def get(self, root, rels, allowed, backup_root):
        if not rels:
            return {}
        proc = self._open("get", allowed)
        try:
            proc.stdin.write(json.dumps(rels).encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self._skip_banner(proc)
            received = receive_files(proc.stdout, root, allowed, backup_root)
        finally:
            proc.stdout.close()
        self._finish(proc, "download")
        return received

    def remove(self, rels, stamp, allowed):
        if rels:
            self._call("rm", [stamp] + allowed, json.dumps(rels).encode("utf-8"))

    def run_shell(self, command, tty=False):
        return subprocess.call(self._ssh_argv(tty) + [command])


# ---------------------------------------------------------------------------
# Config and state
# ---------------------------------------------------------------------------

def clean_paths(name, values):
    out = []
    for v in values:
        v = str(v).replace("\\", "/").strip("/")
        p = PurePosixPath(v)
        if not v or p.is_absolute() or ".." in p.parts or ":" in v:
            raise SyncError("%s: %r must be a relative folder or file name" % (name, v))
        out.append(v)
    return out


def clean_server_files(values):
    if not isinstance(values, list):
        raise SyncError("\"server_files\" must be a list")
    out, names, dests = [], set(), set()
    for i, raw in enumerate(values, 1):
        if not isinstance(raw, dict):
            raise SyncError("server_files #%d must be an object with \"local\" and \"remote\"" % i)
        extra = sorted(set(raw) - {"local", "remote", "sudo", "mode", "after"})
        if extra:
            raise SyncError("server_files #%d: unknown key(s): %s" % (i, ", ".join(extra)))
        local = clean_paths("server_files local", [raw.get("local", "")])[0]
        remote = str(raw.get("remote", ""))
        parts = PurePosixPath(remote).parts
        if not remote.startswith("/") or remote.endswith("/") or ".." in parts or len(parts) < 3 \
                or "\\" in remote or any(c in remote for c in "\n\r\0"):
            raise SyncError("server_files #%d: \"remote\" must be a full file path such as "
                            "/var/www/flask/wsgi.ini" % i)
        mode = str(raw.get("mode", "644"))
        if not re.fullmatch(r"[0-7]{3,4}", mode):
            raise SyncError("server_files #%d: \"mode\" must look like 644" % i)
        name = PurePosixPath(remote).name
        if not re.fullmatch(r"[A-Za-z0-9._@-]+", name) or name.startswith("."):
            raise SyncError("server_files #%d: file name %r has unsupported characters" % (i, name))
        if name in names or remote in dests:
            raise SyncError("server_files #%d: %r duplicates another entry (file names must differ)" % (i, name))
        names.add(name)
        dests.add(remote)
        out.append({"local": local, "remote": remote, "sudo": bool(raw.get("sudo", False)),
                    "mode": mode, "after": str(raw.get("after", "")).strip(), "name": name})
    return out


def load_config(root, path):
    cfg_path = Path(path) if path else root / CONFIG_FILE
    if not cfg_path.exists():
        raise SyncError("%s not found. Run `python sync.py init` and edit it." % cfg_path.name)
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise SyncError("%s is not valid JSON: %s" % (cfg_path.name, exc))
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise SyncError("unknown setting(s) in %s: %s" % (cfg_path.name, ", ".join(unknown)))
    cfg = dict(DEFAULTS, **data)
    if not cfg["host"] or cfg["host"].startswith("-"):
        raise SyncError("set \"host\" in %s (an ssh alias or user@address)" % cfg_path.name)
    if not str(cfg["remote_path"]).startswith(("/", "~")):
        raise SyncError("\"remote_path\" must be an absolute path or start with ~")
    if cfg["remote_venv"] and not str(cfg["remote_venv"]).startswith(("/", "~")):
        raise SyncError("\"remote_venv\" must be an absolute path or start with ~")
    base = str(cfg["remote_base"]).strip().rstrip("/")
    if str(cfg["remote_base"]).strip():
        parts = [x for x in base.split("/") if x]
        if not base.startswith("/") or len(parts) < 2 or base in UNSAFE_BASES or ".." in parts:
            raise SyncError("\"remote_base\" must be a folder such as /var/www/flask, "
                            "not a system folder")
        rpath = str(cfg["remote_path"]).rstrip("/")
        if rpath != base and not rpath.startswith(base + "/"):
            raise SyncError("\"remote_path\" must be inside \"remote_base\"")
        if cfg["remote_group"] and not re.fullmatch(r"[a-z_][a-z0-9_-]*", str(cfg["remote_group"])):
            raise SyncError("\"remote_group\" is not a valid group name")
    cfg["remote_base"] = base
    staging = str(cfg["remote_staging"]).strip().rstrip("/")
    if not staging:
        staging = base + "/staging" if base else "~/.ptsync/staging"
    if not staging.startswith(("/", "~/")) or ".." in staging.split("/") or len(staging) < 4:
        raise SyncError("\"remote_staging\" must be an absolute folder such as /var/www/flask/staging")
    cfg["remote_staging"] = staging
    cfg["server_files"] = clean_server_files(cfg["server_files"])
    for item in cfg["server_files"]:
        for c in cfg["code_paths"] + cfg["data_paths"]:
            lp = item["local"]
            if lp == c or lp.startswith(c.strip("/") + "/"):
                raise SyncError("%r is both a server file and inside code/data path %r" % (lp, c))
    cfg["data_paths"] = clean_paths("data_paths", cfg["data_paths"])
    cfg["code_paths"] = clean_paths("code_paths", cfg["code_paths"])
    for d in cfg["data_paths"]:
        for c in cfg["code_paths"]:
            if d == c or d.startswith(c + "/") or c.startswith(d + "/"):
                raise SyncError("%r is listed as both data and code" % d)
    return cfg


def state_key(cfg):
    return "%s:%s" % (cfg["host"], cfg["remote_path"])


def load_base(root, cfg):
    path = root / STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(state_key(cfg), {})
    except ValueError:
        return {}


def save_base(root, cfg, base):
    path = root / STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state[state_key(cfg)] = base
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def plan_data(local, remote, base, prefer=None, allow_delete=False):
    """Three-way comparison. `base` is what both sides held after the last sync."""
    plan = {"push": [], "pull": [], "rm_remote": [], "rm_local": [],
            "conflicts": [], "kept": []}
    for p in sorted(set(local) | set(remote) | set(base)):
        l, r, b = local.get(p), remote.get(p), base.get(p)
        if l == r:
            continue
        if r == b:  # only this side changed
            if l is not None:
                plan["push"].append(p)
            elif allow_delete:
                plan["rm_remote"].append(p)
            else:
                plan["kept"].append((p, "deleted here, kept on the server"))
        elif l == b:  # only the server changed
            if r is not None:
                plan["pull"].append(p)
            elif allow_delete:
                plan["rm_local"].append(p)
            else:
                plan["kept"].append((p, "deleted on the server, kept here"))
        elif prefer == "local":
            plan["push" if l else "rm_remote"].append(p)
        elif prefer == "remote":
            plan["pull" if r else "rm_local"].append(p)
        else:
            plan["conflicts"].append(p)
    return plan


def plan_force(local, remote, winner, allow_delete=False):
    """Make the losing side match the winning side, ignoring sync history.

    Differing or missing files are copied from the winner. Files that exist only on
    the losing side are kept unless allow_delete is set."""
    plan = {"push": [], "pull": [], "rm_remote": [], "rm_local": [],
            "conflicts": [], "kept": []}
    src, dst = (local, remote) if winner == "local" else (remote, local)
    copy, drop = ("push", "rm_remote") if winner == "local" else ("pull", "rm_local")
    for p in sorted(set(local) | set(remote)):
        if local.get(p) == remote.get(p):
            continue
        if src.get(p) is not None:
            plan[copy].append(p)
        elif allow_delete:
            plan[drop].append(p)
        else:
            plan["kept"].append((p, "only on the %s side" %
                                 ("server" if winner == "local" else "local")))
    return plan


def next_base(base, local, remote, sent, received, removed):
    new = dict(base)
    for p in set(local) | set(remote) | set(base):
        if local.get(p) == remote.get(p):
            if local.get(p) is None:
                new.pop(p, None)
            else:
                new[p] = local[p]
    new.update(sent)
    new.update(received)
    for p in removed:
        new.pop(p, None)
    return new


def show(title, rows):
    print(title)
    if not rows:
        print("  already in sync")
    for label, path in rows:
        print("  %-18s %s" % (label, path))


def confirm(args, prompt):
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise SyncError("not a terminal. Re-run with --yes to apply, or --dry-run to preview.")
    return input(prompt).strip().lower() in ("y", "yes")


def setup_command(cfg, sudo):
    """Shell command that creates the server folders and hands them to the login user."""
    base = shlex.quote(cfg["remote_base"])
    dirs = [cfg["remote_path"]]
    if cfg["server_files"] and cfg["remote_staging"].startswith(cfg["remote_base"] + "/"):
        dirs.append(cfg["remote_staging"])
    path = " ".join(shlex.quote(d) for d in dirs)
    group = cfg["remote_group"]
    owner = "$(id -un):%s" % group if group else "$(id -un)"
    mode = "2775" if group else "755"
    return " && ".join([
        "%s mkdir -p %s %s" % (sudo, base, " ".join(shlex.quote(d) for d in dirs)),
        "%s chown -R %s %s" % (sudo, owner, base),
        "%s chmod %s %s %s" % (sudo, mode, base, path),
    ])


def prepare_remote(cfg, remote, args, dry=False):
    """Create the server folders with sudo when the login user cannot write there."""
    if not cfg["remote_base"] or remote.check().get("writable"):
        return
    print("\nThe server folders do not exist yet or are not writable by your login.")
    print("They will be created and handed to your login user with sudo:")
    print("  " + setup_command(cfg, "sudo"))
    if dry:
        return
    if not confirm(args, "Run this on the server? [y/N] "):
        raise SyncError("cancelled: the server folder is not writable")
    if remote.run_shell(setup_command(cfg, "sudo -n")) != 0:
        print("sudo needs a password. Asking on the server...")
        if remote.run_shell(setup_command(cfg, "sudo"), tty=True) != 0:
            raise SyncError("could not create the server folders. Run the command above "
                            "on the server by hand, or allow passwordless sudo for this login.")
    if not remote.check().get("writable"):
        raise SyncError("the server folder is still not writable by your login")
    print("Server folders ready.\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(root, args):
    path = root / CONFIG_FILE
    if path.exists():
        raise SyncError("%s already exists" % CONFIG_FILE)
    path.write_text(json.dumps(EXAMPLE, indent=2) + "\n", encoding="utf-8")
    print("Wrote %s. Edit it, then run: python sync.py check" % CONFIG_FILE)
    print("  host           an ssh alias from ~/.ssh/config, or user@address")
    print("  remote_path    folder on the server that holds the app")
    print("  post_deploy    optional command to run on the server after a deploy")
    return 0


def cmd_check(root, cfg, remote, args):
    print("host         %s" % cfg["host"])
    print("remote_path  %s" % cfg["remote_path"])
    info = remote.check()
    print("ssh          ok (key login works)")
    print("python       %s on the server" % info.get("python", "?"))
    if info.get("exists"):
        note = "" if info.get("writable") else (
            ", not writable; deploy will fix it with sudo" if cfg["remote_base"] else ", NOT writable")
        print("folder       exists" + note)
    elif info.get("writable"):
        print("folder       missing, will be created on the first sync")
    elif cfg["remote_base"]:
        print("folder       missing; deploy will create it with sudo (remote_base is set)")
    else:
        print("folder       missing and cannot be created; check permissions")
        return 1
    return 0


def local_checks(root, cfg, ssh_bin):
    """Offline checks of sync.config.json against this machine."""
    out = []
    add = lambda status, name, detail: out.append((status, name, detail))

    found = shutil.which(ssh_bin)
    if found:
        add("ok", "ssh client", found)
    else:
        add("fail", "ssh client", "%r not found; install the OpenSSH client" % ssh_bin)

    key = cfg["identity_file"]
    if not key:
        add("ok", "identity_file", "not set; ssh will use your default keys or ssh-agent")
    else:
        kp = Path(os.path.expanduser(key))
        if not kp.is_file():
            add("fail", "identity_file", "%s does not exist" % kp)
        else:
            try:
                head = kp.read_text(encoding="utf-8", errors="replace")[:600]
            except OSError as exc:
                head = ""
                add("fail", "identity_file", "cannot read %s: %s" % (kp, exc))
            if head:
                first = head.strip().splitlines()[0] if head.strip() else ""
                if first.startswith("PuTTY-User-Key-File"):
                    add("fail", "identity_file",
                        "%s is a PuTTY .ppk key; OpenSSH needs the private key in OpenSSH or PEM "
                        "format (PuTTYgen > Conversions > Export OpenSSH key)" % kp)
                elif "-----BEGIN" not in first:
                    add("fail", "identity_file", "%s does not start with -----BEGIN; "
                        "it is not an OpenSSH/PEM private key" % kp)
                elif "PUBLIC KEY" in first or first.startswith("ssh-"):
                    add("fail", "identity_file", "%s looks like the public key; use the private one" % kp)
                else:
                    notes = []
                    if "ENCRYPTED" in head:
                        notes.append("passphrase-protected; BatchMode cannot prompt, so load it "
                                     "into ssh-agent first")
                    if os.name == "posix" and (kp.stat().st_mode & 0o077):
                        notes.append("permissions too open; run chmod 600")
                    add("warn" if notes else "ok", "identity_file",
                        "%s%s" % (kp, " (" + "; ".join(notes) + ")" if notes else ""))

    for label in ("code_paths", "data_paths"):
        for rel in cfg[label]:
            target = root / rel
            if target.exists():
                count = sum(1 for _ in target.rglob("*") if _.is_file()) if target.is_dir() else 1
                add("ok", label[:-1], "%s (%d file%s)" % (rel, count, "" if count == 1 else "s"))
            else:
                add("warn" if label == "data_paths" else "fail", label[:-1],
                    "%s does not exist next to sync.py" % rel)

    for it in cfg["server_files"]:
        if (root / it["local"]).is_file():
            add("ok", "server_file", "%s -> %s%s" % (it["local"], it["remote"],
                                                    " (sudo)" if it["sudo"] else ""))
        else:
            add("fail", "server_file", "%s does not exist next to sync.py" % it["local"])

    if cfg["remote_venv"]:
        if (root / "requirements.txt").is_file() and "requirements.txt" in cfg["code_paths"]:
            add("ok", "requirements", "requirements.txt will be installed into the server venv")
        else:
            add("warn", "requirements",
                "remote_venv is set but requirements.txt is not in code_paths or is missing")
    return out


def cmd_test(root, args):
    """Run every check against sync.config.json and report PASS / WARN / FAIL."""
    rows = []
    try:
        cfg = load_config(root, args.config)
    except SyncError as exc:
        print("[FAIL] config           %s" % exc)
        print("\n0 passed, 0 warnings, 1 failed.")
        return 1
    rows.append(("ok", "config", "%s is valid; host %s, remote_path %s"
                 % (args.config or CONFIG_FILE, cfg["host"], cfg["remote_path"])))
    remote = Remote(cfg)
    rows += local_checks(root, cfg, remote.ssh_bin)

    if not any(r[0] == "fail" and r[1] in ("ssh client", "identity_file") for r in rows):
        hooks = [cfg["post_deploy"]] + [it["after"] for it in cfg["server_files"] if it["after"]]
        spec = {"base": cfg["remote_base"], "path": cfg["remote_path"], "venv": cfg["remote_venv"],
                "group": cfg["remote_group"], "post_deploy": " ; ".join(h for h in hooks if h),
                "server_files": cfg["server_files"], "staging": cfg["remote_staging"]}
        try:
            rows.append(("ok", "ssh", "key login to %s works" % cfg["host"]))
            rows += [tuple(r) for r in remote.test(spec)]
        except SyncError as exc:
            rows.pop()
            rows.append(("fail", "ssh", str(exc)))
    else:
        rows.append(("warn", "ssh", "skipped until the problems above are fixed"))

    tag = {"ok": " OK ", "warn": "WARN", "fail": "FAIL"}
    for status, name, detail in rows:
        print("[%s] %-17s %s" % (tag[status], name, detail))
    n = {k: sum(1 for r in rows if r[0] == k) for k in tag}
    print("\n%d passed, %d warning%s, %d failed." % (n["ok"], n["warn"], "" if n["warn"] == 1 else "s", n["fail"]))
    if n["fail"] == 0 and n["warn"] == 0:
        print("Everything checks out. You can run: python sync.py deploy")
    return 1 if n["fail"] else 0


def cmd_data(root, cfg, remote, args):
    paths = cfg["data_paths"]
    local = scan(root, paths)
    server = remote.manifest(paths)
    base = load_base(root, cfg)
    force = getattr(args, "force", None)
    if force:
        print("FORCE: the %s side wins; sync history is ignored." %
              ("local" if force == "local" else "server"))
        plan = plan_force(local, server, force, args.delete)
        base = {}
    else:
        plan = plan_data(local, server, base, args.prefer, args.delete)

    rows = [("push to server", p) for p in plan["push"]]
    rows += [("pull from server", p) for p in plan["pull"]]
    rows += [("delete on server", p) for p in plan["rm_remote"]]
    rows += [("delete here", p) for p in plan["rm_local"]]
    rows += [("CONFLICT", p) for p in plan["conflicts"]]
    rows += [("kept", "%s (%s; use --delete)" % (p, why)) for p, why in plan["kept"]]
    show("Data: %s" % ", ".join(paths), rows)

    changes = len(plan["push"]) + len(plan["pull"]) + len(plan["rm_remote"]) + len(plan["rm_local"])
    if plan["conflicts"]:
        print("\nBoth sides changed these since the last sync. Nothing was overwritten.")
        print("Pick a winner with --prefer local or --prefer remote (the loser is backed up).")
    if args.dry_run or changes == 0:
        if args.dry_run and plan["push"]:
            prepare_remote(cfg, remote, args, dry=True)
        return 2 if plan["conflicts"] else 0
    if not confirm(args, "Apply %d change(s)? [y/N] " % changes):
        print("Cancelled.")
        return 1

    if plan["push"]:
        prepare_remote(cfg, remote, args)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    local_backup = root / BACKUP_DIR / stamp
    sent = remote.put(root, plan["push"], stamp, paths)
    remote.remove(plan["rm_remote"], stamp, paths)
    received = remote.get(root, plan["pull"], paths, local_backup)
    remove_files(root, plan["rm_local"], paths, local_backup)
    save_base(root, cfg, next_base(base, local, server, sent, received,
                                   plan["rm_remote"] + plan["rm_local"]))
    print("Done. Anything overwritten or deleted was backed up under %s/%s "
          "on the side that changed." % (BACKUP_DIR, stamp))
    return 2 if plan["conflicts"] else 0


def remote_quote(path):
    """Quote a remote path but keep a leading ~/ working."""
    path = str(path).rstrip("/") or "/"
    if path == "~":
        return "$HOME"
    if path.startswith("~/"):
        return "$HOME/" + shlex.quote(path[2:])
    return shlex.quote(path)


def ensure_venv(cfg, remote, args, requirements_changed=False, dry=False):
    """Create the server virtualenv if it is missing and install requirements.txt.

    Runs on every deploy, so it also repairs a venv that was deleted. Requirements
    are installed when the venv is new or requirements.txt was just pushed."""
    if not cfg["remote_venv"]:
        return
    venv = remote_quote(cfg["remote_venv"])
    py = shlex.quote(cfg["remote_python"])
    pip = venv + "/bin/pip"
    req = remote_quote(cfg["remote_path"]) + "/requirements.txt"

    exists = remote.run_shell("test -x %s/bin/pip" % venv) == 0
    if dry:
        if not exists:
            print("\nWould create the virtualenv %s and install requirements.txt." % cfg["remote_venv"])
        return

    if not exists:
        print("\nCreating the virtualenv %s ..." % cfg["remote_venv"])
        if remote.run_shell("%s -m venv %s" % (py, venv)) != 0:
            print("\nCreating the virtualenv failed. On Ubuntu the usual cause is the missing "
                  "python3-venv package.")
            fix = "sudo apt-get install -y python3-venv"
            print("Fix: %s" % fix)
            if not confirm(args, "Run it now on the server? [y/N] "):
                raise SyncError("virtualenv was not created")
            if remote.run_shell(fix, tty=True) != 0:
                raise SyncError("could not install python3-venv")
            if remote.run_shell("%s -m venv %s" % (py, venv)) != 0:
                raise SyncError("virtualenv creation still failed")
        requirements_changed = True
        print("Virtualenv created.")

    if requirements_changed:
        if remote.run_shell("test -f %s" % req) != 0:
            print("No requirements.txt on the server yet, so nothing was installed.")
            return
        print("Installing requirements into %s ..." % cfg["remote_venv"])
        if remote.run_shell("%s install -r %s" % (pip, req)) != 0:
            raise SyncError("pip install failed; see the output above")


def plan_server_files(root, cfg, remote, force=False):
    """Server files whose installed copy differs from the local one."""
    items = cfg["server_files"]
    if not items:
        return []
    for it in items:
        if not (root / it["local"]).is_file():
            raise SyncError("server_files: %s does not exist next to sync.py" % it["local"])
    have = remote.filehash([it["remote"] for it in items])
    return [it for it in items
            if force or have.get(it["remote"]) != sha256_file(root / it["local"])]


def staged_path(cfg, item):
    return cfg["remote_staging"].rstrip("/") + "/" + item["name"]


def install_command(cfg, item, stamp, sudo):
    """Back up the installed copy, then install the staged file with the right owner."""
    staged = remote_quote(staged_path(cfg, item))
    dest = shlex.quote(item["remote"])
    keep = remote_quote(cfg["remote_staging"] + "/.backup/" + stamp)
    # Refuse a staged file that is a symlink or belongs to someone else.
    guard = "test -f %s && test ! -L %s && test -O %s" % (staged, staged, staged)
    backup = "if [ -f %s ]; then mkdir -p %s && cp -p %s %s/%s; fi" % (
        dest, keep, dest, keep, item["name"])
    if item["sudo"]:
        install = "%s install -m %s -o root -g root %s %s" % (sudo, item["mode"], staged, dest)
    else:
        install = "mkdir -p %s && install -m %s %s %s" % (
            shlex.quote(PurePosixPath(item["remote"]).parent.as_posix()), item["mode"], staged, dest)
    return guard + " && " + backup + " && " + install


def run_with_sudo_fallback(remote, build, uses_sudo):
    """Run a command without a password first, then let sudo ask in the terminal."""
    if remote.run_shell(build("sudo -n")) == 0:
        return True
    if not uses_sudo:
        return False
    print("sudo needs a password. Asking on the server...")
    return remote.run_shell(build("sudo"), tty=True) == 0


def install_server_files(root, cfg, remote, items, stamp):
    remote.stage(root, [(it["local"], it["name"]) for it in items])
    afters = []
    for it in items:
        print("Installing %s" % it["remote"])
        if not run_with_sudo_fallback(remote, lambda sudo, it=it: install_command(cfg, it, stamp, sudo),
                                      it["sudo"]):
            raise SyncError(
                "could not install %s. For passwordless installs add this line with "
                "`sudo visudo -f /etc/sudoers.d/flask-files` (see `python sync.py test`)." % it["remote"])
        if it["after"] and it["after"] not in afters:
            afters.append(it["after"])
    for cmd in afters:
        print("Running: %s" % cmd)
        ok = remote.run_shell(cmd) == 0 or ("sudo" in cmd and remote.run_shell(cmd, tty=True) == 0)
        if not ok:
            raise SyncError("`%s` failed" % cmd)


def cmd_deploy(root, cfg, remote, args):
    paths = cfg["code_paths"]
    local = scan(root, paths)
    server = remote.manifest(paths)
    force = getattr(args, "force", False)
    if force:
        print("FORCE: re-sending every code file, even if it looks identical.")
    push = [p for p, h in sorted(local.items()) if force or server.get(p) != h]
    extra = [p for p in sorted(server) if p not in local]
    remove = extra if args.delete else []

    rows = [("push to server", p) for p in push]
    rows += [("delete on server", p) for p in remove]
    rows += [("kept", "%s (only on the server; use --delete)" % p) for p in extra if not args.delete]
    sfiles = plan_server_files(root, cfg, remote, force)
    rows += [("install on server", "%s -> %s%s" % (it["local"], it["remote"],
                                                  " (sudo)" if it["sudo"] else "")) for it in sfiles]
    show("Code: %s" % ", ".join(paths + [it["local"] for it in cfg["server_files"]]), rows)

    changes = len(push) + len(remove) + len(sfiles)
    if args.dry_run or changes == 0:
        if args.dry_run and (push or sfiles):
            prepare_remote(cfg, remote, args, dry=True)
        if args.dry_run:
            ensure_venv(cfg, remote, args, dry=True)
        elif cfg["remote_venv"]:
            ensure_venv(cfg, remote, args, requirements_changed=getattr(args, "pip", False))
        return 0
    if not confirm(args, "Apply %d change(s)? [y/N] " % changes):
        print("Cancelled.")
        return 1

    if push or sfiles:
        prepare_remote(cfg, remote, args)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    remote.put(root, push, stamp, paths)
    remote.remove(remove, stamp, paths)
    if sfiles:
        install_server_files(root, cfg, remote, sfiles, stamp)
    print("Deployed %d file(s)." % changes)
    if cfg["remote_venv"]:
        ensure_venv(cfg, remote, args, requirements_changed="requirements.txt" in push or getattr(args, "pip", False))
    elif "requirements.txt" in push:
        print("requirements.txt changed. Install it inside the server's virtualenv:")
        print("  pip install -r %s/requirements.txt" % cfg["remote_path"])
    if cfg["post_deploy"] and not args.no_hook:
        print("Running post_deploy: %s" % cfg["post_deploy"])
        code = remote.run_shell(cfg["post_deploy"])
        if code != 0:
            raise SyncError("post_deploy exited with %d" % code)
    return 0


def cmd_status(root, cfg, remote, args):
    args.dry_run, args.prefer, args.delete, args.force = True, None, False, None
    worst = cmd_data(root, cfg, remote, args)
    print()
    return max(worst, cmd_deploy(root, cfg, remote, args))


def build_parser():
    p = argparse.ArgumentParser(prog="sync.py", description="Sync PT Helper over SSH.")
    p.add_argument("--config", help="path to a config file (default: sync.config.json)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="write a starter sync.config.json")
    sub.add_parser("check", help="test the ssh connection and remote folder")
    sub.add_parser("test", help="check sync.config.json, the key, local paths and the server setup")
    sub.add_parser("status", help="preview data and deploy changes")

    d = sub.add_parser("data", help="two-way sync of the data folders")
    d.add_argument("-n", "--dry-run", action="store_true", help="show the plan only")
    d.add_argument("-y", "--yes", action="store_true", help="do not ask before applying")
    d.add_argument("--delete", action="store_true", help="also propagate deletions")
    d.add_argument("--prefer", choices=("local", "remote"),
                   help="resolve conflicts in favor of this side")
    d.add_argument("--force", choices=("local", "remote"), metavar="{local,remote}",
                   help="make the other side match this one, ignoring sync history "
                        "(overwritten files are backed up)")

    c = sub.add_parser("deploy", help="push application code to the server")
    c.add_argument("-n", "--dry-run", action="store_true", help="show the plan only")
    c.add_argument("-y", "--yes", action="store_true", help="do not ask before applying")
    c.add_argument("--delete", action="store_true", help="remove server files not present here")
    c.add_argument("--force", action="store_true",
                   help="re-send every code file, even if the server copy looks identical")
    c.add_argument("--pip", action="store_true",
                   help="run pip install -r requirements.txt on the server even if it did not change")
    c.add_argument("--no-hook", action="store_true", help="skip the post_deploy command")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parent
    try:
        if args.command == "init":
            return cmd_init(root, args)
        if args.command == "test":
            return cmd_test(root, args)
        cfg = load_config(root, args.config)
        remote = Remote(cfg)
        handler = {"check": cmd_check, "data": cmd_data, "deploy": cmd_deploy,
                   "status": cmd_status}[args.command]
        if args.command == "status":
            args.yes = True
        return handler(root, cfg, remote, args)
    except SyncError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__ptsync_remote__":
    remote_main(sys.argv[1:])
elif __name__ == "__main__":
    sys.exit(main())
