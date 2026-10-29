# Test plan and coverage

## Run the suite

From the project root with the virtual environment active:

```powershell
python -m unittest discover -s tests -v
```

The 26-test suite uses Python's standard `unittest` library. MySQL workflow and migration tests require the credentials in `.env` to have permission to create and drop databases. Tests use uniquely named temporary databases and do not write test records to `loyalty_ledger`. The test classes drop their temporary databases after running. The generated CSV loader test temporarily enables the MySQL `local_infile` server setting if it is off, then restores the prior value.

## Coverage map

| Problem area | Automated coverage |
|---|---|
| Point arithmetic | Whole-point rounding and negative spend/rate rejection. |
| Accrual | Eligible net spend, order-total fallback when no lines exist, exclusion of ineligible lines, zero-point orders, canceled-order rejection, and duplicate-award rejection. |
| Redemption | Positive integer input, insufficient-balance rejection without writes, oldest-expiry-first lot consumption, active reward and exact reward cost, and concurrent requests where only one can spend the same balance. |
| Returns | Partial proportional reversals, cumulative refunds, reversal links to the source accrual, repeated-return rejection, over-refund rejection without committing the second return, full return after points were redeemed, and zero-value order rejection. |
| Expiry | A due lot expires once, repeat processing is a no-op for that lot, multiple due lots are processed together, and a future-expiring lot remains open. |
| Tiers | Discounted spend is measured net of discount, tier progression occurs at the configured threshold, and the next accrual uses the new tier's earning rate. |
| Ledger validation | An intentionally mismatched account balance is detected. The regular validator also checks all seven ledger and lifecycle conditions. |
| Database constraints | Invalid order status and a missing customer parent are rejected by MySQL. |
| Synthetic edge cases | Six unique labelled fixtures exist separately from the baseline data, including the required mismatch, oversized redemption, missing return reversal, duplicate expiry, gross-versus-net tier, and concurrent redemption cases. |
| Reproducibility and data integrity | The generator CLI produces identical manifests for two small runs with the same seed. Full-scale regeneration also produced identical manifests and SHA-256 checksums. Generator validations cover balance/ledger totals, lots, expired-lot closure, lifecycle audit coverage, IDs, foreign-key references, arithmetic/date ordering, and requested transaction volume. |
| CSV/SQL export | The generated `LOAD DATA LOCAL INFILE` script loads a small dataset into a fresh temporary schema; the imported rows pass the live MySQL reconciliation checks. Custom output directories are included in the generated loader paths. |
| Migration | A synthetic pre-v2 schema is upgraded, old accrual lots are backfilled, an opening balance is reconciled, and the repair can be rerun without duplicate records. A separate disposable clone of the owner's pre-v2 backup also migrated and reconciled successfully. |
| CLI entry points | Award, interactive redemption, return completion, and expiry commands run end-to-end against the temporary test database. |
| CLI failure reporting | Rejected award and redemption requests print an explanation and return a non-zero process exit status. |

## Recorded results

- Automated suite: 26 passed, 0 failures, 0 errors on 2026-10-28.
- Local project database: all seven reconciliation checks passed after the test run.
- Full synthetic dataset: 20,000 customers, 100,000 orders, and 300,000 point transactions passed generator validation.
- Small synthetic dataset: 20 customers, 100 orders, and 300 point transactions passed generator validation.
- Migration and opening-balance repair: passed on both the synthetic legacy fixture and a disposable clone of the owner's backup. Both temporary schemas were dropped.
- CSV loader: passed against a fresh temporary schema. MySQL `local_infile` was temporarily turned on for the import and restored to `OFF`; all temporary schemas were dropped.

These results cover the stated business and data-validation scenarios. They do not prove production performance, a hosted deployment, recovery from a process crash at every SQL statement, or correctness on every MySQL version. Review the implementation and explain the design decisions before submission.
