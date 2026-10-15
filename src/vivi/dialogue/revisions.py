"""Conservative projection of explicit, self-contained speech corrections.

Only pending text is projected. The caller retains the original transcript;
this function neither modifies history nor executes/replaces a model proposal.
"""
import re

from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.parser import OfflineParser

CORRECTION = re.compile(r'^\s*(?:à\s+không|ý\s+tôi\s+là|sửa\s+lại\s+thành)\s*[,.:]?\s*', re.IGNORECASE)


def coalesce_explicit_revisions(text):
    lines = text.splitlines()
    if len(lines) < 2:
        return text
    parser = OfflineParser()
    result = []

    def draft(line):
        frame = parser.interpret(line, SessionState())
        if (frame.act == 'request' and len(frame.actions) == 1
            and not (frame.unresolved or frame.negated or frame.conditional or frame.scheduled or frame.conflicting)):
            return frame.actions[0]
        return None

    for line in lines:
        corrected = CORRECTION.sub('', line, count=1)
        if corrected != line and result and corrected:
            previous = draft(result[-1])
            # An explicit correction may omit the noun retained from the
            # immediately preceding temperature request. Complete only this
            # unambiguous form; never invent an operation for a bare number.
            numeric = re.fullmatch(r'(?:đặt|chỉnh)\s+(\d+(?:[.,]\d+)?)\s*độ(?:\s*c)?', corrected, re.IGNORECASE)
            if (numeric and previous and previous.capability == 'climate.set_temperature'
                and previous.arguments.value_celsius is not None
                and 16 <= float(numeric[1].replace(',', '.')) <= 30):
                corrected = 'đặt nhiệt độ ' + numeric[1] + ' độ'
            replacement = draft(corrected)
            if previous and replacement and previous.capability == replacement.capability:
                before = previous.arguments.model_dump(exclude_none=True)
                after = replacement.arguments.model_dump(exclude_none=True)
                # A short answer or a different numeric mode needs task context;
                # don't guess how it edits the preceding request.
                if before.keys() == after.keys() and before:
                    result[-1] = corrected
                    continue
        result.append(line)
    return '\n'.join(result)
