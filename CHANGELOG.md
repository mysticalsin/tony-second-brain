# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Added

- **`specs/10-conduct-and-safety.md`** — full specification for the conduct and safety layer:
  the injectable ~150-token conduct core, the five enforcement gates (preflight write-check,
  triage promotion gate, injection scanner, integrity check, incident path), the four-probe
  behavioral eval harness (`conduct-eval.sh`), the causal negative-control finding (base model
  alignment ≈ conduct prose; structural value is in the gates and the regression suite), the
  nightly dreaming loop, the sync-check anti-drift mechanism, the `verified / assumed / unknown`
  claim-colour system, and the five success metrics + `conduct-stats.sh --check` breach logic.

### Changed

- **`README.md`** — added the conduct and safety layer to the feature table and the repository
  map (new `specs/10-conduct-and-safety.md` entry).
- **`SETUP.md`** — added a "Conduct and safety layer" subsection in step 3b documenting the
  three commands users can run to verify the behavioral gates:
  `conduct-eval.sh`, `conduct-sync-check.sh`, `conduct-stats.sh --check`.
- **`plugin/claude-command-center/styles.css`** — genericised the CSS file comment header
  (removed a firm-specific brand name; colour palette and rules unchanged).

### Fixed

- **`plugin/claude-command-center/styles.css`** — removed residual employer-specific label
  from the file comment (firm-specific label removed → generic label).
  No functional CSS change.

---

## Notes

- The `rfp-model.json` fixture contact was already `procurement@globex.example.com` — no
  change required.
- The conduct spec is intentionally generic (no client names, no employer names, no vault
  paths hardcoded). Shell script examples use `$VAULT_ROOT` consistent with the existing
  `brain-refresh.sh` convention.
