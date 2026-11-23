"""Short acknowledgements prepared from the request while the model works."""
import re

# Voice transcripts can include a greeting or filler before a direct request.
RESEARCH_RU = re.compile(
    r"\b(?:(?:проведи|сделай|подготовь|выполни|собери|нуж(?:ен|но))\b"
    r"(?:\s+\w+){0,4}?\s+(?P<kind>рес[её]рч|исследовани[ея]|анализ|разбор|обзор|отч[её]т)"
    r"|(?P<verb>исследуй|проанализируй|разбери подробно))\b\s*[:«]?[ \t]*(?P<topic>.+)", re.I,
)
RESEARCH_EN = re.compile(
    r"\b(?:(?:do|conduct|prepare|write|run|give me)\s+(?:\w+\s+){0,5}?"
    r"(?:research|analysis|deep dive|report)(?:\s+(?:on|of|into|about))?"
    r"|(?:^|(?<=\bplease )|(?<=\byou ))(?:research|analy[sz]e))\s+(?P<topic>.+)", re.I,
)
COMPARE_RU = re.compile(r"\b(?:сравни|сопоставь)\b\s+(?:подробно\s+)?(?P<topic>.+)", re.I)
COMPARE_EN = re.compile(r"\b(?:compare|evaluate)\s+(?P<topic>.+)", re.I)
DRAFT_RU = re.compile(
    r"\b(?:составь|напиши|подготовь|собери)\s+(?:мне\s+)?"
    r"(?P<topic>(?:подробн|детальн|разв[её]рнут)\w*\s+.+)", re.I,
)
DRAFT_EN = re.compile(
    r"\b(?:draft|write|prepare|build)\s+(?P<topic>(?:(?:a|an|the)\s+)?(?:detailed|comprehensive|in-depth)\s+.+)", re.I,
)
MARKET = re.compile(r"\b(?:рын\w*|отрасл\w*|market\w*|industr(?:y|ies))\b", re.I)
DEFERRED = re.compile(
    r"\b(?:напомни|запланируй|поставь\s+напоминание|remind me|schedule (?:a|the|this|it|for))\b"
    r"|(?<!на )(?<!for )\b(?:завтра|послезавтра|tomorrow|next (?:week|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"
    r"|\bчерез\s+(?:\d+|один|одну|два|две|три|пять|неделю)\b"
    r"|\b(?:в|at)\s+\d{1,2}(?::|\.)\d{2}\b", re.I,
)
QUICK = re.compile(r"(?:^|\s)(?:кратко|коротко|в двух словах|одной фразой|briefly|quickly|in one sentence)(?:\s|[,.?!]|$)", re.I)
SMALL_TALK = re.compile(r"(?:привет|здравствуй(?:те)?|спасибо(?: большое)?|ок(?:ей)?|hi|hey|hello|thanks|thank you|okay|ok)[ .!,…]*", re.I)
DETAIL = re.compile(r"\b(?:подробн\w*|глубок\w*|детальн\w*|разв[её]рнут\w*|detailed|comprehensive|in-depth)\b", re.I)


def request_text(query):
    """Use the user's words, excluding text extracted or quoted from attachments."""
    original = re.split(r"\n\n\[(?:Attached document content|Files for this request|Цитата из сообщения, на которое дан ответ)\]", query, maxsplit=1)[0]
    return " ".join(original.split())


def skip_ack(text):
    return bool(DEFERRED.search(text) or QUICK.search(text) or SMALL_TALK.fullmatch(text))


def _topic(raw):
    # Keep the user's subject and qualifiers, but let later sentences remain part of the actual answer.
    value = re.split(r"[?!]|\.(?=\s)|[,;:]\s", raw, maxsplit=1)[0].strip(" .«»“”\"")
    if len(value) > 110:
        value = value[:109].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return value


def immediate_ack(text, russian):
    compare = (COMPARE_RU if russian else COMPARE_EN).search(text)
    research = (RESEARCH_RU if russian else RESEARCH_EN).search(text)
    if compare and (not research or compare.start() < research.start()):
        topic = _topic(compare["topic"])
        if topic and (len(topic) >= 30 or DETAIL.search(text) or MARKET.search(topic)):
            return (f"Сравню {topic}. Выделю главные различия и объясню, в каких случаях какой вариант подходит."
                    if russian else f"I’ll compare {topic}. I’ll highlight the main differences and when each option makes sense.")
    if research and (topic := _topic(research["topic"])):
        market = MARKET.search(topic)
        if russian:
            kind = (research["kind"] or "").casefold()
            if kind:
                if kind in {"анализ", "разбор", "обзор", "отчёт", "отчет"}:
                    lead = "Подготовлю " + kind
                else:
                    lead = "Проведу исследование"
            else:
                lead = {"исследуй": "Исследую", "проанализируй": "Проанализирую",
                        "разбери подробно": "Подробно разберу"}[research["verb"].casefold()]
            plan = ("Посмотрю на спрос, покупателей и ключевых конкурентов."
                    if market else "Сверю доступные данные и соберу главные выводы.")
            return f"{lead} {topic}. {plan}"
        plan = ("I’ll look at demand, buyers, and key competitors."
                if market else "I’ll review the available information and pull out the main findings.")
        return f"I’ll research {topic}. {plan}"
    draft = (DRAFT_RU if russian else DRAFT_EN).search(text)
    if draft and (topic := _topic(draft["topic"])):
        return (f"Подготовлю {topic}. Соберу всё в одном ответе."
                if russian else f"I’ll prepare {topic} and put it together in one reply.")
    return None


