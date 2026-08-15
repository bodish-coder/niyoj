#!/usr/bin/env python3
"""NiYoj — pick an app, it SSHes to its server and runs the deploy.

Run:  python deployer.py          (or deployer.exe)
      python deployer.py --selftest

Native WebView2 window (pywebview). Everything is configured in the UI;
apps.json next to the exe is just where it lands.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

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
def remote_script(app):
    """Clone-if-missing, hard-reset to origin, run the repo's own deploy.sh.

    Same script serves a brand-new app and the 200th redeploy, because
    deploy/deploy.sh is idempotent (see DEPLOYMENT_PLAYBOOK.md).
    """
    if app.get("cmd"):
        return app["cmd"]
    d, repo, br = app["dir"], app["repo"], app.get("branch", "main")
    return "\n".join([
        "set -euo pipefail",
        f"if [ ! -d {d}/.git ]; then git clone -b {br} {repo} {d}; fi",
        f"cd {d}",
        "git fetch --all --prune",
        f"git reset --hard origin/{br}",
        "git submodule update --init --recursive || true",
        # a static site checkout has no deploy.sh — the pull above is the whole deploy
        "if [ -f deploy/deploy.sh ]; then sudo bash deploy/deploy.sh;"
        " else echo 'no deploy/deploy.sh — files updated in place'; fi",
    ])


def prep_script(dest):
    return f"set -e; rm -rf {dest}.new {dest}.old; mkdir -p {dest}.new"


def swap_script(dest):
    """Only swap once the upload landed, so a half-sent site never goes live."""
    return "\n".join([
        "set -e",
        f"if [ -d {dest} ]; then mv {dest} {dest}.old; fi",
        f"mv {dest}.new {dest}",
        f"rm -rf {dest}.old",
        f"echo uploaded to {dest}",
    ])


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
    ds=0; [ -f "$d/deploy/deploy.sh" ] && ds=1
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


# ── bridge exposed to the UI ────────────────────────────────────────
class Api:
    def __init__(self):
        self.window = None
        self.busy = set()

    # -- read/write config
    def get_state(self):
        return load_cfg()

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

    # -- actions
    def test_conn(self, server, name=None):
        """Test a server dict straight from the form — no save needed first.
        If it belongs to a saved server, remember whether it answered."""
        if not server or not server.get("host"):
            return {"ok": False, "msg": "No host given"}
        out = []
        probe = ("uname -sr; command -v git >/dev/null && git --version | head -1 "
                 "|| echo 'git MISSING'")
        try:
            code = ssh_run(server, probe, out.append)
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
        "NiYoj",
        html=(BUNDLE / "ui.html").read_text(encoding="utf-8"),
        js_api=api, width=1140, height=720, min_size=(880, 560),
        background_color="#0b0f16",
    )
    webview.start()


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
    assert "if [ -f deploy/deploy.sh ]" in s, "a site without deploy.sh must not fail"

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
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
