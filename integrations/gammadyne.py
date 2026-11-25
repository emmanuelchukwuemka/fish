"""Gammadyne Mailer adapter.

Gammadyne Mailer is a Windows desktop application; this module drives its
command-line mode via ``subprocess`` as an alternative delivery provider to
the existing SMTP/simulation path in app.py.

Command-line syntax status
---------------------------
The switches used below (/o, /q, /n, /m, /nosum, /subject, /text, /html)
were read verbatim from the installed application's own compiled Help file
("Command-Line\\One-Time Send" and "Command-Line\\Send Operation" topics,
confirmed against Gammadyne's documented examples such as
``gm /o"foo@bar.com" /from"<johnq@public.com>" customer.mmp`` and
``gm /s /n /m "c:\\projects\\long file name.mmp"``). None are invented.

Two things the docs do NOT provide, which matters for how this is used:
  * There is no built-in dry-run/test switch for a Send Operation — only a
    separate address-verification tool (/va) that checks syntax/MX records,
    not a "simulate a send" mode. That's why dry_run here never shells out
    at all; it only validates and previews, which is the only safe way to
    offer a dry run with this application.
  * Exit codes aren't enumerated in a table. The docs describe behavior
    ("terminates immediately if there are no problems... otherwise remains
    open" unless /q is given) rather than promising e.g. 0 == success. We
    treat returncode == 0 as success, consistent with that description and
    with normal Windows CLI convention, but this is an inference, not a
    documented guarantee — review the project's own Send Log if in doubt.

/o ("One-Time Send") is used instead of /s because it lets the command line
override the Mailing List entirely ("all project settings on the Database
branch and Recipients branch are ignored"), which is what guarantees a
Gammadyne job can only ever reach the exact, already-allowlisted address
passed on the command line — never whatever recipient list happens to be
saved inside the .mmp project file. /subject, /text, and /html similarly
override the project's stored subject/body so the live content always
matches the PhishLab campaign, not whatever the project last had open in
the Gammadyne GUI.

One subprocess call is made per recipient (not one call for the whole
batch), so each recipient's own unique /simulation/<token> link can be
written into its own body files and so a single bad address can never
affect delivery to the others.

Known limitation: PhishLab campaign subject/body text is not escaped against
Gammadyne's G-Merge template syntax (e.g. a literal "[[" in admin-authored
text could be misinterpreted as a merge expression). Low risk for short,
plain-English training copy, but worth knowing if a send behaves oddly.
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

GAMMADYNE_EXE_PATH = os.getenv("GAMMADYNE_EXE_PATH", "").strip()
GAMMADYNE_PROJECT_PATH = os.getenv("GAMMADYNE_PROJECT_PATH", "").strip()
GAMMADYNE_TIMEOUT_SECONDS = int(os.getenv("GAMMADYNE_TIMEOUT_SECONDS", "120"))
SYNTAX_VERIFIED = os.getenv("GAMMADYNE_SYNTAX_VERIFIED", "false").strip().lower() in ("1", "true", "yes")

# Always applied to every live invocation — not overridable per job. /q and /n
# are what keep an unattended subprocess call from hanging on a popup dialog
# (error message or the Preview Emails confirmation window); /nosum suppresses
# the Send Summary popup for the same reason. /m just keeps the window out of
# the way. All four are documented switches, not something added speculatively
# — omitting them is what would be the gamble, since the docs explicitly say
# Gammadyne "remains open" on error unless /q is given.
UNATTENDED_SWITCHES = ["/q", "/n", "/m", "/nosum"]


class GammadyneNotConfigured(Exception):
    """Raised when the executable or project path is missing or does not exist."""


class GammadyneRecipientRejected(Exception):
    """Raised when a job would target an address outside TEST_RECIPIENTS."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RecipientPreview:
    email: str
    simulation_url: str
    subject: str
    body_preview: str


@dataclass
class JobResult:
    status: str  # "dry_run" | "blocked" | "error" | "completed" | "failed"
    dry_run: bool
    recipient_count: int
    preview: list
    command: Optional[list] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    started_at: str = field(default_factory=_now)
    finished_at: Optional[str] = None


