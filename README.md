# PhishLab — Authorized Awareness Simulator

A small Flask application for controlled phishing-awareness exercises. It records email open-pixel requests, tracked-link clicks, and submission attempts. The email entered in the training flow is held only in browser memory and included in a completion notification to the configured administrator inbox; it is not written to the database or application logs. **The mock password value is discarded in the browser and is never transmitted, stored, or included in a notification.**

## Requirements
- Python 3.10+
- SMTP account dedicated to security-awareness testing (optional until sending email)

## Setup (Windows PowerShell)
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Generate an administrator password hash:
```powershell
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('Choose-A-Strong-Password'))"
```
Put the output in `ADMIN_PASSWORD_HASH` in `.env`. Set a long random `FLASK_SECRET_KEY`.

Set `TEST_RECIPIENTS` to the exact test inboxes you own or have written authorization to use. The app refuses to send campaign messages to addresses not on this list, in both delivery modes. For Gmail notifications to `nwekee125@gmail.com`, set `SMTP_USERNAME` to the Gmail account and `SMTP_PASSWORD` to its Google App Password; optionally set `SMTP_FROM` to that account's sender address. Gmail notification delivery uses `smtp.gmail.com` over SSL on port 465. Configure `SMTP_HOST`, `SMTP_PORT`, and the same SMTP credentials for campaign delivery. Set `BASE_URL` to the public HTTPS URL only after deploying behind a properly configured HTTPS reverse proxy.

### Delivery modes
- `DELIVERY_MODE=simulation` (default): no network connection is made. Each fully rendered message, including completion-notification email addresses, is appended to `SIMULATION_LOG_PATH` (default `simulation_outbox.log`) so you can review exactly what would have been sent. Use this for local development and dry runs.
- `DELIVERY_MODE=smtp`: sends live mail through the configured `SMTP_*` settings, still restricted to `TEST_RECIPIENTS`.

### Gammadyne Mailer (alternative provider)
A separate, optional path for submitting an existing campaign to the locally installed [Gammadyne Mailer](https://www.gammadyne.com/) instead of SMTP. It lives in `integrations/gammadyne.py` and is driven from a new "Gammadyne Mailer" card on the dashboard:

1. In the Gammadyne Mailer GUI, create and save a project (.mmp) with its SMTP/sending settings configured. Its own recipient list and subject/body don't matter — PhishLab overrides both on every live call.
2. Set `GAMMADYNE_EXE_PATH` (Gammadyne's `gm.exe`) and `GAMMADYNE_PROJECT_PATH` (that project file) in `.env`. Both paths are only ever read from the environment — never rendered in the dashboard or logged publicly.
3. Pick a campaign and click **Dry-run Gammadyne job**. This validates the executable/project paths, re-checks every recipient against `TEST_RECIPIENTS`, and builds a preview (subject, body, and each recipient's existing unique `/simulation/<token>` link) — no process is spawned and no mail is sent. Gammadyne has no built-in "dry run" mode for sending, so this preview-only step is implemented entirely on the PhishLab side.
4. Review the dry-run result in the job table at the bottom of the card.
5. Set `GAMMADYNE_SYNTAX_VERIFIED=true` in `.env` once you've reviewed `integrations/gammadyne.py` yourself.
6. **Approve & send via Gammadyne** is a separate, explicit action (its own button/route, own CSRF-protected POST) — a dry run alone never sends anything. A campaign can only be approved for live Gammadyne delivery once; a second approval attempt is rejected at the database level.

Live sending runs one `gm.exe /o<recipient> ...` One-Time Send per recipient (never Gammadyne's own stored mailing list), overriding `/subject`, `/text`, and `/html` with the campaign's content and that recipient's unique training link, and always passing `/q /n /m /nosum` so an unattended run can't hang on a popup. Every switch is documented in Gammadyne's own Help file ("Command-Line\One-Time Send" / "Command-Line\Send Operation" topics) — see the module docstring in `integrations/gammadyne.py` for exactly which examples back each one. Gammadyne's docs describe success/failure behavior qualitatively (terminates immediately vs. remains open) rather than publishing a numeric exit-code table, so treat a non-zero exit as a strong signal of failure rather than an absolute guarantee — check the project's own Send Log if a result looks surprising. **Do one real live test to a single test inbox you control before trusting this broadly.**

## Run
```powershell
python app.py
```
Open http://127.0.0.1:5000 and sign in with the configured username and password.

## What is tracked
- `open`: email client requested the tracking pixel. Many clients block or proxy images, so this is approximate.
- `click`: recipient opened the unique campaign link.
- `submission_attempt`: recipient submitted the training form. Form values are not stored.
- `simulation_completed`: recipient completed the simulation. The notification includes the email entered, UTC timestamp, completion status, and whether the password field was completed; password contents are never sent or stored.

## Safety and deployment
- Use only with documented authorization and test recipients.
- Do not impersonate real services or collect actual passwords.
- Use HTTPS in deployment; never expose the development server directly to the internet.
- Set a strong secret key, protect `.env`, use a dedicated SMTP account, and restrict access to the dashboard.
- Add CSRF protection, rate limiting, audit logging, backups, and a production WSGI server before any broader deployment.
- This starter is a local development scaffold, not a hardened production service.
