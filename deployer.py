#!/usr/bin/env python3
"""NiYoj — pick an app, it SSHes to its server and runs the deploy.

Run:  python deployer.py          (or deployer.exe)
      python deployer.py --selftest

Native WebView2 window (pywebview). Everything is configured in the UI;
apps.json next to the exe is just where it lands.

Copyright (C) 2026 bodish-coder.  All rights reserved.  Source is public
for reading only — no licence is granted to use, copy, modify or
distribute this code.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "1.1.6"   # bumped by --bump on every build; keep the literal on one line

# ponytail: frozen exe unpacks to a temp dir, so anchor config next to the exe
ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _cfg_path():
    """Beside the exe, unless that folder is read-only (installed under
    Program Files) — then per-user, so settings never fail to save."""
    beside = ROOT / "apps.json"
    if beside.exists():
        return beside
    probe = ROOT / ".niyoj-write-test"
    try:
        probe.touch()
        probe.unlink()
        return beside
    except OSError:
        d = Path(os.environ.get("APPDATA", ROOT)) / "NiYoj"
        d.mkdir(parents=True, exist_ok=True)
        return d / "apps.json"


CFG = _cfg_path()

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── config ──────────────────────────────────────────────────────────
def load_cfg():
    if not CFG.exists():
        CFG.write_text(json.dumps({"servers": {}, "apps": {}}, indent=2), encoding="utf-8")
    # utf-8-sig: an editor (or PowerShell) may have left a BOM — plain utf-8 chokes on it
    cfg = json.loads(CFG.read_text(encoding="utf-8-sig"))
    cfg.setdefault("servers", {})
    cfg.setdefault("apps", {})
    return cfg


def save_cfg(cfg):
    CFG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def rename_key(d, old, new, value):
    """Replace/insert preserving insertion order, so the sidebar doesn't jump."""
    out = {}
    for k, v in d.items():
        if k == old:
            out[new] = value
        else:
            out[k] = v
    if old not in d:
        out[new] = value
    return out


# ── ssh ─────────────────────────────────────────────────────────────
# The remote scripts live here as templates so the Android app can read the exact
# same text out of scripts.json — one source of truth, two front ends.
DEPLOY_TPL = "\n".join([
    "set -euo pipefail",
    "if [ ! -d {dir}/.git ]; then git clone -b {branch} {repo} {dir}; fi",
    "cd {dir}",
    "git fetch --all --prune",
    "git reset --hard origin/{branch}",
    "git submodule update --init --recursive || true",
    # deploy/deploy.sh or a root deploy.sh; a static site has neither, the pull is the deploy
    "if [ -f deploy/deploy.sh ]; then sudo bash deploy/deploy.sh;"
    " elif [ -f deploy.sh ]; then sudo bash deploy.sh;"
    " else echo 'no deploy.sh — files updated in place'; fi",
])

PREP_TPL = "set -e; rm -rf {dest}.new {dest}.old; mkdir -p {dest}.new"

# Only swap once the upload landed, so a half-sent site never goes live.
SWAP_TPL = "\n".join([
    "set -e",
    "if [ -d {dest} ]; then mv {dest} {dest}.old; fi",
    "mv {dest}.new {dest}",
    "rm -rf {dest}.old",
    "echo uploaded to {dest}",
])


def remote_script(app):
    """Clone-if-missing, hard-reset to origin, run the repo's own deploy.sh.

    Same script serves a brand-new app and the 200th redeploy, because
    deploy/deploy.sh is idempotent (see DEPLOYMENT_PLAYBOOK.md).
    """
    if app.get("cmd"):
        return app["cmd"]
    return DEPLOY_TPL.format(dir=app["dir"], repo=app["repo"],
                             branch=app.get("branch", "main"))


def prep_script(dest):
    return PREP_TPL.format(dest=dest)


def swap_script(dest):
    return SWAP_TPL.format(dest=dest)


