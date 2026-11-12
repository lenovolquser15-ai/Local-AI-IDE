"""Terminal front-end: python -m harness cli [запрос]"""
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from . import schemas
from .agent import Agent, Bridge
from .config import load
from . import session as session_mod
from .llm import LLM
from .stdout_fix import force_utf8

force_utf8()
console = Console()


class ConsoleBridge(Bridge):
    def emit(self, event):
        kind = event.get("type")
        if kind == "plan":
            console.print("[dim italic]→ %s[/dim italic]" % one_line(event.get("text")))
        elif kind == "reason":
            console.print("[dim]%s[/dim]" % (event.get("text") or "").replace("\n", " "), end="")
        elif kind == "thinking":
            console.print("[dim]  ‹размышление: %s знаков›[/dim]" % event.get("chars"))
        elif kind == "tool":
            color = "green" if event.get("ok") else "red"
            console.print("[%s]▸ %s[/] [dim]%s[/]" % (color, event.get("tool"), one_line(event.get("args"), 160)))
            body = one_line(event.get("result"), 900)
            if body:
                console.print(Panel(body, border_style=color, title=event.get("tool"), title_align="left"))
        elif kind == "notice":
            console.print("[yellow]· %s[/yellow]" % one_line(event.get("text")))
        elif kind == "compact":
            console.print("[yellow]· контекст сжат: свернуто %d ходов[/yellow]" % event.get("folded_turns", 0))
        elif kind == "speed":
            if event.get("tps"):
                console.print("[dim]  ⚡ %s ток/с · %s ток · старт %s с[/dim]" % (
                    event["tps"], event.get("tokens", 0), event.get("ttft", "?")), end="\r")
        elif kind == "goal":
            console.print("[yellow]◆ цель [%s] %s[/yellow]" % (event.get("state"), one_line(
                event.get("summary") or event.get("objective") or "", 200)))
        elif kind == "error":
            console.print("[red]✗ %s[/red]" % one_line(event.get("text")))
        elif kind == "stopped":
            console.print("[red]■ остановлено[/red]")

    def ask(self, kind, payload, timeout=None):
        if kind == "approve":
            console.print(Panel("[bold]%s[/bold]\n%s" % (payload.get("reason"), json.dumps(payload.get("args"), ensure_ascii=False)[:1200]),
                                title="опасное действие", border_style="yellow"))
            answer = console.input("[yellow]разрешить? (y/N) [/yellow]").strip().lower()
            return {"allow": answer in ("y", "да", "+")}
        question = payload.get("question") or ""
        if payload.get("options"):
            console.print("[cyan]" + question + "[/cyan]")
            for i, opt in enumerate(payload["options"], 1):
                console.print("  %d) %s" % (i, opt))
            choice = console.input("[cyan]выбор [/cyan]").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(payload["options"]):
                return {"text": str(payload["options"][int(choice) - 1])}
            return {"text": choice}
        return {"text": console.input("[cyan]%s[/cyan] " % question)}


def one_line(value, limit=700):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    text = " ".join((value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def preflight(cfg):
    llm = LLM(cfg)
    try:
        models = llm.models()
    except Exception as exc:
        console.print("[red]локальный сервер не отвечает:[/red] %s" % exc)
        console.print("нужно запустить LM Studio → Developer → Start Server, либо `lh wake`")
        return None
    console.print("[green]✓ %s[/green] · моделей: %d · %s" % (llm.label, len(models), cfg["providers"][cfg["provider"]]["base_url"]))
    if not cfg.get("model") and models:
        console.print("[dim]модель по умолчанию:[/dim] %s" % models[0])
    return models


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = load()
    if argv and argv[0] in ("check", "preflight"):
        return 0 if preflight(cfg) else 1
    if argv and argv[0] == "tools":
        profile = argv[1] if len(argv) > 1 else cfg.get("tool_profile", "core")
        for spec in schemas.entries(profile):
            console.print("[bold cyan]%s[/bold cyan] — %s" % (spec["name"], spec["desc"]))
        return 0
    bridge = ConsoleBridge()
    sid = session_mod.create("")
    history = []
    agent = Agent(cfg, bridge, sid)
    if argv and argv[0] == "goal":
        objective, _, criteria = " ".join(argv[1:]).partition("==")
        console.print(Markdown("## LocalHarness · режим цели")); preflight(cfg)
        result = agent.run(objective.strip(), history=history,
                           goal={"objective": objective.strip(), "criteria": criteria.strip()})
        console.print(Panel(Markdown(result["answer"] or ""),
                            title="цель %s" % ("достигнута" if (result.get("goal") or {}).get("achieved") else "не достигнута"),
                            border_style="green" if (result.get("goal") or {}).get("achieved") else "yellow"))
        return 0
    if argv and argv[0] not in ("chat",):
        prompt = " ".join(argv)
        console.print(Markdown("## LocalHarness")); preflight(cfg)
        result = agent.run(prompt, history=history)
        history = result["messages"]
        console.print(Panel(Markdown(result["answer"] or ""), title="ответ", border_style="blue"))
        return 0
    console.print(Markdown("# LocalHarness\nлокальный агент на твоей модели. `/goal <цель> == <критерий>` — работать до цели, "
                           "`/exit` — выйти, `/stop` — прервать."))
    if not preflight(cfg):
        return 1
    while True:
        try:
            prompt = console.input("\n[bold blue]ты ›[/bold blue] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not prompt:
            continue
        if prompt in ("/exit", "/quit"):
            break
        if prompt.startswith("/goal "):
            objective, _, criteria = prompt[6:].partition("==")
            result = agent.run(objective.strip(), history=history,
                               goal={"objective": objective.strip(), "criteria": criteria.strip()})
            history = result["messages"]
            console.print(Markdown(result["answer"] or ""))
            continue
        if prompt == "/stop":
            bridge.request_stop()
            continue
        if prompt == "/new":
            history = []
            sid = session_mod.create("")
            agent = Agent(cfg, bridge, sid)
            continue
        if prompt == "/tools":
            for spec in schemas.entries(cfg.get("tool_profile", "core")):
                console.print("[bold cyan]%s[/bold cyan] — %s" % (spec["name"], spec["desc"]))
            continue
        result = agent.run(prompt, history=history)
        history = result["messages"]
        console.print(Markdown(result["answer"] or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
