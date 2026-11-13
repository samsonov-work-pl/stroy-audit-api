"""Постфильтр отчётов аудита: изоляция устаревших норм в разделе 📖.

Промпт требует упоминать отменённые/устаревшие нормы только в разделе
«📖 Опечатки и устаревшие нормы», но модель (особенно gemini-2.5-flash
в экспрессах) недетерминированно нарушает запрет. Фильтр удаляет такие
утечки из остальных разделов: отдельные предложения, пункты списков
(с перенумерацией оставшихся) и строки таблиц. Раздел 📖 не трогается.
Если после чистки критический/спорный/уточняющий раздел опустел,
вставляется каноническая фраза «не выявлено».
"""
import logging
import re

log = logging.getLogger("report_filter")

# Утечка: упоминание устаревших норм вне раздела 📖.
LEAK_RE = re.compile(
    r"устаревш|отмен[её]|не действует|взамен|неактуальн|не актуальн|актуализ",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^#{1,6}\s*\S")
TABLE_ROW_RE = re.compile(r"^\s*\|")
BULLET_RE = re.compile(r"^[-*•]\s+")
# «2.1. Текст» / «2.1) Текст» — номер пункта; короткие числа, после
# разделителя обязателен пробел, чтобы не трогать цитаты норм
# вида «3.04.01-87», «2019. …».
SUBITEM_RE = re.compile(r"^([1-9]\d?)\.(\d{1,2})([.):])[ \t]+(.*)$")
BOLD_SUBITEM_RE = re.compile(r"^\*\*([1-9]\d?)\.(\d{1,2})\.?\*\*[ \t]+(.*)$")
BOLD_TOPITEM_RE = re.compile(r"^\*\*([1-9]\d?)\.?\*\*[ \t]+(.*)$")
TOPITEM_RE = re.compile(r"^([1-9]\d?)([.):])[ \t]+(.*)$")
# Разбиение на предложения: конец .!?… + пробел + заглавная/цифра/«.
SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])[ \t]+(?=[А-ЯЁA-Z«(\d])")

EMPTY_SECTION_FALLBACK = {
    "🔴": "Критических нарушений не выявлено.",
    "🟡": "Спорных решений, требующих обоснования, не выявлено.",
    "🔵": "Замечаний не выявлено.",
}


def _is_item_start(stripped: str) -> bool:
    return bool(
        SUBITEM_RE.match(stripped)
        or BOLD_SUBITEM_RE.match(stripped)
        or BOLD_TOPITEM_RE.match(stripped)
        or TOPITEM_RE.match(stripped)
    )


def _is_block_end(stripped: str) -> bool:
    """Граница блока пункта: пустая строка, заголовок, таблица, новый пункт."""
    return (
        not stripped
        or HEADING_RE.match(stripped)
        or TABLE_ROW_RE.match(stripped)
        or _is_item_start(stripped)
        or BULLET_RE.match(stripped)
    )


def _scrub_sentences(text: str) -> str:
    """Удаляет из строки предложения с утечками, остальные сохраняет."""
    if not LEAK_RE.search(text):
        return text
    parts = SENT_SPLIT_RE.split(text)
    kept = [p for p in parts if p and not LEAK_RE.search(p)]
    return " ".join(kept)


def _scrub_lines(lines):
    """Проход 1: удаление утечек вне 📖 (построчно, с блочной логикой пунктов)."""
    out = []
    in_readme = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if HEADING_RE.match(stripped):
            in_readme = "📖" in stripped
            out.append(line)
            i += 1
            continue

        if in_readme or not LEAK_RE.search(line):
            out.append(line)
            i += 1
            continue

        # Строка таблицы с утечкой — удалить строку целиком.
        if TABLE_ROW_RE.match(stripped):
            i += 1
            continue

        # Пункт списка с утечкой в заголовочной строке — удалить весь блок.
        if _is_item_start(stripped) or BULLET_RE.match(stripped):
            block = [line]
            j = i + 1
            while j < n and not _is_block_end(lines[j].strip()):
                block.append(lines[j])
                j += 1
            if LEAK_RE.search(block[0]):
                i = j
                continue
            out.append(block[0])
            for cont in block[1:]:
                scrubbed = _scrub_sentences(cont)
                if scrubbed:
                    out.append(scrubbed)
            i = j
            continue

        # Проза: удалить только протёкшие предложения.
        scrubbed = _scrub_sentences(line)
        if scrubbed:
            out.append(scrubbed)
        i += 1
    return out


def _renumber(lines):
    """Проход 2: сплошная перенумерация пунктов после удалений."""
    out = []
    sub_counters = {}
    top_counter = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("|"):
            sub_counters = {}
            top_counter = 0
            out.append(line)
            continue

        m = SUBITEM_RE.match(stripped)
        if m:
            prefix, delim, rest = m.group(1), m.group(3), m.group(4)
            sub_counters[prefix] = sub_counters.get(prefix, 0) + 1
            new = f"{prefix}.{sub_counters[prefix]}{delim}"
            if rest:
                new += " " + rest
            out.append(new)
            continue

        mb = BOLD_SUBITEM_RE.match(stripped)
        if mb:
            prefix, rest = mb.group(1), mb.group(3)
            sub_counters[prefix] = sub_counters.get(prefix, 0) + 1
            new = f"**{prefix}.{sub_counters[prefix]}.**"
            if rest:
                new += " " + rest
            out.append(new)
            continue

        mt = BOLD_TOPITEM_RE.match(stripped)
        if mt:
            rest = mt.group(2)
            top_counter += 1
            new = f"**{top_counter}.**"
            if rest:
                new += " " + rest
            out.append(new)
            continue

        m2 = TOPITEM_RE.match(stripped)
        if m2:
            delim, rest = m2.group(2), m2.group(3)
            top_counter += 1
            new = f"{top_counter}{delim}"
            if rest:
                new += " " + rest
            out.append(new)
            continue

        if stripped:
            top_counter = 0
        out.append(line)
    return out


def _fill_empty_sections(lines):
    """Проход 3: пустые после чистки 🔴/🟡/🔵 получают фразу «не выявлено»."""
    out = []
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        out.append(line)
        stripped = line.strip()
        if HEADING_RE.match(stripped):
            emoji = next((e for e in EMPTY_SECTION_FALLBACK if e in stripped), None)
            if emoji:
                j = i + 1
                has_content = False
                while j < n:
                    nxt = lines[j].strip()
                    if HEADING_RE.match(nxt):
                        break
                    if nxt and nxt != "---":
                        has_content = True
                        break
                    j += 1
                if not has_content:
                    out.append("")
                    out.append(EMPTY_SECTION_FALLBACK[emoji])
        i += 1
    return out


def scrub_report(report: str) -> str:
    """Убирает упоминания устаревших норм вне раздела 📖 из отчёта."""
    if not report:
        return report
    lines = report.split("\n")
    before = len(lines)
    lines = _scrub_lines(lines)
    removed = before - len(lines)
    lines = _renumber(lines)
    lines = _fill_empty_sections(lines)
    result = "\n".join(lines)
    outside = 0
    in_readme = False
    for stripped in (l.strip() for l in result.split("\n")):
        if HEADING_RE.match(stripped):
            in_readme = "📖" in stripped
        elif not in_readme and LEAK_RE.search(stripped):
            outside += 1
    if removed or outside:
        log.info(
            "Постфильтр: удалено %d строк-утечек устаревших норм, осталось вне 📖: %d",
            removed,
            outside,
        )
    return result
