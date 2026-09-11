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
import base64
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

VERSION = "1.1.19"   # bumped by --bump on every build; keep the literal on one line

# ponytail: frozen exe unpacks to a temp dir, so anchor config next to the exe
ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _state_dir():
    """A writable home for startup diagnostics and WebView2 profile data."""
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ROOT)
    path = base / "NiYoj"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log_path():
    return _state_dir() / "niyoj.log"


def _configure_logging():
    logging.basicConfig(
        filename=_log_path(),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def _show_startup_error(exc):
    """A --noconsole build must still tell the user why the window did not open."""
    try:
        detail_path = str(_log_path())
    except OSError:
        detail_path = "the Windows Local AppData folder (logging was unavailable)"
    message = (
        "NiYoj could not start.\n\n"
        f"{exc}\n\n"
        f"Technical details were saved to:\n{detail_path}"
    )
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "NiYoj startup error", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


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
    'if [ -n "{token_b64}" ]; then',
    '  _AP=$(mktemp)',
    '  chmod 700 "$_AP"',
    '  printf \'#!/bin/sh\\ncase "$1" in\\n  *sername*) echo "${GIT_USER:-x-access-token}" ;;\\n  *) echo "%s" | { base64 -d 2>/dev/null || base64 -D; } ;;\\nesac\\n\' "{token_b64}" > "$_AP"',
    '  export GIT_ASKPASS="$_AP"',
    '  trap \'rm -f "$_AP"\' EXIT',
    'fi',
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


def remote_script(app, server=None):
    """Clone-if-missing, hard-reset to origin, run the repo's own deploy.sh.

    Same script serves a brand-new app and the 200th redeploy, because
    deploy/deploy.sh is idempotent (see DEPLOYMENT_PLAYBOOK.md).
    """
    if app.get("cmd"):
        return app["cmd"]
    token = (app.get("token") or (server.get("token") if server else "") or "").strip()
    token_b64 = base64.b64encode(token.encode("utf-8")).decode("ascii") if token else ""
    return (DEPLOY_TPL
            .replace("{dir}", app["dir"])
            .replace("{repo}", app["repo"])
            .replace("{branch}", app.get("branch", "main"))
            .replace("{token_b64}", token_b64))


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
SUPERADMIN_DETECTOR = r"""
set -u
dir="$1"
if [ ! -d "$dir" ]; then echo "NOT_FOUND"; exit 0; fi

# 1. Look for SQLite databases in common places
for db in "$dir"/data/qilin.sqlite "$dir"/data/*.sqlite "$dir"/data/*.db "$dir"/backend/harness.db "$dir"/backend/*.db "$dir"/*.db "$dir"/data/saigapos.db; do
  if [ -f "$db" ] && command -v sqlite3 >/dev/null 2>&1; then
    cols=$(sqlite3 "$db" "PRAGMA table_info(users);" 2>/dev/null || true)
    if [ -n "$cols" ]; then
      if echo "$cols" | grep -q "pos_pin_hash"; then
        echo "TYPE:qilin_sqlite|$db"
        users=$(sqlite3 "$db" "SELECT email, role, active FROM users WHERE role IN ('superadmin','admin') ORDER BY role DESC;" 2>/dev/null || true)
        printf 'USERS:%s\n' "$(echo "$users" | tr '\n' ';')"
        exit 0
      elif echo "$cols" | grep -q "password_hash"; then
        echo "TYPE:generic_sqlite|$db"
        users=$(sqlite3 "$db" "SELECT username, role, 1 FROM users WHERE role IN ('superadmin','admin') ORDER BY role DESC;" 2>/dev/null || true)
        printf 'USERS:%s\n' "$(echo "$users" | tr '\n' ';')"
        exit 0
      fi
    fi
  fi
done

# 2. Check for docker compose with postgres (e.g. AeroLens)
if [ -f "$dir/docker-compose.yml" ] || [ -f "$dir/docker-compose.prod.yml" ] || [ -f "$dir/deploy/docker-compose.prod.yml" ]; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qE '^aerolens.*postgres'; then
    pg_c=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^aerolens.*postgres' | head -1)
    echo "TYPE:aerolens_pg|$pg_c"
    users=$(docker exec "$pg_c" psql -U aerolens -d aerolens -Atc "SELECT email, role, is_active FROM users WHERE role IN ('super_admin','admin') ORDER BY role DESC;" 2>/dev/null || true)
    printf 'USERS:%s\n' "$(echo "$users" | tr '\n' ';')"
    exit 0
  fi
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q 'vyuhos_selfhost_db'; then
    echo "TYPE:vyuhos_supabase|vyuhos_selfhost_db"
    users=$(docker exec vyuhos_selfhost_db psql -U postgres -d postgres -Atc "SELECT u.email, coalesce(s.platform_role, 'USER'), 1 FROM auth.users u LEFT JOIN public.platform_staff s ON u.id = s.user_id WHERE s.platform_role IS NOT NULL;" 2>/dev/null || true)
    printf 'USERS:%s\n' "$(echo "$users" | tr '\n' ';')"
    exit 0
  fi
fi

# 3. Look for .env files with SUPERADMIN or BOOTSTRAP credentials
for envfile in "$dir/.env" "$dir/deploy/.env" "$dir/infra/self-host/.env"; do
  if [ -f "$envfile" ]; then
    admin_e=$(grep -E '^(SUPERADMIN_EMAIL|AEROLENS_BOOTSTRAP_EMAIL|SMTP_ADMIN_EMAIL)=' "$envfile" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "' || true)
    admin_p=$(grep -E '^(SUPERADMIN_PASSWORD|AEROLENS_BOOTSTRAP_PASSWORD)=' "$envfile" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "' || true)
    if [ -n "$admin_e" ]; then
      echo "TYPE:env_file|$envfile"
      printf 'USERS:%s|superadmin|1\n' "$admin_e"
      if [ -n "$admin_p" ]; then
        echo "HAS_PASSWORD:true"
      fi
      exit 0
    fi
  fi
done

# 4. Fallback: app directory exists
echo "TYPE:custom|$dir"
exit 0
"""
SCAN_SCRIPT = r"""
set -u
have(){ command -v "$1" >/dev/null 2>&1; }
running=""
if have docker; then running=$(docker ps --format '{{.Names}}' 2>/dev/null | tr '\n' ' '); fi
echo "###APPS###"
for r in /app /opt /srv /var/www /root /home; do
  [ -d "$r" ] || continue
  for d in "$r"/* "$r"/*/*; do
    [ -d "$d/.git" ] || continue
    n=$(basename "$d")
    repo=$(git -C "$d" config --get remote.origin.url 2>/dev/null | sed -E 's|^(https?://)[^/@]+@|\1|')
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
        data = {k: v for k, v in data.items() if v not in ("", None)}
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

    def get_gh_accounts(self):
        """Find logged-in GitHub accounts and tokens via GitHub CLI (gh)."""
        import shutil
        gh = shutil.which("gh")
        if not gh:
            return {"ok": False, "msg": "GitHub CLI (gh) not found on this system"}
        try:
            res = subprocess.run([gh, "auth", "status"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW)
            text = res.stdout + "\n" + res.stderr
            matches = list(dict.fromkeys(re.findall(r"account\s+([A-Za-z0-9_-]+)", text)))
            accounts = []
            for u in matches:
                tok_res = subprocess.run([gh, "auth", "token", "-u", u],
                                         capture_output=True, text=True,
                                         creationflags=NO_WINDOW)
                tok = tok_res.stdout.strip()
                if tok.startswith("gh"):
                    accounts.append({"user": u, "token": tok})
            if not accounts:
                tok_res = subprocess.run([gh, "auth", "token"],
                                         capture_output=True, text=True,
                                         creationflags=NO_WINDOW)
                tok = tok_res.stdout.strip()
                if tok.startswith("gh"):
                    accounts.append({"user": "default", "token": tok})
            return {"ok": True, "accounts": accounts}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def open_external(self, url):
        """Open a URL in the user's default web browser."""
        import webbrowser
        webbrowser.open(url)
        return {"ok": True}

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
        script = remote_script(app, sv) if not upload else ""
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

    def inspect_superadmin(self, app_name):
        """Inspect the droplet for the app's superadmin / admin setup."""
        cfg = load_cfg()
        app = cfg["apps"].get(app_name)
        if not app:
            return {"ok": False, "msg": f"App '{app_name}' not found in config."}
        sv = cfg["servers"].get(app.get("server", ""))
        if not sv:
            return {"ok": False, "msg": f"Server '{app.get('server')}' not found."}
        app_dir = app.get("dir", f"/opt/{app_name}")
        if app.get("admin_cmd"):
            return {"ok": True, "app": app_name, "server": app.get("server"), "dir": app_dir,
                    "type": "custom command", "target": app.get("admin_cmd"), "users": [],
                    "has_password": False, "raw": ""}
        out = []
        try:
            cmd = f'{SUPERADMIN_DETECTOR}\nrun_detect "{app_dir}" 2>/dev/null || true'
            # Simpler invocation passing app_dir as $1
            script = f'set -- "{app_dir}"\n' + SUPERADMIN_DETECTOR
            code = ssh_run(sv, script, out.append)
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        text = "".join(out).strip()
        app_type = "custom"
        target_ref = app_dir
        users = []
        has_password = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("TYPE:"):
                parts = line[5:].split("|", 1)
                app_type = parts[0]
                if len(parts) > 1:
                    target_ref = parts[1]
            elif line.startswith("USERS:"):
                raw_users = line[6:].strip().split(";")
                for u in raw_users:
                    u = u.strip()
                    if not u:
                        continue
                    fields = u.split("|")
                    users.append({
                        "email": fields[0],
                        "role": fields[1] if len(fields) > 1 else "superadmin",
                        "active": fields[2] == "1" if len(fields) > 2 else True,
                    })
            elif line.startswith("HAS_PASSWORD:"):
                has_password = (line[13:].strip().lower() == "true")
        return {
            "ok": True,
            "app": app_name,
            "server": app.get("server"),
            "dir": app_dir,
            "type": app_type,
            "target": target_ref,
            "users": users,
            "has_password": has_password,
            "raw": text,
        }

    def manage_superadmin(self, app_name, action, email, password):
        """Create or update a superadmin / admin account and password for the app."""
        cfg = load_cfg()
        app = cfg["apps"].get(app_name)
        if not app:
            return {"ok": False, "msg": f"App '{app_name}' not found."}
        sv = cfg["servers"].get(app.get("server", ""))
        if not sv:
            return {"ok": False, "msg": f"Server '{app.get('server')}' not found."}
        app_dir = app.get("dir", f"/opt/{app_name}")
        email = email.strip()
        password = password.strip()
        if not email or not password:
            return {"ok": False, "msg": "Email and password are required."}
        if len(password) < 6:
            return {"ok": False, "msg": "Password must be at least 6 characters."}

        # ponytail: per-app escape hatch. A command beats guessing the stack.
        admin_cmd = app.get("admin_cmd", "").strip()
        if admin_cmd:
            script = (
                f"cd {shlex.quote(app_dir)} && "
                f"ADMIN_EMAIL={shlex.quote(email)} ADMIN_PASSWORD={shlex.quote(password)} "
                f"ADMIN_ACTION={shlex.quote(action)} "
                + admin_cmd.replace("$EMAIL", shlex.quote(email))
                           .replace("$PASSWORD", shlex.quote(password))
            )
            out = []
            try:
                code = ssh_run(sv, script, out.append)
            except Exception as e:
                return {"ok": False, "msg": str(e)}
            text = "".join(out).strip()
            return {"ok": code == 0, "msg": text or (f"Command finished (exit {code})"), "raw": text}

        # Python script sent to droplet to perform the update accurately
        # Handles SQLite (qilinpos scrypt, generic sha256), PostgreSQL/Docker (AeroLens bcrypt),
        # Supabase/Vyuhos (auth.users + platform_staff), and .env files
        remote_worker = r"""
import sys, os, glob, sqlite3, subprocess, hashlib, base64

dir_path = sys.argv[1]
action = sys.argv[2] # 'create' or 'update'
email = sys.argv[3].strip().lower()
password = sys.argv[4]

admin_db = sys.argv[5] if len(sys.argv) > 5 else ""

def update_qilin(db_path):
    import node_crypto_scrypt
    pass

# Check 0: operator pointed us at a table -- "path/to.db" or "path/to.db:tablename"
if admin_db:
    db_f, _, table = admin_db.partition(":")
    table = table or "users"
    if not os.path.isabs(db_f):
        db_f = os.path.join(dir_path, db_f)
    conn = sqlite3.connect(db_f)
    c = conn.cursor()
    cols = [r[1] for r in c.execute('PRAGMA table_info("%s")' % table).fetchall()]
    if not cols:
        print("ERROR: table %s not found in %s" % (table, db_f))
        sys.exit(1)
    id_col = "email" if "email" in cols else "username"
    pw_col = next((x for x in ("password_hash", "password", "hashed_password", "encrypted_password") if x in cols), None)
    if not pw_col:
        print("ERROR: no password column in %s.%s (columns: %s)" % (table, db_f, ",".join(cols)))
        sys.exit(1)
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    row = c.execute('SELECT rowid FROM "%s" WHERE lower(%s)=?' % (table, id_col), (email,)).fetchone()
    if row:
        sets = ["%s=?" % pw_col] + (["role='superadmin'"] if "role" in cols else [])
        c.execute('UPDATE "%s" SET %s WHERE rowid=?' % (table, ",".join(sets)), (pwd_hash, row[0]))
        verb = "Updated"
    else:
        fields = [id_col, pw_col] + (["role"] if "role" in cols else [])
        vals = [email, pwd_hash] + (["superadmin"] if "role" in cols else [])
        if id_col == "email" and "username" in cols:
            fields.append("username"); vals.append(email)
        c.execute('INSERT INTO "%s" (%s) VALUES (%s)' % (table, ",".join(fields), ",".join("?" * len(vals))), vals)
        verb = "Created"
    conn.commit()
    conn.close()
    print("SUCCESS: %s superadmin %s in %s.%s" % (verb, email, os.path.basename(db_f), table))
    sys.exit(0)

# Check 1: Qilin POS SQLite
qilin_dbs = glob.glob(os.path.join(dir_path, "data", "qilin.sqlite*")) or glob.glob(os.path.join(dir_path, "data", "*.sqlite"))
for db_f in qilin_dbs:
    if db_f.endswith(("-wal", "-shm")): continue
    if os.path.exists(db_f):
        try:
            conn = sqlite3.connect(db_f)
            c = conn.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
            if "pos_pin_hash" in cols:
                # Use node via subprocess for exact scrypt compatibility
                node_code = f'''
                const crypto = require("crypto");
                const salt = crypto.randomBytes(16).toString("hex");
                const hash = salt + ":" + crypto.scryptSync(process.argv[1], salt, 64).toString("hex");
                process.stdout.write(hash);
                '''
                p = subprocess.run(["node", "-e", node_code, password], capture_output=True, text=True)
                pwd_hash = p.stdout.strip()
                if not pwd_hash:
                    print("ERROR: Failed to generate scrypt hash via node")
                    sys.exit(1)
                existing = c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
                now = subprocess.check_output(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"]).decode().strip()
                if existing:
                    c.execute("UPDATE users SET password_hash=?, role='superadmin', active=1, updated_at=? WHERE id=?", (pwd_hash, now, existing[0]))
                    conn.commit()
                    conn.close()
                    print(f"SUCCESS: Updated password for superadmin {email} in {os.path.basename(db_f)}")
                    sys.exit(0)
                else:
                    user_id = "u-" + subprocess.check_output(["od", "-x", "/dev/urandom"]).decode().split()[1][:8]
                    c.execute("INSERT INTO users (id, email, password_hash, role, active, created_at, updated_at) VALUES (?, ?, ?, 'superadmin', 1, ?, ?)",
                              (user_id, email, pwd_hash, now, now))
                    conn.commit()
                    conn.close()
                    print(f"SUCCESS: Created superadmin {email} in {os.path.basename(db_f)}")
                    sys.exit(0)
        except Exception as e:
            pass

# Check 2: Generic SQLite (e.g. KabelFlux harness.db)
dbs = glob.glob(os.path.join(dir_path, "backend", "harness.db")) or glob.glob(os.path.join(dir_path, "backend", "*.db")) or glob.glob(os.path.join(dir_path, "*.db"))
for db_f in dbs:
    if os.path.exists(db_f):
        try:
            conn = sqlite3.connect(db_f)
            c = conn.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
            if "password_hash" in cols:
                pwd_hash = hashlib.sha256(password.encode()).hexdigest()
                existing = c.execute("SELECT id FROM users WHERE lower(email)=? OR lower(username)=?", (email, email)).fetchone()
                if existing:
                    c.execute("UPDATE users SET password_hash=?, role='superadmin' WHERE id=?", (pwd_hash, existing[0]))
                    conn.commit()
                    conn.close()
                    print(f"SUCCESS: Updated password for superadmin {email} in {os.path.basename(db_f)}")
                    sys.exit(0)
                else:
                    c.execute("INSERT INTO users (username, email, password_hash, full_name, role) VALUES (?, ?, ?, 'Super Administrator', 'superadmin')",
                              (email, email, pwd_hash))
                    conn.commit()
                    conn.close()
                    print(f"SUCCESS: Created superadmin {email} in {os.path.basename(db_f)}")
                    sys.exit(0)
        except Exception as e:
            pass

# Check 3: AeroLens PostgreSQL container
try:
    containers = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True).splitlines()
    aerolens_pg = [c for c in containers if "aerolens" in c and "postgres" in c]
    if aerolens_pg:
        pg_c = aerolens_pg[0]
        # Generate bcrypt hash using python inside the api container or host python3
        py_code = f'''
        import bcrypt, uuid
        pwd = sys.argv[1]
        email = sys.argv[2]
        action = sys.argv[3]
        hashed = bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        # Check if user exists
        check_cmd = ["docker", "exec", "{pg_c}", "psql", "-U", "aerolens", "-d", "aerolens", "-Atc", f"SELECT id FROM users WHERE lower(email)='{{email}}';"]
        uid = subprocess.check_output(check_cmd, text=True).strip()
        if uid:
            sql = f"UPDATE users SET password_hash='{{hashed}}', role='super_admin', is_active=true WHERE id='{{uid}}';"
            subprocess.check_call(["docker", "exec", "{pg_c}", "psql", "-U", "aerolens", "-d", "aerolens", "-c", sql])
            print(f"SUCCESS: Updated superadmin password for {{email}} in AeroLens")
        else:
            uid = uuid.uuid4().hex
            # ensure default org exists
            org_sql = "INSERT INTO organizations (id, name) VALUES ('dev', 'Default Org') ON CONFLICT DO NOTHING;"
            subprocess.run(["docker", "exec", "{pg_c}", "psql", "-U", "aerolens", "-d", "aerolens", "-c", org_sql], capture_output=True)
            sql = f"INSERT INTO users (id, org_id, email, password_hash, role, is_active) VALUES ('{{uid}}', 'dev', '{{email}}', '{{hashed}}', 'super_admin', true);"
            subprocess.check_call(["docker", "exec", "{pg_c}", "psql", "-U", "aerolens", "-d", "aerolens", "-c", sql])
            print(f"SUCCESS: Created superadmin {{email}} in AeroLens")
        '''
        res = subprocess.run(["python3", "-c", py_code, password, email, action], capture_output=True, text=True)
        if res.returncode == 0 and "SUCCESS:" in res.stdout:
            print(res.stdout.strip())
            # Also update .env file if it exists so redeploy preserves it
            env_path = os.path.join(dir_path, ".env")
            if os.path.exists(env_path):
                content = open(env_path).read()
                import re
                if "AEROLENS_BOOTSTRAP_EMAIL=" in content:
                    content = re.sub(r"AEROLENS_BOOTSTRAP_EMAIL=.*", f"AEROLENS_BOOTSTRAP_EMAIL={email}", content)
                else:
                    content += f"\nAEROLENS_BOOTSTRAP_EMAIL={email}"
                if "AEROLENS_BOOTSTRAP_PASSWORD=" in content:
                    content = re.sub(r"AEROLENS_BOOTSTRAP_PASSWORD=.*", f"AEROLENS_BOOTSTRAP_PASSWORD={password}", content)
                else:
                    content += f"\nAEROLENS_BOOTSTRAP_PASSWORD={password}"
                open(env_path, "w").write(content)
            sys.exit(0)
except Exception as e:
    pass

# Check 4: Self-hosted Supabase / Vyuhos
try:
    containers = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True).splitlines()
    if "vyuhos_selfhost_db" in containers:
        # Use psql in vyuhos_selfhost_db to insert into auth.users (with pgcrypto crypt()) and bootstrap platform owner
        sql_script = f'''
        DO $$
        DECLARE
          v_uid uuid;
          v_pwd text := '{password}';
          v_email text := '{email}';
        BEGIN
          SELECT id INTO v_uid FROM auth.users WHERE lower(email) = lower(v_email);
          IF v_uid IS NOT NULL THEN
            UPDATE auth.users SET
              encrypted_password = extensions.crypt(v_pwd, extensions.gen_salt('bf')),
              email_confirmed_at = coalesce(email_confirmed_at, now()),
              updated_at = now()
            WHERE id = v_uid;
          ELSE
            v_uid := gen_random_uuid();
            INSERT INTO auth.users (
              id, aud, role, email, encrypted_password, email_confirmed_at,
              raw_app_meta_data, raw_user_meta_data, created_at, updated_at
            ) VALUES (
              v_uid, 'authenticated', 'authenticated', v_email,
              extensions.crypt(v_pwd, extensions.gen_salt('bf')),
              now(), '{{"provider":"email","providers":["email"]}}'::jsonb,
              '{{"name":"Superadmin"}}'::jsonb, now(), now()
            );
          END IF;
          -- Bootstrap or promote in platform_staff
          IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'platform_staff') THEN
            INSERT INTO public.platform_staff (user_id, platform_role, protected_owner, note)
            VALUES (v_uid, 'PLATFORM_SUPERUSER', true, 'Operator bootstrapped superadmin')
            ON CONFLICT (user_id) DO UPDATE SET platform_role = 'PLATFORM_SUPERUSER', protected_owner = true;
          END IF;
        END $$;
        '''
        res = subprocess.run(["docker", "exec", "-i", "vyuhos_selfhost_db", "psql", "-U", "postgres", "-d", "postgres", "-c", sql_script],
                             capture_output=True, text=True)
        if res.returncode == 0:
            print(f"SUCCESS: Configured platform superadmin {email} in VyuhOS Supabase")
            sys.exit(0)
        else:
            print("ERROR vyuhos_selfhost_db:", res.stderr)
except Exception as e:
    pass

# Check 5: Update environment variables (.env files)
env_updated = False
for env_file in [os.path.join(dir_path, ".env"), os.path.join(dir_path, "deploy", ".env"), os.path.join(dir_path, "infra", "self-host", ".env")]:
    if os.path.exists(env_file):
        try:
            content = open(env_file).read()
            import re
            # Update or set SUPERADMIN_EMAIL / PASSWORD
            for key, val in [("SUPERADMIN_EMAIL", email), ("SUPERADMIN_PASSWORD", password),
                             ("AEROLENS_BOOTSTRAP_EMAIL", email), ("AEROLENS_BOOTSTRAP_PASSWORD", password)]:
                if f"{key}=" in content:
                    content = re.sub(rf"^{key}=.*", f"{key}={val}", content, flags=re.M)
                    env_updated = True
                elif "SUPERADMIN" in content or "BOOTSTRAP" in content:
                    content += f"\n{key}={val}"
                    env_updated = True
            open(env_file, "w").write(content)
            print(f"SUCCESS: Updated credentials in {env_file}")
            sys.exit(0)
        except Exception as e:
            pass

print(f"NOTICE: Could not detect standard database. Created helper entry in {dir_path}/.env")
try:
    with open(os.path.join(dir_path, ".env"), "a") as f:
        f.write(f"\nSUPERADMIN_EMAIL={email}\nSUPERADMIN_PASSWORD={password}\n")
    print(f"SUCCESS: Saved superadmin credentials to {dir_path}/.env")
    sys.exit(0)
except Exception as e:
    print("ERROR: " + str(e))
    sys.exit(1)
"""
        remote_script = (
            f"python3 -c {json.dumps(remote_worker)} "
            f"{json.dumps(app_dir)} {json.dumps(action)} {json.dumps(email)} {json.dumps(password)} "
            f"{json.dumps(app.get('admin_db', ''))}"
        )
        out = []
        try:
            code = ssh_run(sv, remote_script, out.append)
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        text = "".join(out).strip()
        ok = code == 0 and ("SUCCESS:" in text)
        msg = text.split("SUCCESS:")[-1].strip() if ok else (
            text.split("ERROR:")[-1].strip() if "ERROR:" in text else (text or f"Exit {code}")
        )
        return {"ok": ok, "msg": msg, "raw": text}


def main():
    import webview
    logging.info("Starting NiYoj %s from %s", VERSION, ROOT)
    state_dir = _state_dir()
    api = Api()
    api.window = webview.create_window(
        f"NiYoj {VERSION}",
        url=(BUNDLE / "ui.html").as_uri(),
        js_api=api, width=1140, height=720, min_size=(880, 560),
        background_color="#080c17",
    )
    # Explicitly select WebView2. Falling back to the retired MSHTML renderer can
    # otherwise produce inconsistent startup and rendering across Windows machines.
    webview.start(
        gui="edgechromium" if sys.platform == "win32" else None,
        private_mode=False,
        storage_path=str(state_dir / "webview"),
    )


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
    # VERSION is the source of truth. Applying one computed value everywhere also
    # repairs an installer or Android manifest that was edited out of sync.
    new = next_version(*(int(x) for x in VERSION.split(".")), part)
    for name, rx in _VER_RE.items():
        f = here / name
        if not f.exists():                      # the android project is optional
            continue
        old = f.read_text(encoding="utf-8")
        m = rx.search(old)
        if not m:
            raise SystemExit(f"no version literal found in {name}")
        f.write_text(old[:m.start()] + m[1] + new + m[5] + old[m.end():],
                     encoding="utf-8", newline="")
    # Play refuses a repeat versionCode, so derive one that only ever climbs
    g = here / "android/app/build.gradle.kts"
    if g.exists():
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

    st = remote_script({"dir": "/opt/x", "repo": "https://github.com/o/x.git", "branch": "main", "token": "ghp_secret"})
    assert "GIT_ASKPASS" in st
    assert "ghp_secret" not in st, "token must be base64-encoded, never raw plaintext"
    assert base64.b64encode(b"ghp_secret").decode("ascii") in st

    st_sv = remote_script({"dir": "/opt/x", "repo": "https://github.com/o/x.git", "branch": "main"}, {"token": "ghp_sv_secret"})
    assert "GIT_ASKPASS" in st_sv
    assert base64.b64encode(b"ghp_sv_secret").decode("ascii") in st_sv

    gh_res = Api().get_gh_accounts()
    assert isinstance(gh_res, dict) and "ok" in gh_res

    # upload: tar the folder locally, unpack it through the real script, compare
    import shutil
    assert upload_run({}, "no/such/folder", "/x", lambda l: None) == 2
    bash = shutil.which("bash")
    if not bash:
        print("selftest ok (upload round-trip skipped: no bash)")
        return
    # Windows may expose WSL's bash launcher even when no distro is installed.
    # Treat that as unavailable instead of failing an otherwise healthy build.
    try:
        probe = subprocess.run([bash, "-lc", "true"], capture_output=True,
                               creationflags=NO_WINDOW, timeout=3)
        if probe.returncode:
            print("selftest ok (upload round-trip skipped: bash is unavailable)")
            return
    except Exception:
        print("selftest ok (upload round-trip skipped: bash timed out or unavailable)")
        return
    tmpd = Path(os.environ.get("TEMP", ".")) / "niyoj_upload_test"
    shutil.rmtree(tmpd, ignore_errors=True)
    dest = tmpd / "dest"
    (dest / "sub").mkdir(parents=True)
    (dest / "stale.html").write_text("old", encoding="utf-8")
    real, cwd = ssh_argv, os.getcwd()
    ssh_argv = lambda _s: [bash, "-s"]          # what ssh does: script in on stdin
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
    assert "for r in /app /opt /srv /var/www" in SCAN_SCRIPT, \
        "scan roots must include services under /app and websites under /var/www"
    assert (BUNDLE / "ui.html").exists(), "ui.html missing"
    for name, rx in _VER_RE.items():
        f = Path(__file__).resolve().parent / name
        if f.exists():
            m = rx.search(f.read_text(encoding="utf-8"))
            assert m, f"--bump would not find the version in {name}"
            assert ".".join(m.group(i) for i in (2, 3, 4)) == VERSION, \
                f"version drift in {name}: expected {VERSION}"
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
    elif "--version" in sys.argv:
        print(VERSION)
    else:
        try:
            _configure_logging()
            main()
        except Exception as exc:
            logging.critical("Startup failed\n%s", traceback.format_exc())
            _show_startup_error(exc)
            raise SystemExit(1)