def ssh_argv(server):
    key = os.path.expanduser(server.get("key") or "~/.ssh/id_ed25519")
    return [
        "ssh",
        "-o", "BatchMode=yes",              # never hang on a password prompt
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ConnectTimeout=12",
        "-i", key,
        "-p", str(server.get("port", 22)),
        f"{server.get('user', 'root')}@{server['host']}",
        "bash -s",                          # script arrives on stdin, no quoting
    ]


# Walks the usual deploy roots, reports every git checkout it finds. Tab-separated
# because printf-ing JSON from bash is a quoting trap.
SCAN_SCRIPT = r"""
set -u
have(){ command -v "$1" >/dev/null 2>&1; }
running=""
if have docker; then running=$(docker ps --format '{{.Names}}' 2>/dev/null | tr '\n' ' '); fi
echo "###APPS###"
for r in /opt /srv /var/www /root /home; do
  [ -d "$r" ] || continue
  for d in "$r"/* "$r"/*/*; do
    [ -d "$d/.git" ] || continue
    n=$(basename "$d")
    repo=$(git -C "$d" config --get remote.origin.url 2>/dev/null)
    br=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null)
    when=$(git -C "$d" log -1 --format=%cd --date=short 2>/dev/null)
    ds=0; { [ -f "$d/deploy/deploy.sh" ] || [ -f "$d/deploy.sh" ]; } && ds=1
    dc=0; for f in "$d"/docker-compose*.y*ml "$d"/compose*.y*ml; do [ -f "$f" ] && dc=1; done
    up=0; case " $running " in *" $n "*) up=1;; esac
    dom=""
    for c in /etc/nginx/sites-enabled/*; do
      [ -f "$c" ] || continue
      if grep -q "$n" "$c" 2>/dev/null; then
        dom=$(sed -n 's/^[[:space:]]*server_name[[:space:]]\{1,\}\([^;]*\);.*/\1/p' "$c" \
              | head -1 | awk '{print $1}')
        break
      fi
    done
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$n" "$d" "$repo" "$br" "$when" "$ds" "$dc" "$up" "$dom"
  done
done
"""


def parse_scan(text):
    """Rows after the ###APPS### marker -> dicts, first path wins on duplicates."""
    rows, seen, started = [], set(), False
    for line in text.splitlines():
        if line.strip() == "###APPS###":
            started = True
            continue
        if not started or "\t" not in line:
            continue
        f = (line.split("\t") + [""] * 9)[:9]
        if f[1] in seen:
            continue
        seen.add(f[1])
        rows.append({
            "name": f[0], "dir": f[1], "repo": f[2], "branch": f[3] or "main",
            "commit": f[4], "deploy_sh": f[5] == "1", "compose": f[6] == "1",
            "up": f[7] == "1", "domain": f[8],
        })
    return rows


def ssh_run(server, script, on_line):
    p = subprocess.Popen(
        ssh_argv(server),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    )
    # Windows text mode would turn every \n into \r\n and bash chokes on the \r
    p.stdin.reconfigure(newline="\n")
    p.stdin.write(script + "\n")
    p.stdin.close()
    for line in p.stdout:
        on_line(line)
    return p.wait()


