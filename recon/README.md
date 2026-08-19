# Phase 0 — Recon (capturing the booking traffic)

Goal: record one real booking session per platform so we can see the exact API
calls and design the bot's provider adapters. **You do this once per venue.**

Targets:
- `hyde-park-recon.har` — Hyde Park (Park Sports)
- `regents-recon.har` — Regent's Park (Park Sports)
- `paddington-recon.har` — Paddington Rec / Everyone Active (Gladstone MRM)

---

## ⚠️ Safety first — read this

A HAR file records **everything** your browser sends: your password, session
cookies, and anything you type into a form (including card details).

- **For the first pass, STOP before entering any card details.** That keeps
  card data out of the file entirely and still captures login + availability +
  slot selection.
- **Never paste a HAR into chat or commit it to git.** `.gitignore` already
  blocks `*.har`. Just leave the files in this `recon/` folder.
- The parser redacts cookies/tokens/passwords/card fields in its output by
  default — but that redaction is **best-effort, not a guarantee**. It hides
  fields whose *names* look sensitive, so anything it didn't anticipate comes
  through in the clear, and non-JSON bodies (HTML pages, base64 payloads) are
  emitted raw. Treat `recon/out/` as containing your personal data and keep it
  out of git like the HARs themselves.

---

## Steps

Do this once per platform. Start with **Hyde Park**. Use Chrome **or** Firefox.

**Chrome (macOS):**

1. Open an **Incognito window** (`⌘⇧N`) — clean cookies = clean capture.
2. Open **DevTools** (`⌘⌥I`) → **Network** tab.
3. In the Network toolbar tick:
   - ✅ **Preserve log**
   - ✅ **Disable cache**
   - Filter: **Fetch/XHR**
4. Do a normal booking, slowly:
   - Go to the venue's booking page.
   - **Log in.**
   - Select the **date, court, and time** you'd actually want.
   - Proceed up to (but **not into**) the **payment / card** step.
5. **STOP before typing card details.**
6. Right-click the request list → **"Save all as HAR with content"**.
7. Save it into this folder with the matching name above.
8. Repeat for the other two venues.

**Firefox (macOS)** — same idea, different labels:

1. Open a **Private window** (`⌘⇧P`).
2. Open **DevTools** (`⌘⌥I`) → **Network** tab. Keep DevTools open the whole time.
3. Click the **⚙ gear icon** → ensure **"Persist Logs"** is ticked; tick
   **"Disable Cache"** in the toolbar; click the **XHR** filter.
4. Do the booking slowly (navigate → log in → pick date/court/time → up to but
   not into the card step).
5. **STOP before typing card details.**
6. Right-click the request list **or** ⚙ gear icon → **"Save All As HAR"**.
7. Save it into this folder with the matching name above.
8. Repeat for the other two venues.

> Note (both browsers): "Disable Cache" only applies *while DevTools is open*,
> so don't close it mid-session.

> Later, when we're ready to test real payments, you'll do one *full* paid
> booking captured into a separate HAR (treat that one as a secret).

---

## Then parse it

From the project root:

```bash
# one file
python3 scripts/capture_har.py recon/hyde-park-recon.har

# or all of them at once
python3 scripts/capture_har.py recon/*.har
```

This writes readable, redacted summaries to `recon/out/*.md` and prints a short
overview (which hosts and how many API calls were seen).

Tell me once the files are in `recon/` (or once `recon/out/` is populated) and
I'll read the catalogues and map out each platform's auth + availability +
reserve flow.

---

## Troubleshooting

- **"No .har files found"** → the file didn't save here, or didn't end in
  `.har`. Check the path.
- **Almost no requests kept** → the site may render server-side (likely for a
  legacy WebForms booking engine). That's fine and expected — it tells us that
  platform leans browser-automation, not JSON API. Capture it anyway.
- **Worried about a secret leaking** → redaction is on by default and
  `--no-redact` is never used — but do not treat "redacted" as "safe to share".
  Both `recon/out/` and `*.har` are gitignored; leave them that way, and prefer
  storing them outside the repo tree entirely.
