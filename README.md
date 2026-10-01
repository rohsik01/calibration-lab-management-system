# Calibration Laboratory Management System (Flask, local server)

    pip install -r requirements.txt
    python app.py
    open http://127.0.0.1:5000

- Home page: summary counts, items needing attention, recent calibrations, quick actions. Menu is in the left sidebar (collapses on phones).
- Multi-point calibration: record several reference points per calibration; the sensor passes only if every point is within tolerance. Certificates list every point.
- Export: the "Export CSV" menu opens an export page (filter by station, sensor, dates, result, status; choose columns; CSV or Excel-friendly CSV).
- Data is stored in `calibration.db` (SQLite) - back this file up regularly.
- To let other PCs on your network connect, change `host="127.0.0.1"` to `"0.0.0.0"` in app.py
  and allow port 5000 in your firewall.
- On first run, open the site and create the administrator account. `secret.key` is generated automatically - keep it private.

## Language and banner
- English / नेपाली: switch in the sidebar (or top of the sign-in page). The choice is remembered in the browser.
- The banner (Government of Nepal ... Babarmahal, Kathmandu) is shown at the top of the home page and on every report:
  certificates, printed pages, CSV and Excel exports. Edit its wording in `BANNER` in app.py.
- All Nepali text is in `translations.py`. Missing entries simply show in English. Please have the wording reviewed.

## Reference standards and Excel export
- Reference standards register: admins add/edit standards (traceability, certificate, expiry); technicians pick them when calibrating.
  Expired standards are blocked. Install the Excel package once: `python -m pip install -r requirements.txt`.