def upload_run(server, local, dest, on_line):
    """Pipe a local folder up as a tarball. ponytail: tar+ssh only — no rsync on Windows."""
    src = Path(os.path.expandvars(os.path.expanduser(local)))
    if not src.is_dir():
        on_line(f"local folder not found: {src}\n")
        return 2
    rc = ssh_run(server, prep_script(dest), on_line)
    if rc:
        return rc
    # the tarball needs stdin, so this hop takes its command as argv — kept to bare
    # words (no quotes, no $) so nothing depends on how ssh re-joins them remotely
    tar = subprocess.Popen(["tar", "-cf", "-", "-C", str(src), "."],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
    p = subprocess.Popen(
        ssh_argv(server)[:-1] + ["tar", "-C", f"{dest}.new", "-xf", "-"],
        stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    )
    tar.stdout.close()                       # so tar sees EPIPE if ssh dies
    for line in p.stdout:
        on_line(line)
    rc = p.wait() or tar.wait()
    if rc:
        on_line(f"upload failed (exit {rc}) — {dest} left untouched\n")
        return rc
    return ssh_run(server, swap_script(dest), on_line)


# ── ssh keys ────────────────────────────────────────────────────────
# The comment is the only free text that reaches the remote shell, so no quotes
# in it — then single-quoting the whole key below is airtight.
PUB_RE = re.compile(
    r"^(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+(?: [^\r\n'\"]*)?$")


PROBE = ("uname -sr; command -v git >/dev/null && git --version | head -1 "
         "|| echo 'git MISSING'")

AUTHZ_TPL = ("set -e; umask 077; mkdir -p ~/.ssh; "
             "grep -qxF '{pub}' ~/.ssh/authorized_keys 2>/dev/null || "
             "echo '{pub}' >> ~/.ssh/authorized_keys; "
             "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; echo NIYOJ_KEY_OK")


def authz_script(pub):
    """One line, because Windows hands it to ssh as a single quoted argv word."""
    pub = pub.strip()
    if not PUB_RE.match(pub):
        raise ValueError("that is not an OpenSSH public key")
    return AUTHZ_TPL.format(pub=pub)


def password_argv(server, script):
    """Same host as ssh_argv, but password-only — this is the one hop that runs
    before the key exists, so it must NOT fall back to a key and must have a tty."""
    return [
        "ssh",
        "-o", "PubkeyAuthentication=no",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(server.get("port", 22)),
        f"{server.get('user', 'root')}@{server['host']}",
        script,
    ]


def gen_key(path=None, comment="niyoj"):
    """ed25519, no passphrase (deploys must never prompt). Never overwrites an
    existing key — that would lock you out of every server at once."""
    priv = Path(os.path.expandvars(os.path.expanduser(path or "~/.ssh/id_ed25519")))
    pub = Path(str(priv) + ".pub")
    if priv.exists():
        return {"ok": True, "existed": True, "path": str(priv),
                "pub": pub.read_text(encoding="utf-8").strip() if pub.exists() else ""}
    priv.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-C", comment],
            capture_output=True, text=True, creationflags=NO_WINDOW)
    except FileNotFoundError:
        return {"ok": False, "msg": "ssh-keygen not found — install OpenSSH"}
    if r.returncode:
        return {"ok": False, "msg": (r.stderr or r.stdout or "ssh-keygen failed").strip()}
    return {"ok": True, "existed": False, "path": str(priv),
            "pub": pub.read_text(encoding="utf-8").strip()}


