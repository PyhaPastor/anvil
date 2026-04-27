# Adding Wordlists to Anvil

## Where wordlists are stored

Configured in `server/config.toml`:

```toml
[storage]
wordlists_dir = "./data/wordlists"   # relative to server/
```

Default path: `server/data/wordlists/`

You can change this to an absolute path (recommended in production):

```toml
wordlists_dir = "/opt/anvil/wordlists"
```

---

## Method 1 — Web UI (recommended)

Go to **Wordlists** in the sidebar → **Upload wordlist** or **Import from URL**.

Supports files up to 500 MB by default (change `max_upload_bytes` in `config.toml`).

---

## Method 2 — Manual (large files / bulk import)

Dropping a file into the directory isn't enough — Anvil also needs a database record
with the line count, size, name, and category. Use the helper script below.

### Step 1 — Copy files into the wordlists directory

```bash
cp rockyou.txt         /opt/anvil/server/data/wordlists/
cp weakpass_3.txt      /opt/anvil/server/data/wordlists/
cp dutch_wordlist.txt  /opt/anvil/server/data/wordlists/
```

### Step 2 — Register them in the database

You can run this from anywhere — the script finds `anvil.db` automatically:

```bash
/opt/anvil/server/venv/bin/python3 /opt/anvil/server/scripts/import_wordlists.py \
    /opt/anvil/server/data/wordlists/rockyou.txt \
    --name "RockYou 2021" --category "Common"
```

Or register an entire directory at once:

```bash
/opt/anvil/server/venv/bin/python3 /opt/anvil/server/scripts/import_wordlists.py \
    /opt/anvil/server/data/wordlists/ \
    --category "Common"
```

If the DB isn't found automatically (e.g. non-standard install), pass it explicitly:

```bash
/opt/anvil/server/venv/bin/python3 /opt/anvil/server/scripts/import_wordlists.py \
    /opt/anvil/server/data/wordlists/rockyou.txt \
    --db /opt/anvil/server/anvil.db \
    --name "RockYou 2021" --category "Common"
```

Run with `--help` for all options:

```bash
/opt/anvil/server/venv/bin/python3 /opt/anvil/server/scripts/import_wordlists.py --help
```

---

## Method 3 — SQLite direct insert (advanced / last resort)

If the script isn't available, you can insert manually:

```bash
sqlite3 /opt/anvil/server/anvil.db
```

```sql
INSERT INTO wordlists (name, file_path, file_size_bytes, line_count, category, uploaded_at)
VALUES (
  'RockYou 2021',
  '/opt/anvil/wordlists/rockyou.txt',
  14344392,          -- file size in bytes  (stat -c%s rockyou.txt)
  14344391,          -- line count          (wc -l < rockyou.txt)
  'Common',
  datetime('now')
);
```

Get the values quickly on Linux:
```bash
stat -c%s rockyou.txt      # file size
wc -l < rockyou.txt        # line count
```

---

## Recommended wordlist layout

```
data/wordlists/
├── common/
│   ├── rockyou.txt
│   └── weakpass_3.txt
├── dutch/
│   └── dutch_wordlist.txt
└── targeted/
    └── company_names.txt
```

Anvil doesn't require subdirectories — files can sit flat in the directory.

---

## Checking it worked

After importing, go to **Wordlists** in the UI. The new wordlist should appear with its
line count and file size. If it doesn't, check that:

1. The file path in the DB exactly matches the file's location on disk.
2. The server process has read permission on the file.
3. You re-ran the import script from the correct working directory.