BACKGROUND = re.compile(r"\b(?:в\s+фон(?:е|овом\s+режиме)|параллельно|in\s+(?:the\s+)?background|in parallel)\b", re.I)
FILE_WORK = re.compile(r"\b(?:отредактируй|исправь|пересчитай|обнови|добавь|заполни|объедини|сравни|собери|измени|edit|fix|recalculate|update|add|merge|compare|fill)\b", re.I)
FOLDER_WORK = re.compile(r"сравни|собери|подготовь|compare|compile|prepare", re.I)
SEPARATE = re.compile(r"субагент|subagents?|отдельн\w* (?:задач|поручен)|separate tasks", re.I)
LIST_ITEM = re.compile(r"(?m)^\s*(?:\d{1,2}[.)]|[-•])\s+")
STATUS = re.compile(
    r"^(?:а\s+)?(?:что (?:сейчас )?(?:в фоне|в работе|ты делаешь в фоне)|(?:какие|сколько) "
    r"(?:у (?:меня|тебя) )?(?:сейчас )?(?:фоновых |фоновые )?(?:задач\w*|поручени\w*) (?:сейчас )?(?:в фоне|в работе|в очереди|выполня\w*)"
    r"|(?:покажи|перечисли) (?:мои |текущие )?фоновые (?:задачи|поручения)|what(?:’s|'s| is) (?:running|in the queue)"
    r"|(?:show|list) (?:my )?background (?:jobs|tasks))[?!. ]*$", re.I)
QUESTION = re.compile(r"^(?:а\s+)?(?:как\b|что такое\b|можно ли\b|сколько\b|какие\b|что сейчас\b|how\b|what\b|which\b|are you\b|is there\b)|\b(?:не\s+(?:делай\s+)?в фон|не параллельно|not in (?:the )?background|don't run)", re.I)


def background_prompts(query, files, has_computer):
    """Fast path for self-contained requests. Context-dependent requests go to the planner."""
    prompt = query.partition("\n\n[Files for this request]")[0].strip()
    text = request_text(query)
    if (not text or STATUS.fullmatch(text) or QUESTION.search(text) or DEFERRED.search(text)
            or re.search(r"\b(?:отслеживай|отследи|следи за|уведоми|watch for|notify me|monitor)\b", text, re.I)):
        return []
    if not files and prompt == query and "[Цитата" not in prompt and re.search(
            r"\b(?:сделай это|по нашему обсуждению|предыдущ\w* (?:файл|таблиц|сообщени)|do that|as discussed|previous (?:file|message))\b",
            text, re.I):
        return []
    explicit = BACKGROUND.search(text)
    batch = SEPARATE.search(text)
    folder = has_computer and re.search(r"папк|\bfolder\b", text, re.I)
    spreadsheet = any(item["format"] in {"xlsx", "xls", "ods", "csv", "tsv"} for item in files)
    research = RESEARCH_RU.search(text) or RESEARCH_EN.search(text)
    substantial = (folder and FOLDER_WORK.search(text) or spreadsheet and FILE_WORK.search(text)
                   or research and (MARKET.search(text) or DETAIL.search(text)))
    if not (explicit or batch or substantial and not skip_ack(text)):
        return []
    if (explicit or batch) and len(LIST_ITEM.findall(prompt)) >= 2 and "\n\n[Цитата" not in prompt:
        header, *items = LIST_ITEM.split(prompt)
        # A numbered procedure is one job unless the user explicitly asks for separate workers.
        if SEPARATE.search(header) or re.search(r"параллельно|in parallel", header, re.I):
            parts = [(item.strip(), header.strip() + "\n" + item.strip()) for item in items if item.strip()]
            return parts if all(len(body) <= 4000 for _, body in parts) else []
    if batch or explicit and not files and len(BACKGROUND.sub("", text).split()) < 3:
        return []
    return [(text, prompt)] if len(prompt) <= 4000 else []
