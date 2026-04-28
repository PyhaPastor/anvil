# Changelog

## Unreleased

### Added
- **Per-customer "Delete hashes" button** on the customers list. Removes all `HashList` rows (and cascaded `Hash` rows) for every job belonging to a customer, and unlinks the underlying hash files from disk when no surviving hash list still references them. Action is recorded in the audit log as `customer_hashes_deleted`.
- **Customer is now required when creating a job.** `POST /jobs/new` rejects submissions without a valid `customer_id` (HTTP 400), and the new-job form's customer `<select>` is marked `required` with a disabled placeholder so the browser blocks submission client-side as well. Ensures every job has a clear scope for the per-customer hash-deletion flow. Job re-run/clone is unaffected — it inherits `customer_id` from the original job.

### Changed
- **"Delete hashes" no longer resets job counters.** `Job.total_hashes` and `Job.cracked_count` are preserved when a customer's hashes are deleted, so the presentation dashboard's **Accounts Cracked** statistic (`SUM(Job.cracked_count)`) and overall crack-rate history remain intact even after the underlying hash data has been purged.
