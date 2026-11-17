# Restore media.py on fix/youtube-pot

## Problem
`plugins/media/media.py` was corrupted by incomplete large-file uploads.

## Last known-good commit for media.py
`d8e6bcb1a4f847681ad4758350f0ab017ab74de6`
(also identical to `main` for this file)

## Restore command (run locally, then push)

```bash
git fetch origin
git checkout fix/youtube-pot
git restore --source=d8e6bcb1a4f847681ad4758350f0ab017ab74de6 -- plugins/media/media.py
# verify
wc -c plugins/media/media.py   # expect ~51726 bytes
python3 -m py_compile plugins/media/media.py
git add plugins/media/media.py
git commit -m "fix: restore media.py from d8e6bcb1 (last good)"
git push origin fix/youtube-pot
```

## Then apply integration

```bash
git apply plugins/media/youtube-integration.patch
# OR if patch path differs, use the full patch from this branch
python3 -m py_compile plugins/media/media.py
python3 plugins/media/tests_youtube_pot.py
git add plugins/media/media.py
git commit -m "fix(youtube): integrate youtube_pot into probe and download"
git push origin fix/youtube-pot
```

Do NOT force-push. Do NOT change main or Railway production.
