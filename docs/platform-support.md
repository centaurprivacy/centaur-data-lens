# Platform support

Export formats are provider-controlled and can change without notice. Guides
are bundled locally and include the date they were last verified.

## Google

Request JSON through [Google Takeout](https://takeout.google.com/).

Supported:

- My Activity
- Chrome browser history
- YouTube and YouTube Music history
- Google Play installation history

Excluded in v0.1:

- Gmail, Drive, Keep, Photos, and media
- Unsupported location formats
- HTML exports

## Meta

Request JSON through
[Accounts Center](https://accountscenter.facebook.com/info_and_permissions/).

Supported:

- Facebook and Instagram account/profile metadata
- Searches and activity history
- Advertising interests and activity
- Off-Meta activity
- Devices, sessions, and login history
- Connected apps and websites
- Connection and content counts/date ranges when available

Excluded in v0.1:

- Message bodies and contacts
- Post and comment bodies
- Photos, videos, and media
- Facial-recognition assets
- HTML exports

## Reporting schema changes

Use `centaur-data-lens diagnostics PLATFORM EXPORT... --output diagnostics.json`
to create value-free category counts. Do not attach an archive or report to an
issue. Review the diagnostics before sharing even though it is designed not to
contain record values.
