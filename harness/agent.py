"""The agent loop: model turn -> tool dispatch -> observation, with approvals and folding."""
import json
import queue
import re
import threading
import time

from . import llm as llm_mod
from . import lms as lms_mod
from . import permissions
from . import schemas
from . import session as session_mod
from . import vision as vision_mod
from .tools import Toolkit

RESULT_LIMIT = 9000
ARGS_LIMIT = 900
ASK_TIMEOUT = 1800.0
SHOTS_PER_TURN = 3
UNITS_PER_TOKEN = 3.0        # условных единиц в одном токене
ASCII_UNIT = 0.75            # латиница и пунктуация: ~4 символа на токен
WIDE_UNIT = 1.6              # кириллица: ~2 символа на токен
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
LOOP_HINT_AT = 2             # столько одинаковых провалов — подсказка «смени аргументы»
LOOP_WEB_AT = 3              # столько — подсказка «поищи в интернете»
LOOP_STOP_AT = 5             # столько — подход срывается (в режиме цели он просто начинается заново)
FAIL_STREAK_HINT = 3         # столько подряд упавших вызовов (даже разных) — тоже подсказка сменить тактику
REPEAT_SEARCH_AT = 3         # столько одинаковых успешных web_search — подсказка сменить запрос или источник
FORMAT_RETRIES = 2            # столько попыток переспросить ответ в правильном формате, прежде чем сдаться


class Bridge:
    """Transport for events and blocking questions. SSE server and CLI subclass it."""

    def __init__(self):
        self.pending = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self._stop_callback = None

    def emit(self, event):
        pass

    def ask(self, kind, payload, timeout=ASK_TIMEOUT):
        key = "%s_%d" % (kind, int(time.time() * 1000000))
        slot = {"event": threading.Event(), "value": None}
        with self.lock:
            self.pending[key] = slot
        self.emit(dict(payload, type=kind, key=key))
        deadline = time.time() + timeout
        while not slot["event"].wait(0.5):
            if self.stop.is_set() or time.time() > deadline:
                break
        with self.lock:
            self.pending.pop(key, None)
        return slot["value"]

    def respond(self, key, value):
        with self.lock:
            slot = self.pending.get(key)
        if not slot:
            return False
        slot["value"] = value
        slot["event"].set()
        return True

    def request_stop(self):
        self.stop.set()
        callback = self._stop_callback
        if callback:
            try:
                callback()
            except Exception:
                pass

    def set_stop_callback(self, callback):
        self._stop_callback = callback


class QueueBridge(Bridge):
    def __init__(self):
        Bridge.__init__(self)
        self.queue = queue.Queue()

    def emit(self, event):
        self.queue.put(event)


