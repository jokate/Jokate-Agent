"""Tell the user when a relay needs them: finished, failed, waiting for approval, stopped at a budget.

Windows: a native toast through PowerShell (no extra package); clicking it opens the run in the dashboard.
Elsewhere (or if the toast fails) nothing is shown here — the dashboard still shows its own alert.
Never raises: a notification must not break a run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from xml.sax.saxutils import escape

# kind -> (icon, label)
NOTIFY_KINDS = {
    "run_done": ("✅", "완료"),
    "run_failed": ("💥", "실패"),
    "awaiting_approval": ("✋", "승인 필요"),
    "stage_budget_exceeded": ("💸", "단계 비용 한도 — 승인 필요"),
    "budget_exceeded": ("💸", "실행 비용 한도 — 승인 필요"),
}

# The toast is shown under PowerShell's registered app id (an unregistered id is silently dropped on Win10/11).
# Title/body/url come in through environment variables, so nothing user-written is ever parsed as script.
PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:KATAE_TOAST_XML)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
  [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def toast_xml(title: str, body: str, url: str) -> str:
    return (f'<toast activationType="protocol" launch="{escape(url, {chr(34): "&quot;"})}">'
            f'<visual><binding template="ToastGeneric"><text>{escape(title)}</text><text>{escape(body)}</text>'
            f"</binding></visual></toast>")


class Notifier:
    def __init__(self, server_url: str = "http://127.0.0.1:8020", enabled: bool = True):
        self.server_url = server_url.rstrip("/")
        self.enabled = enabled and sys.platform == "win32"

    def __call__(self, run_id: str, kind: str, title: str, detail: dict) -> None:
        if not self.enabled or kind not in NOTIFY_KINDS:
            return
        icon, label = NOTIFY_KINDS[kind]
        body = (detail.get("error") or detail.get("reason") or "").strip()
        if kind == "awaiting_approval" and not body:
            body = f"다음 단계: {detail.get('next', '')} — 대시보드에서 확인 후 승인"
        xml = toast_xml(f"{icon} Agent 카태 · {label}", f"{title[:80]}\n{body[:160]}".strip(),
                        f"{self.server_url}/?run={run_id}")
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", PS_TOAST],
                env={**os.environ, "KATAE_TOAST_XML": xml},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            pass
