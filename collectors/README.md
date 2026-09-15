# Naukri collector (runs in your browser)

Naukri blocks automated servers (GitHub) with a captcha, so Naukri roles are collected in the
Claude desktop app's browser once a day, before the 9 AM IST scan:

1. Open each search page below (last 3 days, 2+ yrs experience).
2. On each page run `naukri_extract.js` – it saves matching roles in the browser.
3. Export the saved roles and commit them to `data/naukri_inbox.json`.
4. The 9 AM scan reads that file (ignored if older than 3 days).

Searches: strategy manager, growth manager, chief of staff, program manager, business development manager,
category manager, key account manager, strategy consultant, product manager, partnerships manager,
business operations, corporate strategy, founders office, mba strategy
