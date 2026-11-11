"""Guardrails for full-auto mode: nothing is refused outright, it is escalated."""
import json
import re

# Команды, которые уносят машину: перезагрузка, выключение, сброс сетевого стека.
# Их нельзя пропускать даже в режиме unrestricted — агент уронит сессию пользователя.
HARD_BLOCK = [
    (r"\brestart-computer\b|\bstop-computer\b|\bshutdown(\.exe)?\s+/[rsp]\b"
     r"|\bwmic\b[^;]*\bcall\s+(reboot|shutdown)\b|\bexitwindows\b|\brundll32\b[^;]*\bshutdown\b",
     "перезагрузка/выключение компьютера"),
    (r"\bwinsock\s+reset\b|\bnetsh\s+interface\s+(reset|delete)\b",
     "сброс сетевого стека (нужна перезагрузка)"),
    (r"\bbcdedit\b|\bformat(\.exe)?\s+[A-Za-z]:|\bdiskpart\b|\bmkfs\b|\bfdisk\b",
     "разрушительные изменения загрузки или дисков"),
]

SHELL_TOOLS = ("shell", "run_command", "powershell", "cmd", "bash", "sh", "console")

SHELL_RULES = [
    (r"\brm\s+(-[a-zA-Z]+\s+)*-?[a-zA-Z]*[rf][a-zA-Z]*\s+[\"']?(?:[A-Za-z]:[\\/](?:Windows|Program Files|Users|PerfLogs|\$)|/(?:bin|etc|usr|var|home|root|boot)\b)",
     "рекурсивное удаление системного каталога"),
    (r"\b(rmdir|del|erase)\b.*\s[\"']?[A-Za-z]:[\\/](?:Windows|Program Files|Users)[\\/]?[\"']?\s*\*?/?",
     "удаление внутри системного каталога"),
    (r"Remove-Item\b[^;]*-(Recurse|Force)[^;]*[\"']?(?:C:)?\\?(?:Windows|Program Files|Users|System32)",
     "Remove-Item по системному дереву"),
    (r"\bformat(\.exe)?\s+[A-Za-z]:", "форматирование диска"),
    (r"\b(diskpart|mkfs|fdisk|dd\s+if=)\b", "работа с разделами/сырыми дисками"),
    (r"\bvssadmin\s+delete|\bcipher\s+/w", "зачистка теневых копий"),
    (r"\bbcdedit\b|\bschtasks\s+/delete\b|\bshutdown\b|\breboot\b", "загрузка/планировщик"),
    (r"reg\s+(delete|add)\s+H(KLM|KU|CC)\\[^\s]*(Run|Image File Execution|Policies)", "правки автозагрузки и политик в реестре"),
    (r"Set-MpPreference.*(Disable|ExclusionPath)|Remove-Item.*Microsoft\.Windows\s*Defender|sc\s+(config|delete)\s+(WinDefend|SecurityHealthService)",
     "отключение антивируса или защитника"),
    (r"\bnet\s+user\s+\S+\s+(delete|add)|net\s+localgroup\s+administrators\s+\S+\s+(delete|add)", "изменение учётных записей"),
    (r"winsock\s+reset|\bnetsh\s+interface\s+(reset|delete)", "сброс сетевого стека (нужна перезагрузка)"),
    (r"git\s+push\b.*(\s--force|\s-f)\b", "force-push"),
    (r"git\s+(reset\s+--hard|clean\s+-[fd])", "git reset --hard / clean -fd"),
    (r"(curl|wget|Invoke-WebRequest)[^|]*\|\s*(ba)?sh", "pipes-to-shell"),
    (r"\breg\s+delete\s+H(KLM|CC)\b", "удаление ключей реестра"),
]

PROTECTED_PROCESSES = (
    "csrss.exe", "wininit.exe", "winlogon.exe", "smss.exe", "services.exe",
    "lsass.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "memory compression",
)

SYSTEM_PATHS = (
    "c:\\windows", "c:\\program files", "c:\\program files (x86)", "c:\\programdata",
)


def _norm(path):
    # a bare "\\" as a re.sub replacement string is an invalid escape template, so use a function
    return re.sub(r"[\\/]+", lambda _m: "\\", (path or "").lower()).strip()


def _scan(text, rules):
    low = (text or "").lower()
    for pattern, reason in rules:
        if re.search(pattern, low, re.I):
            return reason
    return None


CONFIRM_TOOLS = ("shell", "powershell", "ssh", "ssh_upload", "ssh_download", "write_file", "edit_file", "python", "kill_process", "download", "lmstudio", "train_model")

PLUGIN_DANGEROUS = set()


def decide(name, args, mode="auto"):
    args = args or {}
    if name in SHELL_TOOLS:
        reason = _scan(args.get("cmd") or args.get("command") or args.get("script") or "", HARD_BLOCK)
        if reason:
            return "deny", reason
    if name == "python":
        reason = _scan(args.get("code") or "", HARD_BLOCK)
        if reason:
            return "deny", reason
    if mode == "unrestricted":
        return "allow", None
    if mode == "ask" and name in CONFIRM_TOOLS:
        return "confirm", "режим «спрашивать перед каждым шагом»: %s" % name
    if name in PLUGIN_DANGEROUS:
        return "confirm", "плагин-инструмент %s помечен как опасный" % name
    if name in ("shell", "run_command", "powershell"):
        reason = _scan(args.get("cmd") or args.get("command") or "", SHELL_RULES)
        if reason:
            return "confirm", "потенциально опасная команда: " + reason
    if name in ("write_file", "edit_file", "create_file", "patch_file"):
        target = _norm(args.get("path") or args.get("file") or "")
        for base in SYSTEM_PATHS:
            if target.startswith(base):
                return "confirm", "запись в системный каталог: %s" % target[:80]
    if name == "kill_process":
        ident = str(args.get("name") or args.get("image") or "").lower()
        if ident in PROTECTED_PROCESSES:
            return "confirm", "системный процесс %s — остановка уронит Windows" % ident
    if name == "python":
        reason = _scan(args.get("code") or "", SHELL_RULES)
        if reason:
            return "confirm", "python-код похож на разрушительное действие: " + reason
    return "allow", None