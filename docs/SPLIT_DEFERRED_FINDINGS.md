
## `update_open_take_profit` refuses through an undefined name

`update_open_take_profit` in the storage layer reaches for a bare `_log` that
is not defined in its module, so the refusal branch raises `NameError` instead
of recording the refusal. Pre-existing on `main` before the database rebuild;
the body moved verbatim into `src/storage/trades/ledger.py`, so the defect
moved with it unchanged. Found 2026-10-02 during database instalment 3. Fix
after the structure is sound: give the module its logger, then prove the
refusal path records rather than raises.
