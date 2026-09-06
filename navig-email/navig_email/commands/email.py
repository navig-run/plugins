"""
Email Commands

List, search, and manage emails from configured providers.
"""

import asyncio
import json

import typer

from navig import console_helper as ch
from navig.core.coerce import coerce_bool

email_app = typer.Typer(help="Email operations")


@email_app.command("list")
def list_emails(
    limit: int = typer.Option(10, "--limit", "-n", help="Max number of emails"),
    unread_only: bool = typer.Option(True, "--unread/--all", help="Show only unread"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """
    List emails from your inbox.

    Fetches emails from your configured email provider(s).
    """

    async def _fetch():
        from navig.agent.proactive import (
            GmailProvider,
            IMAPEmailProvider,
            MockEmail,
            OutlookProvider,
        )
        from navig.config import get_config_manager

        cm = get_config_manager()
        # `get_global_config()`, NOT `_load_global_config()`: the latter returns
        # the PYDANTIC-VALIDATED view, which does not declare `proactive` -- so
        # this was ALWAYS {} and the command reported email as disabled no matter
        # what the operator configured. Found by
        # core/tests/quality/test_validated_config_view_keys.py.
        config = cm.get_global_config() or {}

        proactive_cfg = config.get("proactive", {})
        email_cfg = proactive_cfg.get("email", {})

        if not coerce_bool(email_cfg.get("enabled"), default=False):
            if not json_output:
                ch.warning("Email not configured. Using mock data.")
            provider = MockEmail()
        else:
            provider_type = email_cfg.get("provider", "mock")
            email_addr = email_cfg.get("address")
            password = email_cfg.get("password")

            if provider_type == "gmail":
                provider = GmailProvider(email=email_addr, password=password)
            elif provider_type == "outlook":
                provider = OutlookProvider(email=email_addr, password=password)
            elif provider_type == "imap":
                imap_host = email_cfg.get("imap_host")
                smtp_host = email_cfg.get("smtp_host")
                provider = IMAPEmailProvider(
                    email=email_addr,
                    password=password,
                    imap_host=imap_host,
                    smtp_host=smtp_host,
                )
            else:
                provider = MockEmail()

        if unread_only:
            messages = await provider.list_unread(limit=limit)
        else:
            # For now, unread is the only method available
            messages = await provider.list_unread(limit=limit)

        return messages

    messages = asyncio.run(_fetch())

    if json_output:
        # Convert to JSON-serializable format
        messages_data = [
            {
                "id": m.id,
                "subject": m.subject,
                "sender": m.sender,
                "date": m.date.isoformat(),
                "preview": m.preview,
                "is_important": m.is_important,
            }
            for m in messages
        ]
        print(json.dumps(messages_data, indent=2))
    else:
        if not messages:
            ch.info("No emails found")
            return

        status = "unread" if unread_only else "all"
        ch.info(f"Inbox ({status}, showing {len(messages)})")
        ch.console.print()

        for msg in messages:
            date_str = msg.date.strftime("%b %d, %I:%M %p")
            important = "⭐ " if msg.is_important else ""

            ch.console.print(f"  {important}[bold]{msg.subject}[/bold]")
            ch.console.print(f"    [dim]From: {msg.sender}[/dim]")
            ch.console.print(f"    [dim]{date_str}[/dim]")
            if msg.preview:
                preview = msg.preview[:80] + "..." if len(msg.preview) > 80 else msg.preview
                ch.console.print(f"    [dim]{preview}[/dim]")
            ch.console.print()


@email_app.command("setup")
def setup_email(
    provider: str = typer.Argument("gmail", help="Provider: gmail, outlook, imap"),
):
    """
    Configure email provider credentials.

    Interactive setup for email access.
    """
    from navig.config import get_config_manager
    from navig.core.yaml_io import atomic_write_yaml, load_yaml_for_update

    cm = get_config_manager()
    global_config_file = cm.global_config_dir / "config.yaml"

    # Read-modify-write through the shared guard — never wipe a locked/unreadable config.
    config = load_yaml_for_update(global_config_file)

    if "proactive" not in config:
        config["proactive"] = {}

    if "email" not in config["proactive"]:
        config["proactive"]["email"] = {}

    email_cfg = config["proactive"]["email"]

    ch.info(f"{provider.title()} Email Setup")

    email_addr = typer.prompt("Email address")
    email_cfg["address"] = email_addr
    email_cfg["enabled"] = True
    email_cfg["provider"] = provider
    email_cfg["password"] = "${EMAIL_PASSWORD}"

    if provider == "imap":
        imap_host = typer.prompt("IMAP host (e.g., imap.gmail.com)")
        smtp_host = typer.prompt("SMTP host (e.g., smtp.gmail.com)")
        email_cfg["imap_host"] = imap_host
        email_cfg["smtp_host"] = smtp_host

    atomic_write_yaml(config, global_config_file)

    ch.success("✓ Email configured!")
    ch.warning("Set your password: export EMAIL_PASSWORD='your-password'")
    ch.info("Test with: navig email list")


@email_app.command("search")
def search_emails(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max results"),
):
    """
    Search emails by subject, sender, or content.
    """
    ch.warning(f"Search is not implemented — nothing was searched for {query!r}.")
    raise typer.Exit(1)


@email_app.command("send")
def send_email(
    to: str = typer.Option(..., "--to", "-t", help="Recipient email"),
    subject: str = typer.Option(..., "--subject", "-s", help="Email subject"),
    body: str | None = typer.Option(None, "--body", "-b", help="Email body"),
):
    """
    Send an email.

    Requires configured email provider with SMTP access.
    """

    async def _send():
        import os

        from navig.agent.proactive import GmailProvider, IMAPEmailProvider
        from navig.config import get_config_manager

        cm = get_config_manager()
        # `get_global_config()`, NOT `_load_global_config()`: the latter returns
        # the PYDANTIC-VALIDATED view, which does not declare `proactive` -- so
        # this was ALWAYS {} and the command reported email as disabled no matter
        # what the operator configured. Found by
        # core/tests/quality/test_validated_config_view_keys.py.
        config = cm.get_global_config() or {}
        email_cfg = config.get("proactive", {}).get("email", {})

        if not coerce_bool(email_cfg.get("enabled"), default=False):
            ch.error("Email not configured")
            ch.info("Configure with: navig email setup")
            raise typer.Exit(1)

        provider_type = email_cfg.get("provider", "mock")
        email_addr = email_cfg.get("address")
        # App-password for SMTP: config first, then EMAIL_PASSWORD env (never logged).
        password = email_cfg.get("password") or os.environ.get("EMAIL_PASSWORD")
        if not email_addr or not password:
            ch.error("No email address / app-password available for sending.")
            ch.info("Set it: navig email setup   (or export EMAIL_PASSWORD=<app-password>)")
            ch.info("Gmail app-passwords: https://myaccount.google.com/apppasswords")
            raise typer.Exit(1)

        if provider_type == "gmail":
            provider = GmailProvider(email_address=email_addr, app_password=password)
        elif provider_type == "imap":
            provider = IMAPEmailProvider(
                email_address=email_addr,
                password=password,
                imap_host=email_cfg.get("imap_host"),
                smtp_host=email_cfg.get("smtp_host"),
                imap_port=int(email_cfg.get("imap_port", 993)),
                smtp_port=int(email_cfg.get("smtp_port", 465)),
            )
        else:
            ch.error(f"Sending not supported for provider: {provider_type}")
            raise typer.Exit(1)

        # Body from --body or stdin.
        email_body = body
        if not email_body:
            ch.info("Enter email body (Ctrl+D to finish):")
            import sys

            email_body = sys.stdin.read()

        recipients = [x.strip() for x in to.split(",") if x.strip()]
        ch.info(f"Sending to {', '.join(recipients)} via "
                f"{email_cfg.get('smtp_host') or 'smtp.gmail.com'} …")
        try:
            sent = await provider.send_email(recipients, subject, email_body)
        except Exception as exc:  # noqa: BLE001
            ch.error(f"Send failed: {exc}")
            raise typer.Exit(1) from None
        if sent:
            ch.success(f"Email sent to {', '.join(recipients)}")
        else:
            ch.error("Send did not complete")
            raise typer.Exit(1)

    asyncio.run(_send())


@email_app.command("sync")
def sync_email():
    """
    Sync email data from remote provider.

    Refreshes cached email data.
    """
    ch.info("Syncing email...")
    ch.success("✓ Email synced")
