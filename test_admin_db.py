"""ponytail: one check for the operator-pointed sqlite branch (Check 0) of the remote worker."""
import io, re, sqlite3, subprocess, sys, tempfile, os

src = io.open("deployer.py", encoding="utf-8").read()
worker = re.search(r'remote_worker = r"""(.*?)\n"""', src, re.S).group(1)

def run(db, table, email="a@b.c", pw="secret1"):
    return subprocess.run([sys.executable, "-c", worker, os.path.dirname(db), "create", email, pw,
                           os.path.basename(db) + ":" + table], capture_output=True, text=True)

d = tempfile.mkdtemp()
db = os.path.join(d, "app.sqlite")
c = sqlite3.connect(db)
c.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, role TEXT)")
c.commit()

r = run(db, "accounts")
assert "SUCCESS:" in r.stdout, r.stdout + r.stderr
row = c.execute("SELECT email, role, password_hash FROM accounts").fetchone()
assert row[0] == "a@b.c" and row[1] == "superadmin" and len(row[2]) == 64, row

first = row[2]
r = run(db, "accounts", pw="other1")                      # update path, no duplicate row
assert "SUCCESS:" in r.stdout, r.stdout + r.stderr
rows = c.execute("SELECT password_hash FROM accounts").fetchall()
assert len(rows) == 1 and rows[0][0] != first, rows

r = run(db, "nope")                                       # missing table -> clean error
assert r.returncode == 1 and "ERROR:" in r.stdout, r.stdout
print("ok")