# ── bridge exposed to the UI ────────────────────────────────────────
class Api:
    def __init__(self):
        self.window = None
        self.busy = set()

    # -- read/write config
    def get_state(self):
        return dict(load_cfg(), version=VERSION,
                    caps={"pick": True, "upload": True,
                          "install_key": "console" if os.name == "nt" else "manual"})

    def save_server(self, old, name, data):
        cfg = load_cfg()
        if not old and name in cfg["servers"]:
            return {"ok": False, "msg": "A server with that name already exists."}
        if old in cfg["servers"] and "last" in cfg["servers"][old]:
            data["last"] = cfg["servers"][old]["last"]      # keep reachability on edit
        cfg["servers"] = rename_key(cfg["servers"], old, name, data)
        if old and old != name:
            for a in cfg["apps"].values():
                if a.get("server") == old:
                    a["server"] = name
        save_cfg(cfg)
        return {"ok": True}

    def delete_server(self, name):
        cfg = load_cfg()
        cfg["servers"].pop(name, None)
        save_cfg(cfg)
        return {"ok": True}

    def save_app(self, old, name, data):
        cfg = load_cfg()
        if not old and name in cfg["apps"]:
            return {"ok": False, "msg": "An app with that name already exists."}
        data = {k: v for k, v in data.items() if v not in ("", None)}
        for k in ("last", "commit"):                    # not in the form; keep on edit
            if k in cfg["apps"].get(old, {}):
                data.setdefault(k, cfg["apps"][old][k])
        cfg["apps"] = rename_key(cfg["apps"], old, name, data)
        save_cfg(cfg)
        return {"ok": True}

    def delete_app(self, name):
        cfg = load_cfg()
        cfg["apps"].pop(name, None)
        save_cfg(cfg)
        return {"ok": True}

    def pick_key(self):
        import webview
        r = self.window.create_file_dialog(
            webview.OPEN_DIALOG, directory=os.path.expanduser("~/.ssh"))
        return r[0] if r else ""

    def pick_dir(self):
        import webview
        r = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return r[0] if r else ""

    def read_key(self, path=None):
        """Look, don't create — opening the key dialog must not mint a key."""
        pub = Path(os.path.expandvars(os.path.expanduser(
            (path or "~/.ssh/id_ed25519") + ".pub")))
        return pub.read_text(encoding="utf-8").strip() if pub.exists() else ""

    def gen_key(self, path=None):
        return gen_key(path or "~/.ssh/id_ed25519")

    def install_key(self, server, key_path=None, password=None):
        """Push the public key using a password — the only step a key can't do.
        Desktop has no password field: a console we spawn collects it, so it never
        enters the app. `password` exists for the Android bridge, which uses sshj."""
        if not server or not server.get("host"):
            return {"ok": False, "msg": "No host given"}
        k = gen_key(key_path or server.get("key") or "~/.ssh/id_ed25519")
        if not k["ok"]:
            return k
        if not k["pub"]:
            return {"ok": False, "msg": f"{k['path']}.pub is missing"}
        try:
            argv = password_argv(server, authz_script(k["pub"]))
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        line = " ".join(a if a.startswith("-") or "@" in a or a.isdigit() or a == "ssh"
                        else f'"{a}"' for a in argv)
        if os.name != "nt":
            return {"ok": True, "how": "manual", "cmd": line, "pub": k["pub"], "path": k["path"]}
        bat = Path(os.environ.get("TEMP", ".")) / "niyoj-install-key.cmd"
        bat.write_text("\r\n".join([
            "@echo off",
            "title NiYoj - install SSH key",
            f"echo Installing {k['path']}.pub on {server['host']}",
            "echo Type the server password when asked.",
            "echo.",
            line,
            "echo.",
            "pause",
            "",
        ]), encoding="utf-8")
        os.startfile(bat)          # its own console window, so ssh gets a real tty
        return {"ok": True, "how": "console", "pub": k["pub"], "path": k["path"]}

    # -- actions
    def test_conn(self, server, name=None):
        """Test a server dict straight from the form — no save needed first.
        If it belongs to a saved server, remember whether it answered."""
        if not server or not server.get("host"):
            return {"ok": False, "msg": "No host given"}
        out = []
        try:
            code = ssh_run(server, PROBE, out.append)
        except FileNotFoundError:
            return {"ok": False, "msg": "ssh not found on this PC"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        lines = [l.strip() for l in "".join(out).splitlines() if l.strip()]
        ok = code == 0
        msg = " · ".join(lines[-2:]) if ok and lines else (
            lines[-1] if lines else f"ssh exit {code}")
        if name:
            cfg = load_cfg()
            if name in cfg["servers"]:
                cfg["servers"][name]["last"] = {
                    "status": "ok" if ok else "fail",
                    "at": time.strftime("%d %b %H:%M"),
                }
                save_cfg(cfg)
        return {"ok": ok, "msg": msg}

    def scan_server(self, server_name):
        """Sniff a droplet for git checkouts and report what's already there."""
        cfg = load_cfg()
        sv = cfg["servers"].get(server_name)
        if not sv:
            return {"ok": False, "msg": "Unknown server"}
        out = []
        try:
            code = ssh_run(sv, SCAN_SCRIPT, out.append)
        except FileNotFoundError:
            return {"ok": False, "msg": "ssh not found on this PC"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        text = "".join(out)
        if code != 0 and "###APPS###" not in text:
            tail = text.strip().splitlines()
            return {"ok": False, "msg": tail[-1] if tail else f"ssh exit {code}"}
        rows = parse_scan(text)
        known = {n: a.get("dir") for n, a in cfg["apps"].items()}
        for r in rows:
            r["known"] = r["name"] in known and known[r["name"]] == r["dir"]
        return {"ok": True, "rows": rows}

    def import_apps(self, server_name, rows):
        """Add scanned apps; refresh repo/branch on ones already listed."""
        cfg = load_cfg()
        added = updated = 0
        for r in rows:
            entry = {"server": server_name, "repo": r.get("repo", ""),
                     "branch": r.get("branch") or "main", "dir": r["dir"],
                     "commit": r.get("commit", "")}      # last commit date, refreshed by each scan
            cur = cfg["apps"].get(r["name"])
            if cur and cur.get("dir") == r["dir"]:
                cur.update({k: v for k, v in entry.items() if v})
                updated += 1
            elif cur:                                   # same name, different path
                cfg["apps"][f"{r['name']} ({server_name})"] = entry
                added += 1
            else:
                cfg["apps"][r["name"]] = entry
                added += 1
        save_cfg(cfg)
        return {"ok": True, "added": added, "updated": updated}

    def deploy(self, name):
        if name in self.busy:
            return {"ok": False}
        self.busy.add(name)
        threading.Thread(target=self._deploy, args=(name,), daemon=True).start()
        return {"ok": True}

    def _emit(self, app, line):
        self.window.evaluate_js(f"logLine({json.dumps(app)},{json.dumps(line)})")

    def _deploy(self, name):
        cfg = load_cfg()
        app = cfg["apps"][name]
        sv = cfg["servers"][app["server"]]
        emit = lambda s: self._emit(name, s)
        if not app.get("cmd") and not app.get("repo") and not app.get("local"):
            emit(f"=== {name}: no repository set — click Edit and add the Git URL ===\n")
            self.busy.discard(name)
            self.window.evaluate_js(f"deployDone({json.dumps(name)})")
            return
        upload = app.get("local") and not app.get("cmd") and not app.get("repo")
        script = remote_script(app) if not upload else ""
        code = 1
        try:
            emit(f"$ ssh {sv.get('user', 'root')}@{sv['host']}  # {name}\n")
            if upload:
                emit(f"| upload {app['local']}  ->  {app['dir']}\n")
                emit("-" * 62 + "\n")
                code = upload_run(sv, app["local"], app["dir"], emit)
            else:
                emit("".join(f"| {ln}\n" for ln in script.splitlines()))
                emit("-" * 62 + "\n")
                code = ssh_run(sv, script, emit)
            emit(f"\n=== {name}: {'OK' if code == 0 else 'FAILED'} (exit {code}) ===\n")
        except Exception as e:
            emit(f"\n=== {name}: ERROR {e} ===\n")
        finally:
            self.busy.discard(name)
            cfg = load_cfg()                             # re-read: UI may have edited it
            if name in cfg["apps"]:
                cfg["apps"][name]["last"] = {
                    "status": "ok" if code == 0 else "fail",
                    "at": time.strftime("%d %b %H:%M"),
                }
                save_cfg(cfg)
            self.window.evaluate_js(f"deployDone({json.dumps(name)})")


def main():
    import webview
    api = Api()
    api.window = webview.create_window(
        f"NiYoj {VERSION}",
        html=(BUNDLE / "ui.html").read_text(encoding="utf-8"),
        js_api=api, width=1140, height=720, min_size=(880, 560),
        background_color="#0b0f16",
    )
    webview.start()


_VER_RE = {
    "deployer.py": re.compile(r'(?m)^(VERSION = ")(\d+)\.(\d+)\.(\d+)(")'),
    "installer.iss": re.compile(r'(?m)^(#define AppVersion ")(\d+)\.(\d+)\.(\d+)(")'),
    "android/app/build.gradle.kts":
        re.compile(r'(?m)^(\s*versionName = ")(\d+)\.(\d+)\.(\d+)(")'),
}


def next_version(major, minor, patch, part="patch"):
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def bump(part="patch"):
    """Raise the version in deployer.py and installer.iss so no two builds ever
    ship the same version. Called by build.cmd / build.sh."""
    here = Path(__file__).resolve().parent
    new = None
    for name, rx in _VER_RE.items():
        f = here / name
        if not f.exists():                      # the android project is optional
            continue
        old = f.read_text(encoding="utf-8")
        m = rx.search(old)
        if not m:
            raise SystemExit(f"no version literal found in {name}")
        new = next_version(int(m[2]), int(m[3]), int(m[4]), part)
        f.write_text(old[:m.start()] + m[1] + new + m[5] + old[m.end():],
                     encoding="utf-8", newline="")
    # Play refuses a repeat versionCode, so derive one that only ever climbs
    g = here / "android/app/build.gradle.kts"
    if g.exists() and new:
        maj, mi, pa = (int(x) for x in new.split("."))
        g.write_text(re.sub(r"(?m)^(\s*versionCode = )\d+",
                            lambda m: m[1] + str(maj * 10000 + mi * 100 + pa),
                            g.read_text(encoding="utf-8")),
                     encoding="utf-8", newline="")
    print(new)
    return new


SCRIPTS_JSON = (Path(__file__).resolve().parent
                / "android/app/src/main/assets/scripts.json")


def scripts_blob():
    """Every line of shell we send to a server. The Android app ships this file
    verbatim, so the two front ends can never drift apart."""
    return json.dumps({"deploy": DEPLOY_TPL, "prep": PREP_TPL, "swap": SWAP_TPL,
                       "authz": AUTHZ_TPL, "scan": SCAN_SCRIPT, "probe": PROBE},
                      indent=2) + chr(10)


def dump_scripts():
    SCRIPTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    SCRIPTS_JSON.write_text(scripts_blob(), encoding="utf-8", newline="")
    print(SCRIPTS_JSON)


def selftest():
    global CFG
    real, tmp = CFG, Path(os.environ.get("TEMP", ".")) / "niyoj_selftest.json"
    tmp.write_text("\ufeff" + json.dumps({"servers": {"a": {}}}), encoding="utf-8")
    CFG = tmp
    try:
        assert load_cfg()["servers"] == {"a": {}}, "config with a BOM must still load"
    finally:
        CFG = real
        tmp.unlink()

    # the script must reach the remote shell with LF only — a stray \r kills bash
    global ssh_argv
    real = ssh_argv
    ssh_argv = lambda _s: [sys.executable, "-c",
                           "import sys; sys.stdout.write(repr(sys.stdin.read()))"]
    got = []
    try:
        ssh_run({}, "line1\nline2", got.append)
    finally:
        ssh_argv = real
    assert "\\r" not in "".join(got), f"CRLF reached the shell: {''.join(got)}"

    s = remote_script({"dir": "/opt/x", "repo": "git@h:o/x", "branch": "dev"})
    assert "git clone -b dev git@h:o/x /opt/x" in s
    assert "git reset --hard origin/dev" in s and "sudo bash deploy/deploy.sh" in s
    assert "no deploy.sh" in s, "a site without deploy.sh must not fail"

    # upload: tar the folder locally, unpack it through the real script, compare
    import shutil
    assert upload_run({}, "no/such/folder", "/x", lambda l: None) == 2
    if not shutil.which("bash"):
        print("selftest ok (upload round-trip skipped: no bash)")
        return
    tmpd = Path(os.environ.get("TEMP", ".")) / "niyoj_upload_test"
    shutil.rmtree(tmpd, ignore_errors=True)
    dest = tmpd / "dest"
    (dest / "sub").mkdir(parents=True)
    (dest / "stale.html").write_text("old", encoding="utf-8")
    real, cwd = ssh_argv, os.getcwd()
    ssh_argv = lambda _s: ["bash", "-s"]        # what ssh does: script in on stdin
    log = []
    try:
        os.chdir(tmpd)                          # relative path: bash may be WSL or git-bash
        assert ssh_run({}, prep_script("dest"), log.append) == 0, "".join(log)
        (tmpd / "dest.new" / "index.html").write_text("new", encoding="utf-8")
        assert ssh_run({}, swap_script("dest"), log.append) == 0, "".join(log)
    finally:
        ssh_argv, _ = real, os.chdir(cwd)
    assert (dest / "index.html").read_text() == "new", "upload did not land"
    assert not (dest / "stale.html").exists(), "old files must not survive the swap"
    assert not (dest.parent / "dest.old").exists(), "swap left a stale .old behind"
    shutil.rmtree(tmpd, ignore_errors=True)

    assert remote_script({"cmd": "echo hi", "dir": "/o", "repo": "r"}) == "echo hi"
    a = ssh_argv({"host": "1.2.3.4", "user": "deploy", "port": 2222, "key": "~/k"})
    assert a[-1] == "bash -s" and a[-2] == "deploy@1.2.3.4"
    assert "2222" in a and "BatchMode=yes" in a
    # rename keeps position and survives a plain edit
    d = {"a": 1, "b": 2, "c": 3}
    assert list(rename_key(d, "b", "z", 9)) == ["a", "z", "c"]
    assert list(rename_key(d, "b", "b", 9)) == ["a", "b", "c"]
    assert rename_key(d, "", "new", 4)["new"] == 4 and len(rename_key(d, "", "new", 4)) == 4
    rows = parse_scan("noise\n###APPS###\n"
                      "a\t/opt/a\tr\tmain\t2026-01-01\t1\t0\t1\td.com\n"
                      "a\t/opt/a\tdup\tmain\t\t0\t0\t0\t\n"
                      "b\t/srv/b\t\t\t\t0\t0\t0\t\n")
    assert [r["name"] for r in rows] == ["a", "b"], "scan dedup broken"
    assert rows[0]["deploy_sh"] and rows[0]["up"] and rows[0]["domain"] == "d.com"
    assert rows[1]["branch"] == "main" and not rows[1]["compose"]
    assert parse_scan("no marker") == []
    assert (BUNDLE / "ui.html").exists(), "ui.html missing"
    for name, rx in _VER_RE.items():
        f = Path(__file__).resolve().parent / name
        if f.exists():
            assert rx.search(f.read_text(encoding="utf-8")),                 f"--bump would not find the version in {name}"
    assert next_version(1, 2, 9) == "1.2.10"
    assert next_version(1, 2, 9, "minor") == "1.3.0"
    assert next_version(1, 2, 9, "major") == "2.0.0"

    # a key line is single-quoted into a remote shell: nothing may escape it
    pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc+/de= niyoj"
    a = authz_script(pub)
    assert a.count("\n") == 0, "authz must stay one line for the Windows console hop"
    assert f"'{pub}'" in a and "NIYOJ_KEY_OK" in a
    for bad in ("ssh-ed25519 AAA'; rm -rf / #", "not-a-key AAAA", "ssh-ed25519 A#B c",
                'ssh-ed25519 AAAA "x"'):
        try:
            authz_script(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"authz_script accepted {bad!r}")
    if SCRIPTS_JSON.exists():        # the phone reads this copy — it must be current
        assert SCRIPTS_JSON.read_text(encoding="utf-8") == scripts_blob(),             "android scripts.json is stale — run: python deployer.py --scripts"
    assert password_argv({"host": "h", "port": 2222}, "x")[-1] == "x"
    assert "PubkeyAuthentication=no" in password_argv({"host": "h"}, "x")

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--scripts" in sys.argv:
        dump_scripts()
    elif "--bump" in sys.argv:
        rest = sys.argv[sys.argv.index("--bump") + 1:]
        bump(rest[0] if rest else "patch")
    else:
        main()
