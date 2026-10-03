# Vendored: i18n-audit seed

`tools/i18n_audit_seed.py` is an unmodified copy of `src/i18n_audit_seed.py` from
https://github.com/theluckystrike/i18n-audit

- Commit: `34d6ce1e1c1b55ea7c8895891c73553e736c6c62` (2026-10-03, "Seed: i18n audit scripts from the 2026-10-03 audit of 6 repos")
- Source URL: https://raw.githubusercontent.com/theluckystrike/i18n-audit/34d6ce1e1c1b55ea7c8895891c73553e736c6c62/src/i18n_audit_seed.py
- SHA-256 of the file: see `tools/i18n_audit_seed.sha256`

The upstream repo does not yet ship an installable `src/i18n_audit/` package, so the
seed script is vendored. `bench.py` imports only its parsing and classification
functions (`flat`, `load_po`, `classify`, `icu_args`, `ref_key`); the per-repo path
table inside the seed (`REPOS`) is not used. Locale discovery lives in `bench.py`.

To update: replace the file, update the commit SHA above, and regenerate the checksum
with `sha256sum tools/i18n_audit_seed.py > tools/i18n_audit_seed.sha256`.
