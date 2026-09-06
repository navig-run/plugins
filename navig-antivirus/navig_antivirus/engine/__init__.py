"""navig-antivirus engine — stdlib-only scan/recovery/system-scan logic.

Split so the Typer command layer stays thin:
  * browsers      — Chromium-family extension malware/PUP scan + profile-index recovery
  * registry_scan — Chrome extension-registry force-install PUP audit (Windows/winreg)
  * system_scan   — Windows Defender on-demand scan + Malwarebytes detection
"""