def resolve_executable() -> Path:
    """Reads the executable path from the environment only — never from a
    request or the database — so it can't be influenced by anything short of
    editing .env directly."""
    if not GAMMADYNE_EXE_PATH:
        raise GammadyneNotConfigured("GAMMADYNE_EXE_PATH is not set in the environment.")
    exe = Path(GAMMADYNE_EXE_PATH)
    if not exe.is_file():
        raise GammadyneNotConfigured(f"Configured Gammadyne executable was not found: {exe.name}")
    return exe


def resolve_project() -> Path:
    """The admin must have already created and saved a Gammadyne project
    (.mmp) in the Gammadyne GUI with its SMTP/sending settings configured —
    this integration overrides the project's subject, body, and recipient
    on every live call, but does not create or edit the project file itself."""
    if not GAMMADYNE_PROJECT_PATH:
        raise GammadyneNotConfigured("GAMMADYNE_PROJECT_PATH is not set in the environment.")
    project = Path(GAMMADYNE_PROJECT_PATH)
    if not project.is_file():
        raise GammadyneNotConfigured(f"Configured Gammadyne project file was not found: {project.name}")
    return project


def validate_recipients(recipients, allowed_recipients):
    """recipients: iterable of (email, token) tuples.

    Re-checks the TEST_RECIPIENTS allowlist independently of the campaign
    creation check in app.py, mirroring deliver_email()'s defense-in-depth
    re-check for the SMTP path — a Gammadyne job can never be built for an
    address outside the allowlist, even if that check is ever changed or
    bypassed upstream.
    """
    rejected = [email for email, _ in recipients if email.lower() not in allowed_recipients]
    if rejected:
        raise GammadyneRecipientRejected(
            "Recipients outside the TEST_RECIPIENTS allowlist: " + ", ".join(rejected)
        )


def _render_bodies(subject: str, body: str, sim_url: str):
    safe_body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    text_body = body + f"\n\nOpen the authorized awareness exercise: {sim_url}"
    html_body = (
        '<html><body style="font-family:Arial,sans-serif">'
        '<div style="max-width:600px;margin:auto;padding:24px">'
        '<p style="font-size:12px;color:#64748b">AUTHORIZED SECURITY AWARENESS EXERCISE</p>'
        f'<div>{safe_body}</div>'
        f'<p><a href="{sim_url}">Continue to the training exercise</a></p>'
        '<hr><small>This message is part of an approved security-awareness test.</small>'
        '</div></body></html>'
    )
    return text_body, html_body


def build_preview(subject: str, body: str, recipients, base_url: str) -> list:
    """Builds a per-recipient preview using the SAME unique /simulation/<token>
    URLs already generated for the SMTP/simulation delivery path (app.py's
    new_campaign route) — Gammadyne jobs reuse existing recipient tokens
    rather than minting new ones, so training links stay unique and tracked
    regardless of which provider actually sends the message."""
    previews = []
    for email, token in recipients:
        sim_url = f"{base_url}/simulation/{token}"
        text_body, _ = _render_bodies(subject, body, sim_url)
        previews.append(RecipientPreview(
            email=email, simulation_url=sim_url, subject=subject, body_preview=text_body,
        ))
    return previews


