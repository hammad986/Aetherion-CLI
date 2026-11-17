"""Four-rung ambiguity-safe anchor matching."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class MatchResult:
    start: int
    end: int
    rung: int
    similarity: float
    ambiguous: bool = False


@dataclass(slots=True, frozen=True)
class ReplacementResult:
    text: str
    match: MatchResult
    user_modified: bool


class FuzzyMatchError(ValueError):
    pass


def find_match(text: str, old: str, *, threshold: float = 0.95, allow_inline: bool = True) -> MatchResult:
    if not old:
        raise FuzzyMatchError("old anchor cannot be empty")
    exact = _substring_matches(text, old)
    if len(exact) == 1:
        start, end = exact[0]
        return MatchResult(start, end, 1, 1.0)
    if len(exact) > 1 and allow_inline:
        raise FuzzyMatchError("ambiguous exact anchor; use replace_all explicitly")

    old_lines = old.splitlines(keepends=True)
    for rung, normalizer in ((2, _normalize_whitespace), (3, _normalize_trimmed)):
        candidates = _line_candidates(text, old_lines, normalizer)
        if len(candidates) == 1:
            return candidates[0]._replace(rung=rung) if hasattr(candidates[0], "_replace") else MatchResult(candidates[0].start, candidates[0].end, rung, candidates[0].similarity)
        if len(candidates) > 1:
            raise FuzzyMatchError(f"ambiguous rung {rung} anchor")

    fuzzy = _fuzzy_line_candidates(text, old_lines, threshold)
    if len(fuzzy) == 1:
        return MatchResult(fuzzy[0].start, fuzzy[0].end, 4, fuzzy[0].similarity)
    if len(fuzzy) > 1:
        raise FuzzyMatchError("ambiguous fuzzy anchor")
    raise FuzzyMatchError("anchor not found")


def replace_anchor(text: str, old: str, new: str, *, replace_all: bool = False, threshold: float = 0.95) -> ReplacementResult:
    if replace_all:
        matches = _substring_matches(text, old)
        if not matches:
            raise FuzzyMatchError("anchor not found")
        output = text
        for start, end in reversed(matches):
            output = output[:start] + new + output[end:]
        return ReplacementResult(output, MatchResult(matches[0][0], matches[-1][1], 1, 1.0), False)
    match = find_match(text, old, threshold=threshold)
    replacement = new
    if match.rung >= 2 and old.count("\n"):
        replacement = _reindent(replacement, text[match.start:match.end])
    return ReplacementResult(text[:match.start] + replacement + text[match.end:], match, match.rung > 1)


def _substring_matches(text: str, old: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(old, start)
        if index < 0:
            return matches
        matches.append((index, index + len(old)))
        start = index + len(old)


def _line_candidates(text: str, old_lines: list[str], normalizer) -> list[MatchResult]:
    lines = text.splitlines(keepends=True)
    target = [normalizer(line) for line in old_lines]
    candidates: list[MatchResult] = []
    offset = 0
    for index in range(0, len(lines) - len(old_lines) + 1):
        window = [normalizer(line) for line in lines[index : index + len(old_lines)]]
        if window == target:
            end = offset + sum(len(line) for line in lines[index : index + len(old_lines)])
            candidates.append(MatchResult(offset, end, 2, 1.0))
        offset += len(lines[index])
    return candidates


def _fuzzy_line_candidates(text: str, old_lines: list[str], threshold: float) -> list[MatchResult]:
    lines = text.splitlines(keepends=True)
    target = "\n".join(_normalize_trimmed(line) for line in old_lines)
    candidates: list[MatchResult] = []
    offset = 0
    for index in range(0, len(lines) - len(old_lines) + 1):
        window_text = "\n".join(_normalize_trimmed(line) for line in lines[index : index + len(old_lines)])
        similarity = levenshtein_ratio(target, window_text)
        if similarity >= threshold:
            end = offset + sum(len(line) for line in lines[index : index + len(old_lines)])
            candidates.append(MatchResult(offset, end, 4, similarity))
        offset += len(lines[index])
    if not candidates:
        return []
    best = max(candidate.similarity for candidate in candidates)
    return [candidate for candidate in candidates if candidate.similarity == best]


def levenshtein_ratio(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left_char != right_char)))
        previous = current
    distance = previous[-1]
    return 1.0 - distance / max(len(left), len(right))


def _normalize_whitespace(line: str) -> str:
    return " ".join(line.strip().split())


def _normalize_trimmed(line: str) -> str:
    return line.strip()


def _reindent(new: str, matched: str) -> str:
    old_first = next((line for line in matched.splitlines() if line.strip()), "")
    new_first = next((line for line in new.splitlines() if line.strip()), "")
    old_indent = old_first[: len(old_first) - len(old_first.lstrip())]
    new_indent = new_first[: len(new_first) - len(new_first.lstrip())]
    delta = old_indent.replace("\t", "    ").__len__() - new_indent.replace("\t", "    ").__len__()
    if delta == 0:
        return new
    return "\n".join((line[delta:] if delta < 0 and line[: -delta].isspace() else line) for line in new.split("\n"))
