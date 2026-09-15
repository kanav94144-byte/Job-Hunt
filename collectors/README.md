# Naukri collector (runs in the Claude desktop app's browser)

Naukri blocks automated servers (GitHub) with a captcha, so a scheduled Claude task on Kanav's Mac
collects Naukri roles 3 times a day, ~30 minutes before each scan (8:30 AM, 1:30 PM, 6:30 PM IST):

1. Open each search page below (`https://www.naukri.com/<search>-jobs?experience=2&jobAge=3`).
2. On each page run `naukri_extract.js` – it saves matching roles in the browser (localStorage `jh_naukri`).
3. Run `naukri_export.js` – returns the roles collected in the last 26 hours as gzip+base64.
4. Commit that to `data/naukri_inbox.json` on GitHub using `github_paste.js` in the web editor.
5. The next scan (9 AM / 2 PM / 7 PM IST) reads the file (ignored if older than 3 days). Roles stay on the
   dashboard for 7 days, and the scan folds repeats (same role on Naukri + LinkedIn + company site) into one row.

Searches: strategy-manager, growth-manager, chief-of-staff, program-manager, business-development-manager,
category-manager, key-account-manager, strategy-consultant, product-manager, partnerships-manager,
business-operations, corporate-strategy, founders-office, mba-strategy