def _run_subprocess(args: list, timeout: int) -> subprocess.CompletedProcess:
    """Safe subprocess invocation: argv list (no shell string), shell=False,
    output captured as text, bounded by an explicit timeout."""
    return subprocess.run(
        args,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _build_live_args(exe: Path, project: Path, subject: str, text_path: Path, html_path: Path, recipient_email: str) -> list:
    """Builds argv for one recipient's One-Time Send. Every switch here is
    documented (see module docstring) — each list element is one argv entry,
    so Windows/Python's own quoting delivers switch+value pairs like
    "/subject<value>" to gm.exe intact without any shell involved."""
    return [
        str(exe),
        f"/o{recipient_email}",
        *UNATTENDED_SWITCHES,
        f"/subject{subject}",
        f"/text{text_path}",
        f"/html{html_path}",
        str(project),
    ]


def _run_live_send(exe: Path, project: Path, subject: str, body: str, recipients, base_url: str, timeout: int):
    """Runs one gm.exe invocation per recipient (so one bad address can't
    affect the others, and each recipient's own unique training link is
    guaranteed to land in their own body files). Returns
    (status, exit_code, stdout, stderr, error, commands)."""
    commands = []
    stdout_lines = []
    stderr_parts = []
    overall_exit_code = 0
    had_error = False

    with tempfile.TemporaryDirectory(prefix="phishlab_gammadyne_") as tmpdir:
        tmp = Path(tmpdir)
        for i, (email, token) in enumerate(recipients):
            sim_url = f"{base_url}/simulation/{token}"
            text_body, html_body = _render_bodies(subject, body, sim_url)
            text_path = tmp / f"body_{i}.txt"
            html_path = tmp / f"body_{i}.htm"
            text_path.write_text(text_body, encoding="utf-8")
            html_path.write_text(html_body, encoding="utf-8")

            args = _build_live_args(exe, project, subject, text_path, html_path, email)
            commands.append(args)
            try:
                proc = _run_subprocess(args, timeout)
                stdout_lines.append(f"{email}: exit={proc.returncode}")
                if proc.stdout:
                    stderr_parts.append(f"{email} stdout: {proc.stdout.strip()[:500]}")
                if proc.stderr:
                    stderr_parts.append(f"{email} stderr: {proc.stderr.strip()[:500]}")
                if proc.returncode != 0:
                    had_error = True
                    if overall_exit_code == 0:
                        overall_exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                had_error = True
                stdout_lines.append(f"{email}: timeout after {timeout}s")
            except OSError as exc:
                had_error = True
                stdout_lines.append(f"{email}: OSError {exc}")

    status = "failed" if had_error else "completed"
    error = None if not had_error else "One or more recipients failed — see stdout for the per-recipient breakdown."
    return status, (overall_exit_code if had_error else 0), "\n".join(stdout_lines), "\n".join(stderr_parts), error, commands


def submit_job(subject: str, body: str, recipients, base_url: str, allowed_recipients, dry_run: bool = True) -> JobResult:
    """Builds (and, once approved, executes) a Gammadyne delivery job for an
    existing campaign's recipients. Callers in app.py always pass dry_run=True
    first; dry_run=False is only reached from the separate, explicit
    /gammadyne/approve route after an administrator has approved the job.
    """
    started = _now()
    recipients = list(recipients)

    try:
        exe = resolve_executable()
    except GammadyneNotConfigured as exc:
        return JobResult(status="error", dry_run=dry_run, recipient_count=len(recipients), preview=[],
                          error=str(exc), started_at=started, finished_at=_now())

    try:
        validate_recipients(recipients, allowed_recipients)
    except GammadyneRecipientRejected as exc:
        return JobResult(status="blocked", dry_run=dry_run, recipient_count=len(recipients), preview=[],
                          error=str(exc), started_at=started, finished_at=_now())

    preview = build_preview(subject, body, recipients, base_url)

    if dry_run:
        return JobResult(status="dry_run", dry_run=True, recipient_count=len(recipients), preview=preview,
                          command=None, started_at=started, finished_at=_now())

    if not SYNTAX_VERIFIED:
        return JobResult(
            status="blocked", dry_run=False, recipient_count=len(recipients), preview=preview,
            error=("Live send blocked: GAMMADYNE_SYNTAX_VERIFIED is not set to true. Set it in .env "
                   "only after reviewing integrations/gammadyne.py's documented switches yourself."),
            started_at=started, finished_at=_now(),
        )

    try:
        project = resolve_project()
    except GammadyneNotConfigured as exc:
        return JobResult(status="error", dry_run=False, recipient_count=len(recipients), preview=preview,
                          error=str(exc), started_at=started, finished_at=_now())

    if not recipients:
        return JobResult(status="error", dry_run=False, recipient_count=0, preview=preview,
                          error="No recipients to send to.", started_at=started, finished_at=_now())

    try:
        status, exit_code, stdout, stderr, error, commands = _run_live_send(
            exe, project, subject, body, recipients, base_url, GAMMADYNE_TIMEOUT_SECONDS
        )
        return JobResult(status=status, dry_run=False, recipient_count=len(recipients), preview=preview,
                          command=commands, exit_code=exit_code, stdout=stdout, stderr=stderr, error=error,
                          started_at=started, finished_at=_now())
    except Exception as exc:
        return JobResult(status="error", dry_run=False, recipient_count=len(recipients), preview=preview,
                          error=str(exc), started_at=started, finished_at=_now())
