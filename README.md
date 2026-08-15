<p align="center">
  <img src="logo.png" width="88" alt="NiYoj">
</p>

<h1 align="center">NiYoj</h1>

<p align="center">
  A small desktop console for deploying your apps to your own servers over SSH.<br>
  No CI minutes, no runners, no YAML. Pick an app, press Deploy, watch the log.
</p>

---

## What it does

You keep a handful of apps on one or more VPS boxes (DigitalOcean, Hetzner, Vultr, a
Raspberry Pi — anything you can SSH into). NiYoj gives you one window over all of them:

- **Servers down the side**, apps as **cards** — each showing branch, path, repo and
  whether its last deploy passed
- **Deploy** runs over SSH from your own key. Output streams into a log popup you can
  close and reopen; the deploy keeps running
- **Scan for apps** sniffs a server for git checkouts and fills in the details for you,
  so an existing box is set up in one click
- **Test connection** from inside the server form, before you save it
- Everything configured through the UI — you never hand-edit a config file

The deploy itself is deliberately dumb, and that is the point:

```sh
git clone -b <branch> <repo> <dir>     # only if it isn't there yet
cd <dir>
git fetch --all --prune
git reset --hard origin/<branch>
sudo bash deploy/deploy.sh             # your repo's own script does the real work
```

A brand-new app and the 200th redeploy take the identical path, because your
`deploy/deploy.sh` is idempotent. Anything unusual gets a **custom command** per app.

---

## Install

You need **Python 3.9+** and an **`ssh` client** on your machine.

```sh
git clone https://github.com/bodish-coder/niyoj
cd niyoj
pip install pywebview        # see per-platform notes below
python3 deployer.py
```

### Windows
Nothing extra — the window uses the Edge **WebView2** runtime, which ships with
Windows 11 (Windows 10: install the
[Evergreen runtime](https://developer.microsoft.com/microsoft-edge/webview2/)).
`ssh.exe` is built into Windows 10/11.

```powershell
pip install pywebview
python deployer.py
```

### macOS
The window uses the system WebKit — no extra runtime.

```sh
pip3 install pywebview
python3 deployer.py
```

### Linux
pywebview needs a GTK or Qt backend. On Debian/Ubuntu:

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
pip install "pywebview[gtk]"
python3 deployer.py
```

Fedora: `sudo dnf install python3-gobject gtk3 webkit2gtk4.1`.
Prefer Qt? `pip install "pywebview[qt]"` instead, no system packages needed on most
distros.

---

## Build a standalone binary (optional)

Only if you want a double-clickable app instead of `python3 deployer.py`.

```sh
pip install pyinstaller
./build.sh          # macOS / Linux  -> ./niyoj
build.cmd           # Windows        -> niyoj.exe
```

Binaries are per-platform — build on the OS you intend to run on. Keep `apps.json`
in the same folder as the binary.

---

## First run

1. **+ Server** — name, host/IP, SSH user, port, private key. Hit **Test connection**
   before saving; it reports the box's kernel and git version, or the actual SSH error.
2. **Scan for apps** — walks `/opt`, `/srv`, `/var/www`, `/root` and `/home` for git
   checkouts and shows what it found: repo, branch, last commit, whether
   `deploy/deploy.sh` exists, whether a container is running, and the nginx domain.
   Tick what you want and **Import**.
3. Anything not on the box yet: **+ App**, give it a repo — the first Deploy clones it.

### SSH keys

Use a key **without a passphrase**. NiYoj runs SSH with `BatchMode=yes` so a deploy can
never hang waiting on a prompt — which also means it cannot answer a passphrase or
password. (A passphrase key works if you keep it loaded in `ssh-agent`.)

```sh
ssh-keygen -t ed25519 -f ~/.ssh/niyoj -C niyoj     # press Enter twice
ssh-copy-id -i ~/.ssh/niyoj.pub root@YOUR_IP       # Windows: see below
ssh -i ~/.ssh/niyoj root@YOUR_IP "echo connected"
```

Windows has no `ssh-copy-id`; do the same thing with:

```powershell
type "$HOME\.ssh\niyoj.pub" | ssh root@YOUR_IP "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Deploying as a non-root user? `deploy.sh` calls `sudo`, and BatchMode cannot type a
password, so give that user passwordless sudo:

```sh
echo "deploy ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/deploy
```

---

## Your data

Servers, hosts and key paths are stored in **`apps.json` beside the program** — local
only, never uploaded anywhere, and gitignored. Private keys themselves are never read
or copied by NiYoj; it only passes the path to `ssh -i`.

---

## Files

| File | |
|---|---|
| `deployer.py` | Everything: SSH, deploy, scanner, config API |
| `ui.html` | The whole interface — layout, CSS, JS |
| `apps.json` | Your servers and apps (created on first run, gitignored) |
| `build.cmd` / `build.sh` | Build a binary for Windows / macOS / Linux |

Run the self-check with `python3 deployer.py --selftest`.

## Licence

[GNU AGPL-3.0-or-later](LICENSE). Use it, change it, run it — but anything you
distribute or host built on this code must ship its complete source under the
same licence. No closed-source forks, no proprietary rebrands.