def _short(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[ещё %d знаков]" % (len(text) - limit)


class ReasonPump:
    """Coalesces reasoning deltas: token-by-token events would bury the SSE stream and the log."""

    def __init__(self, bridge, limit=160, interval=0.35):
        self.bridge = bridge
        self.limit = limit
        self.interval = interval
        self.buf = ""
        self.sent = 0.0

    def feed(self, piece):
        if not piece:
            return
        self.buf += piece
        now = time.time()
        if len(self.buf) >= self.limit or now - self.sent >= self.interval:
            self.flush(now)

    def flush(self, now=None):
        if not self.buf:
            return
        text, self.buf = self.buf, ""
        self.sent = now or time.time()
        self.bridge.emit({"type": "reason", "text": text})


def _text_units(text):
    """Вес текста в условных токенах: кириллица стоит дороже латиницы."""
    other = len(_NON_ASCII.findall(text))
    return (len(text) - other) * ASCII_UNIT + other * WIDE_UNIT


def _content_units(content):
    text = vision_mod.content_text(content)
    return _text_units(text) + vision_mod.IMAGE_COST * len(vision_mod.content_images(content))


def _estimate_units(messages):
    """Размер истории в условных токенах (схема инструментов считается отдельно)."""
    total = 0.0
    for msg in messages:
        total += _content_units(msg.get("content"))
        for call in msg.get("tool_calls") or []:
            total += _text_units(json.dumps(call, ensure_ascii=False))
    return total


def fold(messages, budget, keep_tail=8):
    """Replace old turns with a digest instead of paying for an extra summariser call."""
    if _estimate_units(messages) <= budget:
        return messages, 0
    head = [m for m in messages[:2] if m.get("role") == "system"]
    rest = messages[len(head):]
    keep_from = max(0, len(rest) - keep_tail)
    old, tail = rest[:keep_from], rest[keep_from:]
    lines = []
    for msg in old:
        text = vision_mod.content_text(msg.get("content")).replace("\n", " ")[:220]
        shots = len(vision_mod.content_images(msg.get("content")))
        if shots:
            text += " [изображений: %d]" % shots
        if msg.get("tool_calls"):
            names = [c["function"]["name"] for c in msg["tool_calls"]]
            text = "вызвал %s; %s" % (",".join(names), text)
        if text:
            lines.append("%s: %s" % (msg.get("role"), text))
    digest = {"role": "system", "content": "КРАТКО О ПРЕДЫДУЩИХ %d ХОДАХ:\n%s" % (len(old), "\n".join(lines[-60:]))}
    return head + [digest] + tail, len(old)


def _is_digest(msg):
    if msg.get("role") != "system":
        return False
    return vision_mod.content_text(msg.get("content")).lstrip().startswith(
        ("ЖЁСТКОЕ СЖАТИЕ:", "КРАТКО О ПРЕДЫДУЩИХ"))


def fold_hard(messages, budget):
    """Жёстко ужать историю и гарантировать, что результат не превышает бюджет.
    Последние наблюдения и исходная задача сохраняются. Старые дайджесты не копятся,
    а сворачиваются в новый, и урезается содержимое, а не сами сообщения: раньше цикл
    урезания выбрасывал свежий хвост, последним сообщением навсегда оставалась исходная
    задача, и модель теряла память о последних шагах."""
    if _estimate_units(messages) <= budget:
        return messages, 0
    head = [m for m in messages if m.get("role") == "system" and not _is_digest(m)]
    prior = [m for m in messages if _is_digest(m)]
    rest = [m for m in messages if m.get("role") != "system"]
    if not rest:
        return messages, 0
    first_user = rest[0] if rest[0].get("role") == "user" else None
    tail = rest[-4:]
    squeezed = []
    if prior:
        # строки прошлого дайджеста переносим в новый, чтобы свёрнутая история не пропала
        text = vision_mod.content_text(prior[-1].get("content"))
        if "СВЁРНУТАЯ ИСТОРИЯ:" in text:
            text = text.split("СВЁРНУТАЯ ИСТОРИЯ:", 1)[1]
        squeezed.extend(line for line in text.splitlines() if line.strip())
    for msg in rest[:-4]:
        text = vision_mod.content_text(msg.get("content")).replace("\n", " ")[:160]
        if msg.get("tool_calls"):
            names = [c.get("function", {}).get("name", "?") for c in msg["tool_calls"]]
            text = "вызвал %s; %s" % (",".join(names), text)
        if text:
            squeezed.append("%s: %s" % (msg.get("role"), text))
    digest = {"role": "system", "content":
              "ЖЁСТКОЕ СЖАТИЕ: старые ходы свёрнуты, чтобы влезть в контекст.\n"
              "ЗАДАЧА И ИТОГИ НИЖЕ ВАЖНЕЕ ИСТОРИИ.\n%s\n\nСВЁРНУТАЯ ИСТОРИЯ:\n%s"
              % (vision_mod.content_text(first_user.get("content"))[:800] if first_user else "(задача потеряна — продолжай по последним наблюдениям)",
                 "\n".join(squeezed[-80:]))}
    out = head + [digest]
    if first_user is not None and not any(m is first_user for m in tail):
        out.append(first_user)
    out.extend(tail)
    # 1. Убираем изображения только из СТАРЫХ сообщений, сохраняя активный скриншот в tail[-1]
    while _estimate_units(out) > budget:
        old_images = [i for i, m in enumerate(out[:-1])
                      if m.get("role") != "system" and m is not first_user
                      and vision_mod.content_images(m.get("content"))]
        if not old_images:
            break
        i = old_images[0]
        out[i] = dict(out[i], content=vision_mod.text_only(out[i].get("content")))

    # 2. Урезаем самое объёмное текстовое содержимое старых сообщений/дайджеста/логов утилит,
    # не трогая исходную задачу пользователя и самое последнее наблюдение
    while _estimate_units(out) > budget:
        candidates = [(_content_units(m.get("content")), i) for i, m in enumerate(out)
                      if m is not first_user and i != len(out) - 1 and (m.get("role") != "system" or _is_digest(m))]
        if not candidates:
            break
        weight, i = max(candidates)
        content = vision_mod.content_text(out[i].get("content"))
        if weight <= 0 or not content:
            break
        excess = _estimate_units(out) - budget
        per_char = weight / max(1, len(content))
        keep = max(0, len(content) - int(excess / per_char) - 1)
        text = content[:keep] + ("…" if keep else "")
        if _content_units(text) >= weight:
            text = content[:keep]      # без многоточия вес гарантированно падает
        out[i] = dict(out[i], content=text)

    # 3. Если вес остался в промежуточных сообщениях — убираем старые сообщения,
    # но исходную задачу и самое свежее наблюдение не трогаем
    while _estimate_units(out) > budget:
        victim = None
        for i, msg in enumerate(out[:-1]):
            if msg.get("role") != "system" and msg is not first_user:
                victim = i
                break
        if victim is None:
            break
        del out[victim]

    # 4. Крайняя мера для изображений: если даже после сжатия текста окно не вмещает активную картинку
    while _estimate_units(out) > budget:
        last_images = [i for i, m in enumerate(out)
                       if m.get("role") != "system" and m is not first_user
                       and vision_mod.content_images(m.get("content"))]
        if not last_images:
            break
        i = last_images[0]
        out[i] = dict(out[i], content=vision_mod.text_only(out[i].get("content")))

    # 5. Финальный предохранитель: если одно оставшееся сообщение превышает бюджет
    while _estimate_units(out) > budget:
        candidates = [(_content_units(m.get("content")), i) for i, m in enumerate(out)]
        if not candidates:
            break
        weight, i = max(candidates)
        content = vision_mod.content_text(out[i].get("content"))
        if weight <= 0 or not content:
            break
        excess = _estimate_units(out) - budget
        per_char = weight / max(1, len(content))
        keep = max(0, len(content) - int(excess / per_char) - 1)
        out[i] = dict(out[i], content=content[:keep])
    return out, max(0, len(messages) - len(out))


def _goal_brief(goal):
    text = "ЦЕЛЬ: %s" % (goal.get("objective") or goal.get("text") or "").strip()
    if goal.get("criteria"):
        text += "\nКРИТЕРИЙ ГОТОВНОСТИ: %s" % str(goal["criteria"]).strip()
    if goal.get("context"):
        text += "\nКонтекст: %s" % str(goal["context"]).strip()
    return text


def _split_checklist(criteria):
    """Критерий-чеклист → отдельные пункты (нумерованные/маркированные строки)."""
    items = []
    for raw in str(criteria or "").splitlines():
        line = re.sub(r"^(?:[-*•]|\d+[.)]|\[\s?[xX ]?\s?\])\s*", "", raw.strip()).strip()
        if line:
            items.append(line)
    return items[:8]


def _checklist_text(items):
    if not items:
        return ""
    lines = ["ЧЕКЛИСТ ЦЕЛИ (отмечай выполненные через check_goal_point):"]
    for i, item in enumerate(items, 1):
        lines.append("%d. [%s] %s" % (i, "x" if item.get("done") else " ", item.get("text", "")))
    return "\n".join(lines)


def _plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _goal_rounds_limit(goal, default):
    """Явный лимит подходов. rounds<=0 означает бесконечно (до finish_goal/Stop/срыва).
    Читаем без `or`, чтобы пользовательский 0 не подменялся дефолтом."""
    value = (goal or {}).get("rounds")
    if value is None or value == "":
        value = default
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


REFINE_PROMPT = (
    "Ты готовишь цель для автономного агента с инструментами (PowerShell, SSH/VPS, файлы, скриншоты). "
    "Переформулируй цель пользователя в чёткую задачу, сохранив весь смысл и реквизиты. "
    "Разбей цель на чеклист из 2-4 измеримых пунктов, каждый проверяется командой, "
    "файлом или скриншотом. Если пользователь дал свой критерий — улучши формулировку, не выбрасывай смысл. "
    "Составь короткий план (3-6 шагов). Ответь СТРОГО одним JSON-объектом без markdown и без раздумий: "
    '{"objective":"...","criteria":"1. ...\\n2. ...","checklist":["пункт 1","пункт 2"],"plan":["шаг 1","шаг 2"]}'
)


def refine_goal(llm, goal, stop=None):
    """Модель сама переформулирует цель и критерий; при любом сбое остаётся исходная формулировка."""
    try:
        raw = llm.complete([{"role": "system", "content": REFINE_PROMPT},
                            {"role": "user", "content": _goal_brief(goal)}], protocol="json", stop=stop)
        blob = llm_mod._balanced_object(llm_mod.strip_reasoning(raw.get("content") or ""))
        if not isinstance(blob, dict):
            return None
        objective = str(blob.get("objective") or "").strip()
        criteria = str(blob.get("criteria") or "").strip()
        if not objective:
            return None
        plan = [str(s).strip() for s in (blob.get("plan") or []) if str(s).strip()][:8]
        checklist = [str(s).strip() for s in (blob.get("checklist") or []) if str(s).strip()][:8]
        out = {"objective": objective}
        if criteria:
            out["criteria"] = criteria
        if checklist:
            out["checklist"] = checklist
        if plan:
            out["plan"] = plan
        return out
    except Exception:
        return None


class Agent:
    def __init__(self, cfg, bridge, sid, toolkit=None):
        self.cfg = cfg
        self.bridge = bridge
        self.sid = sid
        self.tools = toolkit or Toolkit(cfg)
        self.llm = llm_mod.LLM(cfg)
        self.profile = cfg.get("tool_profile", "core")
        self.max_steps = int(cfg.get("max_steps", 40))
        self.window_units = float(cfg.get("context_tokens", 18000)) * UNITS_PER_TOKEN
        self._reserve_cache = {}
        self.squeeze = str(cfg.get("context_mode", "auto"))  # off | auto | hard | always
        self.perm_mode = (cfg.get("permissions") or {}).get("mode", "auto")
        self.goal_rounds = int(cfg.get("goal_rounds", 6))
        self.goal = None
        self.goal_state = None
        self.checklist = []
        self.rounds = 0
        self.speed = {}
        self.repeats = {}
        self.stalled = False
        self.truncated = 0
        self.vision = False
        self._shots_left = 0
        self._pending_shots = []
        self._real_tools = 0
        self._soft_fail = False
        self._fail_streak = 0
        self._loop_tool = ""
        self._loop_count = 0
        self._loop_approaches = 0
        self.repeats_ok = {}
        self._format_retries = 0

    def _watch_loop(self, name, args, result):
        """Слабая локальная модель может вечно тыкать одной и той же упавшей командой.

        Раньше на третий повтор прогон просто умирал. Теперь вместо этого модель получает
        нарастающую подсказку — сменить тактику, а в первую очередь поискать решение в
        интернете: web_search/web_fetch доступны и ничем не ограничены. Обрыв остался
        только как последний рубеж (LOOP_STOP_AT одинаковых провалов подряд).
        """
        soft = self._soft_fail
        self._soft_fail = False
        if soft:
            return ""
        ok = isinstance(result, dict) and result.get("ok", True)
        if ok:
            self._fail_streak = 0
            return self._watch_repeat_ok(name, args)
        self._fail_streak += 1
        key = name + json.dumps(args or {}, sort_keys=True, ensure_ascii=False)[:400]
        self.repeats[key] = self.repeats.get(key, 0) + 1
        n = self.repeats[key]
        if n < LOOP_HINT_AT:
            if self._fail_streak >= FAIL_STREAK_HINT:
                return ("ПОДРЯД %d НЕУДАЧНЫХ ВЫЗОВОВ (%s последний). Этот путь не работает. Смени тактику: "
                        "поищи решение в интернете через web_search (потом web_fetch по найденной ссылке), "
                        "попробуй другой инструмент или другой путь." % (self._fail_streak, name))
            return ""
        self._loop_tool = name
        if n < LOOP_WEB_AT:
            return ("ПОВТОР: %s с такими аргументами уже падал. Возьми другие аргументы "
                    "(например абсолютный путь из сообщения об ошибке) или ответь тем, что уже есть." % name)
        if n < LOOP_STOP_AT:
            if n == LOOP_WEB_AT:
                return ("ЗАЦИКЛИВАНИЕ: %s с одними и теми же аргументами падает уже %d раз — повтор не сработает. "
                        "Смени тактику: найди решение или недостающие факты в интернете через web_search "
                        "(затем web_fetch по найденной ссылке), попробуй другой инструмент или другой путь. "
                        "Если нужный ответ у тебя уже есть — используй его и закрой цель." % (name, n))
            return ("ТЫ СНОВА ПОВТОРЯЕШЬ ТО ЖЕ САМОЕ (%s). Хватит. Первым следующим действием вызови "
                    "web_search с запросом по теме задачи и проверь найденное инструментом." % name)
        self.stalled = True
        self._loop_count = n
        return ("СТОП: %s упал с одними и теми же аргументами %d раз подряд — этот подход нерабочий, "
                "нужен другой (интернет, другой инструмент, другой путь)." % (name, n))

    def _watch_repeat_ok(self, name, args):
        """Тот же успешный поиск повторяется — результат не изменится, а шаги тратятся."""
        if name != "web_search":
            return ""
        query = str((args or {}).get("query") or "")[:120]
        key = name + json.dumps(args or {}, sort_keys=True, ensure_ascii=False)[:400]
        self.repeats_ok[key] = self.repeats_ok.get(key, 0) + 1
        n = self.repeats_ok[key]
        if n < REPEAT_SEARCH_AT:
            return ""
        self._loop_tool = name
        if n < LOOP_STOP_AT:
            return ("ТЫ ПОВТОРЯЕШЬ ТОТ ЖЕ ЗАПРОС (%s) уже %d %s — выдача будет такой же. Смени формулировку, "
                    "уточни запрос или укажи другой источник: engine=rightdao, engine=wikipedia, engine=bing, "
                    "engine=github, engine=stackoverflow. Если нужный факт уже есть в выдаче — используй его."
                    % (query, n, _plural(n, "раз", "раза", "раз")))
        self.stalled = True
        self._loop_count = n
        return ("СТОП: один и тот же поисковый запрос повторён %d %s — измени формулировку или источник."
                % (n, _plural(n, "раз", "раза", "раз")))

    def _goal_nudge(self, turn):
        """Подсказка между подходами цели. Чем глубже модель застряла, тем настойчивее
        отправляем её в интернет: web_search/web_fetch доступны и ничем не ограничены."""
        base = ("ЦЕЛЬ НЕ ЗАКРЫТА: finish_goal не вызван.\n%s\n"
                "Пройди по критерию готовности: каждый невыполненный пункт проверь реальным инструментом "
                "(команда, файл, screenshot для GUI). Доделай недостающее и закрой цель вызовом finish_goal "
                "с evidence из вывода проверки." % _goal_brief(self.goal))
        if self.checklist:
            base += "\n" + _checklist_text(self.checklist)
        query = (self.goal.get("objective") or "").strip()
        quoted = (" по запросу «%s»" % query[:120]) if query else ""
        if turn.get("stalled"):
            tool = self._loop_tool or "инструмент"
            extra = "ПРОШЛЫЙ ПОДХОД УШЁЛ В ПОВТОР НА %s — не повторяй его. " % tool
            if self._loop_approaches >= 2:
                extra += ("Это уже %d-й подход подряд с повтором — хватит пробовать тем же. Следующий подход обязан "
                          "начаться с web_search%s, затем web_fetch по найденной ссылке, и только потом проверка "
                          "инструментом." % (self._loop_approaches, quoted))
            else:
                extra += ("Смени тактику: сначала web_search%s, потом web_fetch по найденной ссылке, "
                          "и только потом проверка инструментом." % quoted)
        elif self.rounds >= 3:
            extra = ("Ты уже %d подход(ов) не закрываешь цель. Если не хватает знаний или фактов — вызови "
                     "web_search%s и web_fetch по лучшей ссылке, потом проверь найденное инструментом. "
                     % (self.rounds, quoted))
        else:
            extra = ""
        return extra + base

    def _system(self, protocol):
        base = schemas.build_system_prompt(self.cfg, self.profile, vision=self.vision)
        if self.vision:
            base += ("\nТы видишь изображения: прикреплённые картинки и скриншоты приходят тебе автоматически. "
                     "После значимых действий с GUI/сайтом делай screenshot (region='window' для конкретного окна), "
                     "читай ошибки и состояние на снимке и делай вывод вслух, прежде чем продолжать.")
        if protocol == "json":
            base += "\n" + schemas.PROTOCOL_INSTRUCTION
        return base

    def _shot_for(self, name, result):
        """Скриншот для vision-модели: не больше SHOTS_PER_TURN за ход, чтобы не забить контекст."""
        if not self.vision or name not in ("screenshot", "game_control", "generate_image") or self._shots_left <= 0:
            return None
        shot_path = (result.get("path") or result.get("screenshot") or result.get("image")) if isinstance(result, dict) else None
        if not isinstance(result, dict) or not result.get("ok") or not shot_path:
            return None
        data_url = vision_mod.encode_path(shot_path)
        if data_url:
            self._shots_left -= 1
        return data_url

    def _tools_for(self, protocol):
        return schemas.native_tools(self.profile) if protocol == "native" else None

    def _tools_units(self, protocol):
        """Вес схемы инструментов: она уходит в каждый запрос."""
        key = ("tools", protocol)
        if key not in self._reserve_cache:
            tools = self._tools_for(protocol) or []
            self._reserve_cache[key] = _text_units(json.dumps(tools, ensure_ascii=False))
        return self._reserve_cache[key]

    def _answer_units(self):
        """Запас под ответ модели: max_tokens тоже места в окне."""
        return float(self.cfg.get("max_tokens", 1500)) * UNITS_PER_TOKEN

    def _reserve_units(self, protocol):
        """Сколько окна занято всегда: схема инструментов + запас под ответ.
        Системный промпт не считаем — он лежит в messages[0] и уже входит в оценку истории."""
        return self._tools_units(protocol) + self._answer_units()

    def _budget_units(self, protocol):
        """Бюджет переписки: окно минус постоянный резерв. На этом пороге включается сжатие."""
        return max(2000.0, self.window_units - self._reserve_units(protocol))

    def _context_event(self, messages, protocol):
        """Снимок заполнения контекста для шкалы в интерфейсе."""
        used = _estimate_units(messages)
        total = used + self._tools_units(protocol)
        window = self.window_units
        answer = self._answer_units()
        toks = lambda units: int(units // UNITS_PER_TOKEN)
        return {"type": "context", "chars": int(total), "tokens": toks(total),
                "window": toks(window), "threshold": toks(window - answer),
                "answer": toks(answer), "pct": int(min(100.0, total * 100.0 / max(1.0, window)))}

    def _delta(self, piece):
        self.bridge.emit({"type": "delta", "text": piece})

    def _speed(self, snap):
        self.speed = snap
        self.bridge.emit(dict(snap, type="speed"))

    def _record_call(self, call):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except ValueError:
            return name, {}, {"ok": False, "error": "кривой JSON в arguments: %s" % _short(call["function"]["arguments"], 300)}
        if name == "ask_user":
            answer = self.bridge.ask("question", {"question": args.get("question") or "",
                                                 "options": args.get("options") or []})
            text = (answer or {}).get("text") or ""
            if not text:
                self._soft_fail = True
            return name, args, {"ok": bool(text), "answered": True, "answer": text or "(пользователь промолчал)"}
        if name == "check_goal_point":
            if not self.goal or not self.checklist:
                return name, args, {"ok": False, "error": "чеклист цели не задан — отмечать нечего"}
            try:
                idx = int(args.get("index"))
            except (TypeError, ValueError):
                idx = 0
            if idx < 1 or idx > len(self.checklist):
                return name, args, {"ok": False, "error": "номер пункта вне чеклиста: 1..%d" % len(self.checklist)}
            done = args.get("done", True)
            done = True if done is None else bool(done)
            evidence = str(args.get("evidence") or "").strip()
            if done and not self._real_tools:
                self._soft_fail = True
                return name, args, {"ok": False, "error":
                    "ПУНКТ НЕ ПРОВЕРЕН: сначала проверь этот пункт реальным инструментом (команда, файл или "
                    "screenshot), затем отметь его с evidence из вывода проверки."}
            if done and not evidence:
                self._soft_fail = True
                return name, args, {"ok": False, "error":
                    "нет evidence: укажи, что именно показала проверка этого пункта."}
            item = self.checklist[idx - 1]
            item["done"] = done
            item["evidence"] = evidence
            remaining = sum(1 for p in self.checklist if not p["done"])
            self.bridge.emit({"type": "goal", "state": "point", "round": self.rounds,
                              "index": idx, "done": done, "evidence": _short(evidence, 300),
                              "points": [dict(p) for p in self.checklist], "remaining": remaining})
            return name, args, {"ok": True, "index": idx, "done": done, "remaining": remaining}
        if name == "finish_goal":
            achieved = bool(args.get("achieved"))
            if achieved and self.goal and not self._real_tools:
                self._soft_fail = True
                return name, args, {"ok": False, "error":
                    "ЦЕЛЬ НЕ ПРОВЕРЕНА: сначала выполни действие и проверь результат реальным инструментом "
                    "(команда, файл или screenshot), потом вызови finish_goal с evidence из вывода проверки."}
            self.goal_state = {"achieved": achieved,
                               "summary": args.get("summary") or "", "evidence": args.get("evidence") or ""}
            remaining = [i for i, p in enumerate(self.checklist, 1) if not p["done"]] if self.checklist else []
            if achieved and remaining:
                self.bridge.emit({"type": "notice", "text":
                    "цель закрыта, но не отмечены пункты чеклиста: %s" % ", ".join(map(str, remaining))})
            self.bridge.emit({"type": "goal", "state": "closed", "round": self.rounds,
                              "objective": (self.goal or {}).get("objective", ""),
                              "achieved": self.goal_state["achieved"],
                              "summary": self.goal_state["summary"],
                              "evidence": _short(self.goal_state["evidence"], 600)})
            return name, args, {"ok": True, "recorded": True, "achieved": self.goal_state["achieved"]}
        self.bridge.emit({"type": "tool_start", "tool": name, "args": _short(args, ARGS_LIMIT)})
        decision, reason = permissions.decide(name, args, self.perm_mode)
        if decision == "deny":
            self._soft_fail = True
            return name, args, {"ok": False,
                                "error": ("ЗАПРЕЩЕНО: %s. Это уронит машину пользователя (перезагрузка, сброс сети), "
                                          "поэтому выполнение заблокировано. Не пробуй обходным путём. "
                                          "Реши задачу иначе: web_fetch/web_search по нужному адресу, другой "
                                          "инструмент или правка конфигурации. Если без этого никак — спроси "
                                          "пользователя через ask_user." % reason)}
        if decision == "confirm":
            reply = self.bridge.ask("approve", {"tool": name, "args": args, "reason": reason,
                                                "preview": _short(args, ARGS_LIMIT)})
            if not reply or not reply.get("allow"):
                self._soft_fail = True
                return name, args, {"ok": False,
                                    "error": "ОТКАЗАНО ПОЛЬЗОВАТЕЛЕМ (%s). Сделай иначе или спроси, что именно нужно." % reason}
        result = self.tools.call(name, args)
        if isinstance(result, dict) and result.get("ok"):
            self._real_tools += 1
        return name, args, result

    def _observe(self, name, args, result):
        image = ((result.get("path") or result.get("screenshot") or result.get("image"))
                 if isinstance(result, dict) and name in ("screenshot", "game_control", "generate_image") else None)
        self.bridge.emit({"type": "tool", "tool": name, "args": _short(args, ARGS_LIMIT),
                          "result": _short(result, RESULT_LIMIT),
                          "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
                          "image": image})
        return json.dumps(result, ensure_ascii=False)[:RESULT_LIMIT]

    def _turn(self, messages, protocol, steps_left):
        steps = 0
        answer = ""
        stopped = False
        failure = ""
        self.stalled = False
        self._shots_left = SHOTS_PER_TURN
        self._pending_shots = []
        self._format_retries = 0
        tools_before = self._real_tools
        while steps < steps_left:
            if self.bridge.stop.is_set():
                self.bridge.emit({"type": "stopped"})
                stopped = True
                break
            steps += 1
            mode = self.squeeze
            budget = self._budget_units(protocol)
            if mode == "off":
                folded, dropped = messages, 0
            elif mode in ("hard", "always"):
                folded, dropped = fold_hard(messages, budget)
            else:  # auto: мягкая свёртка, а если хвост всё равно не влез — жёсткое сжатие
                folded, dropped = fold(messages, budget)
                if _estimate_units(folded) > budget:
                    folded, dropped = fold_hard(messages, budget)
            if dropped:
                self.bridge.emit({"type": "compact", "folded_turns": dropped, "chars": int(_estimate_units(folded))})
            self.bridge.emit(self._context_event(folded, protocol))
            try:
                pump = ReasonPump(self.bridge, limit=80, interval=0.12)
                self.bridge.set_stop_callback(self.llm.stop)
                raw = self.llm.complete(folded, tools=self._tools_for(protocol), protocol=protocol,
                                        stream_cb=self._delta, stats_cb=self._speed, reason_cb=pump.feed,
                                        stop=self.bridge.stop)
                pump.flush()
                thought = (raw.get("reasoning") or "").strip()
                if thought:
                    self.bridge.emit({"type": "thinking", "text": _short(thought, 8000), "chars": len(thought)})
                    raw.pop("reasoning", None)
            except llm_mod.LLMError as exc:
                if self.bridge.stop.is_set():
                    self.bridge.emit({"type": "stopped"})
                    stopped = True
                    break
                if protocol == "native" and self.cfg.get("tool_protocol", "auto") == "auto":
                    self.bridge.emit({"type": "notice",
                                      "text": "нативные инструменты не прошли (%s) — перехожу на JSON-протокол" % _short(str(exc), 200)})
                    protocol = "json"
                    messages[0]["content"] = self._system(protocol)
                    continue
                self.bridge.emit({"type": "error", "text": str(exc)})
                answer = ""
                failure = str(exc)
                stopped = True
                break
            content = raw.get("content") or ""
            calls = raw.get("tool_calls") or []
            if not content.strip() and not calls and raw.get("finish_reason") == "length" and self.truncated < 2:
                self.truncated += 1
                hint = ("Раздумья обрезаны лимитом %s токенов, ответа нет. Думай в 3-5 фраз и сразу "
                        "вызови инструмент или дай готовый ответ." % self.cfg.get("max_tokens", 1500))
                self.bridge.emit({"type": "notice", "text": hint})
                messages.append({"role": "user", "content": hint})
                session_mod.message(self.sid, "user", hint)
                failure = "раздумья обрезаны лимитом токенов"
                continue
            if protocol == "json":
                parsed = llm_mod.parse_json_protocol(content)
                if not parsed["calls"]:
                    if self._retry_format(messages, content, parsed.get("content"), "json"):
                        continue
                    answer = parsed["content"]
                    break
                self._format_retries = 0
                messages.append({"role": "assistant", "content": content})
                session_mod.message(self.sid, "assistant", content)
                if parsed.get("content"):
                    self.bridge.emit({"type": "plan", "text": parsed["content"]})
                blocks = []
                shots = []
                for call in parsed["calls"]:
                    name, args, result = self._record_call(call)
                    hint = self._watch_loop(name, args, result)
                    blocks.append("[%s] -> %s%s" % (name, self._observe(name, args, result),
                                                    ("\n" + hint) if hint else ""))
                    shot = self._shot_for(name, result)
                    if shot:
                        shots.append(shot)
                    if self.stalled:
                        break
                messages.append({"role": "user", "content": vision_mod.make_user_content("\n\n".join(blocks), shots)})
                for block in blocks:
                    session_mod.message(self.sid, "user", block)
                if self.stalled:
                    self.bridge.emit({"type": "notice",
                                      "text": "модель зациклилась на %s — подход оборван, дальше другой тактикой" % name})
                    failure = ("зацикливание: %s упал с одними и теми же аргументами %d раз подряд"
                               % (name, self._loop_count))
                    break
                if self.goal and self.goal_state:
                    answer = self.goal_state["summary"]
                    break
                continue
            if calls:
                self._format_retries = 0
                messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
                session_mod.message(self.sid, "assistant", content or None, tool_calls=calls)
                if content:
                    self.bridge.emit({"type": "plan", "text": content})
                for call in calls:
                    name, args, result = self._record_call(call)
                    text = self._observe(name, args, result)
                    hint = self._watch_loop(name, args, result)
                    if hint:
                        text += "\n" + hint
                    shot = self._shot_for(name, result)
                    if shot:
                        self._pending_shots.append(shot)
                        text += "\nИзображение придёт следующим сообщением пользователя — дождись его, не делай выводов вслепую."
                    messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": text})
                    session_mod.message(self.sid, "tool", text)
                    if self.stalled:
                        break
                if self.stalled:
                    self.bridge.emit({"type": "notice",
                                      "text": "модель зациклилась на %s — подход оборван, дальше другой тактикой" % name})
                    failure = ("зацикливание: %s упал с одними и теми же аргументами %d раз подряд"
                               % (name, self._loop_count))
                    break
                if self.goal and self.goal_state:
                    answer = self.goal_state["summary"]
                    break
                continue
            if self._pending_shots:
                # строгие jinja-шаблоны не принимают user-сообщение сразу после tool-результатов,
                # поэтому скриншоты отдаём после промежуточного ответа модели
                shots, self._pending_shots = self._pending_shots, []
                messages.append({"role": "assistant", "content": content or ""})
                session_mod.message(self.sid, "assistant", content or "")
                note = "Скриншоты из последних вызовов инструментов. Посмотри на них и продолжай задачу:"
                messages.append({"role": "user", "content": vision_mod.make_user_content(note, shots)})
                session_mod.message(self.sid, "user", note)
                self.bridge.emit({"type": "user", "text": note, "images": len(shots)})
                continue
            if self._retry_format(messages, content, content, "native"):
                continue
            answer = content
            break
        return {"answer": answer, "steps": steps, "protocol": protocol, "stopped": stopped,
                "failure": failure, "stalled": self.stalled, "real_tools": self._real_tools - tools_before}

    def _retry_format(self, messages, content, parsed_text, protocol="json"):
        """Модель вместо ответа переписала наблюдения или выдала пустоту.

        Раньше такой ход просто закрывал прогон пустым ответом. Теперь харнес
        говорит ей нужный формат и даёт ещё попытку — прогон не должен вставать
        из-за того, что модель сбилась с протокола.
        """
        text = (parsed_text or "").strip()
        echoed = bool(re.search(r"(^|\n)\s*user:\s*\[|(^|\n)\s*\[(web_fetch|web_search|read_file|shell|browser)\]\s*->",
                                content or "", re.I))
        if text and not echoed:
            return False
        if self._format_retries >= FORMAT_RETRIES:
            return False
        self._format_retries += 1
        if protocol == "json":
            hint = ("Твой последний ответ не распознан. Ответь РОВНО одним JSON-объектом без пояснений: "
                    "{\"tool\":\"<имя_инструмента>\",\"args\":{...}} — или {\"final\":\"<ответ>\"}. "
                    "Не переписывай наблюдения из предыдущих сообщений и не выдумывай адреса — "
                    "бери факты из того, что уже вернули инструменты.")
        else:
            hint = ("Твой последний ответ не распознан как ответ по задаче. Дай СРАЗУ готовый текст ответа "
                    "и не переписывай наблюдения из предыдущих сообщений — бери факты из того, что уже "
                    "вернули инструменты.")
        self.bridge.emit({"type": "notice", "text": hint})
        shown = (content or "").strip()
        if shown:
            if len(shown) > 1500:
                shown = shown[:1500] + "…"
            messages.append({"role": "assistant", "content": shown})
        messages.append({"role": "user", "content": hint})
        session_mod.message(self.sid, "user", hint)
        return True

    def _sync_context_window(self):
        """Автоопределение размера контекста из LM Studio и адаптация порогов сжатия на лету."""
        try:
            detected = self.llm.detect_context_length(self.llm.model)
            if detected and detected >= 2048:
                old_units = self.window_units
                self.window_units = float(detected) * UNITS_PER_TOKEN
                self._reserve_cache.clear()
                if abs(self.window_units - old_units) > 100:
                    self.bridge.emit({
                        "type": "notice",
                        "text": "контекст модели определён автоматически: %d токенов (окно адаптировано)" % detected
                    })
        except Exception:
            pass

    def run(self, user_text, history=None, goal=None, image=None, images=None):
        self._sync_context_window()
        protocol = self.cfg.get("tool_protocol", "auto")
        if protocol == "auto":
            protocol = self.llm.probe_protocol(schemas.native_tools(self.profile))
        self.vision = vision_mod.supports_vision(self.cfg, self.llm.model)
        messages = [{"role": "system", "content": self._system(protocol)}]
        hist = history or []
        if not self.vision:
            hist = [dict(m, content=vision_mod.text_only(m.get("content")))
                    if isinstance(m.get("content"), list) else m for m in hist]
        messages += hist
        self.goal = goal or None
        self.goal_state = None
        self.checklist = []
        self.rounds = 0
        self.repeats = {}
        self.repeats_ok = {}
        self.stalled = False
        self.truncated = 0
        self._real_tools = 0
        if self.goal:
            self.bridge.emit({"type": "notice", "text": "уточняю цель и критерий готовности…"})
            refined = refine_goal(self.llm, self.goal, stop=self.bridge.stop)
            if refined:
                self.goal = dict(self.goal, **refined)
                self.bridge.emit({"type": "goal", "state": "refined", "round": 0,
                                  "objective": self.goal.get("objective") or "",
                                  "criteria": self.goal.get("criteria") or "",
                                  "plan": refined.get("plan") or []})
                if refined.get("plan"):
                    self.bridge.emit({"type": "plan", "text": "План: " + " → ".join(refined["plan"])})
            points = []
            if refined and refined.get("checklist"):
                points = list(refined["checklist"])
            elif self.goal.get("criteria"):
                points = _split_checklist(self.goal["criteria"])
            if not points and (self.goal.get("objective") or "").strip():
                points = [self.goal["objective"].strip()]
            self.checklist = [{"text": p, "done": False, "evidence": ""} for p in points[:8]]
            messages.append({"role": "system", "content": schemas.GOAL_INSTRUCTION})
            user_text = _goal_brief(self.goal)
            if self.checklist:
                user_text += "\n" + _checklist_text(self.checklist)
            self.bridge.emit({"type": "goal", "state": "started", "round": 0,
                              "objective": self.goal.get("objective") or "",
                              "criteria": self.goal.get("criteria") or "",
                              "rounds": _goal_rounds_limit(self.goal, self.goal_rounds)})
            if self.checklist:
                self.bridge.emit({"type": "goal", "state": "checklist", "round": 0,
                                  "objective": self.goal.get("objective") or "",
                                  "points": [dict(p) for p in self.checklist]})
        user_content = user_text
        images_count = 0
        raw_images = [i for i in (list(images or []) + ([image] if image else [])) if i]
        data_urls = []
        for item in raw_images:
            data_url = vision_mod.normalize_image(item)
            if not data_url:
                self.bridge.emit({"type": "notice",
                                  "text": "не удалось прочитать изображение (допустимы png/jpeg/webp/gif до 10 МБ)"})
            else:
                data_urls.append(data_url)
        if data_urls:
            if not self.vision:
                self.bridge.emit({"type": "notice", "text": "модель без vision — %d картинки не переданы, отправлен только текст"
                                  % len(data_urls)})
            else:
                user_content = vision_mod.make_user_content(user_text, data_urls)
                images_count = len(data_urls)
        messages.append({"role": "user", "content": user_content})
        session_mod.message(self.sid, "user", user_text)
        session_mod.set_title_if_empty(self.sid, user_text)
        self.bridge.emit({"type": "user", "text": user_text, "images": images_count})
        rounds_limit = _goal_rounds_limit(self.goal, self.goal_rounds) if self.goal else 1
        infinite = self.goal is not None and rounds_limit <= 0
        idle_rounds = 0
        idle_limit = max(2, int(self.cfg.get("goal_idle_limit", 4)))
        total_steps = 0
        turn = {"answer": "", "steps": 0, "protocol": protocol, "stopped": False, "failure": "", "stalled": False}
        while True:
            left = max(1, self.max_steps)
            turn = self._turn(messages, turn["protocol"], left)
            total_steps += turn["steps"]
            protocol = turn["protocol"]
            if turn["stopped"] or not self.goal:
                break
            if self.goal_state:
                break
            if turn.get("stalled"):
                # зацикливание больше не убивает цель: подход обрывается, счётчики повторов
                # обнуляются, а следующий подход получает другую тактику (см. _goal_nudge)
                self._loop_approaches += 1
                self.repeats = {}
                self.repeats_ok = {}
                self._fail_streak = 0
                self.bridge.emit({"type": "notice",
                                  "text": "подход зациклился на %s — начинаю новый подход с другой тактикой"
                                          % (self._loop_tool or "инструменте")})
            self.rounds += 1
            if not infinite and self.rounds >= rounds_limit:
                self.bridge.emit({"type": "goal", "state": "exhausted", "round": self.rounds,
                                  "objective": self.goal.get("objective") or "",
                                  "summary": "лимит из %d подходов исчерпан, finish_goal не вызван" % rounds_limit})
                break
            # предохранитель бесконечного режима: если модель несколько подходов подряд
            # не вызвала ни одного реального инструмента и не закрыла цель — она застряла
            # на болтовне, а не работает. Останавливаемся честно, а не жжём токены вечно.
            if infinite:
                idle_rounds = 0 if turn.get("real_tools") else idle_rounds + 1
                if idle_rounds >= idle_limit:
                    self.bridge.emit({"type": "goal", "state": "exhausted", "round": self.rounds,
                                      "objective": self.goal.get("objective") or "",
                                      "summary": "нет прогресса: %d подходов подряд без действий и без finish_goal. "
                                                 "Похоже, нужен другой источник знаний — попробуй web_search по теме задачи "
                                                 "или задай вопрос пользователю." % idle_limit})
                    break
            nudge = self._goal_nudge(turn)
            messages.append({"role": "user", "content": nudge})
            session_mod.message(self.sid, "user", nudge)
            # между подходами цели можно сжать историю сильнее — предыдущий подход уже видно в digest
            if self.squeeze in ("auto", "hard", "always"):
                messages[:], _ = fold_hard(messages, self._budget_units(protocol))
            self.bridge.emit({"type": "goal", "state": "continue", "round": self.rounds,
                              "steps": total_steps, "objective": self.goal.get("objective") or "",
                              "summary": _short(turn.get("answer") or "", 400)})
        answer = turn["answer"]
        if not answer:
            if self.bridge.stop.is_set():
                answer = "Остановлено. Успел сделать %d шагов, спроси детали." % total_steps
            elif turn["failure"]:
                answer = ("Модель не ответила: %s" % _short(turn["failure"], 400)
                          + (" Попробуй сам поискать решение через web_search или уточни запрос."
                             if "зацикливание" in turn["failure"] else ""))
            elif self.goal_state:
                answer = self.goal_state["summary"]
            else:
                answer = "Модель за %d шагов не дала ответа — скорее всего уперлась в контекст или в недоступный инструмент." % total_steps
        messages.append({"role": "assistant", "content": answer})
        session_mod.message(self.sid, "assistant", answer)
        done = {"type": "done", "text": answer, "steps": total_steps, "protocol": protocol,
                "speed": self.speed, "rounds": self.rounds + (1 if self.goal_state else 0)}
        if self.goal:
            done["goal"] = {"achieved": bool(self.goal_state and self.goal_state["achieved"]),
                            "summary": (self.goal_state or {}).get("summary", "")}
        try:
            if self.cfg.get("provider") == "lmstudio":
                lms_mod.invalidate()
        except Exception:
            pass
        self.bridge.emit(done)
        return {"answer": answer, "steps": total_steps, "messages": messages[1:], "protocol": protocol,
                "speed": self.speed, "goal": self.goal_state}
