# AGY Rule: Mandatory Documentation Protocol for Changes & Fixes

When modifying the codebase for any bug fix, architectural change, or feature:

1. **Create a Dated Design / Implementation Doc**:
   - Location: `changes/fixes/YYYY-MM-DD_<DESCRIPTIVE_TITLE>.md` (or `changes/design/` for architecture).
   - Structure: Metadata $\to$ Problem & Root Cause $\to$ Architecture Diagram $\to$ Code Implementation $\to$ Verification Results.
2. **Update Living Changelog**:
   - Add newest dated section at top of `changes/CHANGES_SINCE_2026-07-06.md` with link to the fix document.
3. **Ensure Git Readiness**:
   - Verify all edits and unit tests are tracked cleanly with `git status`.
