"""Uppdateringar: frågar GitHub efter senaste releasen och installerar dess MSI åt användaren.

Det enda swpt hämtar från nätet. Inget om datorn eller filerna skickas – bara en fråga om
senaste versionen. Installationen är per användare, så den kräver inte administratörsbehörighet.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

from version import VERSION

REPO = "YamYam-ee/swpt"
API = os.environ.get("SWPT_UPDATE_API") or f"https://api.github.com/repos/{REPO}/releases/latest"
INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Programs", "swpt")
CHECK_EVERY = 24 * 3600 * 1000   # ms


def current():
    return os.environ.get("SWPT_VERSION") or VERSION


def _parse(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def supported():
    """Uppdatering går bara för den byggda appen (inte när swpt körs från källkoden)."""
    return getattr(sys, "frozen", False) or "SWPT_UPDATE_API" in os.environ


def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "swpt/" + current(),
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def check():
    """Senaste releasen om den är nyare än den här: dict(version, url, sha256, size, page), annars None."""
    with _get(API, 15) as r:
        rel = json.load(r)
    if rel.get("draft") or rel.get("prerelease"):
        return None
    version = rel.get("tag_name", "").lstrip("v")
    if not version or _parse(version) <= _parse(current()):
        return None
    for a in rel.get("assets", []):
        if a.get("name", "").lower().endswith(".msi"):
            digest = a.get("digest") or ""
            return {"version": version, "url": a["browser_download_url"], "size": a.get("size") or 0,
                    "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
                    "page": rel.get("html_url", "")}
    return None


def download(info, progress=None):
    """Laddar ner MSI:n till en tempmapp och kontrollerar kontrollsumman. Returnerar sökvägen."""
    folder = os.path.join(tempfile.gettempdir(), "swpt-update")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"swpt-{info['version']}.msi")
    h, got = hashlib.sha256(), 0
    with _get(info["url"], 30) as r, open(path + ".part", "wb") as f:
        total = int(r.headers.get("Content-Length") or info["size"] or 0)
        while chunk := r.read(1 << 16):
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if progress and total:
                progress(got / total)
    if info["sha256"] and h.hexdigest() != info["sha256"]:
        os.remove(path + ".part")
        raise ValueError("checksum mismatch")
    os.replace(path + ".part", path)
    return path


_INSTALL_SCRIPT = r"""
param([int]$WaitPid, [string]$Msi, [string]$NewExe, [string]$OldExe)
Start-Transcript -Path ([IO.Path]::ChangeExtension($Msi, ".txt")) -Force | Out-Null
Wait-Process -Id $WaitPid -Timeout 60 -ErrorAction SilentlyContinue
$log = [IO.Path]::ChangeExtension($Msi, ".log")
$p = Start-Process msiexec.exe -ArgumentList "/i `"$Msi`" /passive /norestart /l*v `"$log`"" -Wait -PassThru
"msiexec exit code: $($p.ExitCode)"
if ($p.ExitCode -eq 0 -or $p.ExitCode -eq 3010) { Start-Process $NewExe } elseif ($OldExe) { Start-Process $OldExe }
Stop-Transcript | Out-Null
"""


def install(msi):
    """Väntar tills swpt har avslutats, installerar (med ett litet förloppsfönster, utan frågor) och
    startar nya versionen. Misslyckas installationen startas den gamla igen. Logg: <msi>.txt / .log."""
    script = os.path.join(os.path.dirname(msi), "install.ps1")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(_INSTALL_SCRIPT)
    old_exe = sys.executable if getattr(sys, "frozen", False) else ""
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", script, "-WaitPid", str(os.getpid()), "-Msi", msi,
                      "-NewExe", os.path.join(INSTALL_DIR, "swpt.exe"), "-OldExe", old_exe],
                     creationflags=0x08000000 | 0x00000200)   # inget fönster, egen processgrupp
