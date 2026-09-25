# Google Drive sync: undownloadable files + silently unreachable roots

**Branch:** `fix/gdrive-undownloadable-and-dead-roots`
**Branched from:** `18c44bf` (not `origin/master` — see *Merging* below)
**Written:** 2026-09-25, from a live diagnosis on the `voitta.agnitio.ai` box.
**Deployed:** running in the `voitta-rag` screen session since 2026-09-25.

## What was wrong in production

Folder 1 (*Agnitio Meeting Recordings*, 13 Google Drive roots, hourly auto-sync)
sat at `sync_status='error'` on every single run, while simultaneously failing
to notice that two of its roots had gone dark. Both halves were reporting bugs;
the sync engine itself was fine (500/500 recent jobs `done`, 1750/1750 files
`indexed`).

### Bug 1 — three unfixable files owned the error badge

`sync_job.py:194` sets the folder's status from the errors list:

```python
src.sync_status = "error" if stats.errors else "idle"
```

`stats.errors` held three Google Chat transcript attachments in
`Meet Recordings-2`, each failing with **403 `cannotDownloadFile`**. Drive lists
Chat attachments but never permits `alt=media` for them, so this is permanent
and unfixable — no scope or share grants it. Three files pinned the whole
folder red forever, which meant the badge carried no information and any *real*
failure would have been invisible in the noise.

### Bug 2 — an unshared root syncs nothing, silently (the serious one)

Two roots had been unshared from the service account
(`gdrive-agent@agnitio-ops.iam.gserviceaccount.com`, a plain SA with no
domain-wide delegation, so it only sees what is explicitly shared with it):

| Root ID | Local dir slot |
|---|---|
| `1X_BOi4ofN34524HOZ-lAjhVPU_0xgc4S` | `Meet Recordings-7` |
| `1bum9y_PP5h8msyjWfNeRbgJgEXaVLWGl` | `Meet Recordings-9` |

Confirmed on disk: `Meet Recordings`, `-2`…`-6`, `-8`, `-10` all exist, and
**only `-7` and `-9` are missing** — the two slots those roots reserve at
`google_drive.py:1014` before enumeration runs.

The reason nobody noticed for ~4 months (first `PERMISSION_DENIED` in
`app.log.3` is **2026-05-31**) is that **Drive does not raise for an
inaccessible parent**. Verified directly against the live API:

```
files().get(fileId=<dead root>)                      -> HTTP 404 notFound
files().list(q="'<dead root>' in parents")           -> HTTP 200, 0 children
```

So `_enumerate` *succeeded*, appended nothing to `stats.errors`, and the root
was indistinguishable from an empty one. `"Drive enumeration failed"` appears
**0 times** in the logs. The only trace was a `googleapiclient` WARNING from
`_fetch_shared_by`, whose `files.get` is wrapped in `except Exception: return {}`
— the library logged the 403 on its way out and the app threw it away. One
swallowed warning per dead root per run: exactly the 2/run we were seeing.

## What this branch changes

All in `src/voitta_rag_enterprise/services/sync/google_drive.py`.

**Fix 1 — `cannotDownloadFile` is a skip, not an error.** Follows the existing
`files_404` / `files_too_large` precedent exactly (non-error buckets for things
the user cannot act on):

- new `files_undownloadable` counter on `GoogleDriveSyncStats` + `as_dict()`
- new `_is_cannot_download()` classifier (403 **and** `cannotDownloadFile`;
  every other 403 stays a real error)
- `_materialize_one` returns the `"undownloadable"` skip bucket; the result
  loop counts it

Effect: the 3 Chat files leave `stats.errors`, so folder 1 reports `idle`.

**Fix 2 — a dead root is now loud.** `_fetch_shared_by` already makes the one
API call that *does* fail on a dead root and was discarding it, so this costs
**zero extra requests**:

- `_fetch_shared_by` returns `(shared_by, error)` instead of swallowing
- the root loop pairs two signals — **listed 0 items AND metadata unreadable**
  — and appends a real `stats.errors` entry naming the folder
- new `_is_root_inaccessible()` restricts this to a genuine Drive **403/404**

That last predicate matters: `_FakeFiles` in the test suite has no `get()`, so
a naive `error is not None` check would read the resulting `AttributeError` as
"not shared" and cry wolf on every empty folder. Narrowing to 403/404 also
keeps a genuinely-empty-but-readable root (the real `aclaw` root) quiet.

## Tests

5 added to `tests/unit/test_google_drive_connector.py`, mirroring the existing
`test_export_too_large_routes_to_files_too_large_not_errors` harness:

- `test_is_cannot_download_recognises_chat_attachment_403`
- `test_is_root_inaccessible_only_matches_403_404`
- `test_unshared_root_listing_zero_items_is_surfaced_as_error` ← the incident
- `test_empty_but_readable_root_is_not_reported` ← the false-positive guard
- `test_cannot_download_routes_to_files_undownloadable_not_errors`

`tests/unit/test_google_drive_connector.py`: **25 passed**.
Full `tests/unit`: **733 passed, 2 failed, 6 skipped** — both failures
(`test_parsers.py::test_image_file_parser_rejects_non_image`,
`test_qdrant_managed.py::test_orphan_sweep_kills_previous_child`) are
**pre-existing on `18c44bf`**, verified by re-running them stashed. ruff (8) and
mypy (27) counts are likewise byte-identical to baseline on these files.

## Merging (for 2026-09-26)

This branched from local `18c44bf`, which is **3 commits behind
`origin/master`** (`cd1f60a`). That was deliberate — the box was running
`18c44bf` and the priority was getting the fix live without also shipping a
month of unreviewed change.

Those 3 commits (`27f7558` linked folders, `53f4e3c` merge, `cd1f60a` release
automation) **do not touch `google_drive.py`**, so this should rebase onto
`origin/master` cleanly:

```sh
git fetch origin
git rebase origin/master fix/gdrive-undownloadable-and-dead-roots
.venv/bin/python -m pytest tests/unit/test_google_drive_connector.py -q
```

⚠️ Before deploying `origin/master` itself: `cd1f60a`'s range includes
`db/models.py` (+8) and `db/database.py` (+5) — **a schema change**. Check how
it migrates and back up `~/.voitta-image-rag/voitta.db` (97 MB) first. Also
`services/ignore.py` (+75) changes which files get skipped; folder 1 currently
reports `files_skipped: 140` per run, so expect that number to move. None of
this applies to the branch as it stands, which is `google_drive.py` + tests only.

## Still open (not code)

1. **Share the two dead roots** with
   `gdrive-agent@agnitio-ops.iam.gserviceaccount.com` (Viewer), or remove them
   from folder 1's `gd_folder_id`. A human who can see them must identify them
   first — the 404 hides even the folder name and owner from the SA. Until then
   Fix 2 will now report them as a real error each run, which is the point.
2. **Clear the stale `sync_error`** left in the DB from before Fix 1, via the
   existing endpoint at `api/routes/sync/core.py:196` (wipes `sync_error`,
   resets `error` → `idle`). No manual DB edit needed.
