"""Simplify the context and model settings of task-title requests."""

import re
import sys


TITLE_SENTENCE = (
    "You will be presented with a user prompt, and your job is to provide a short "
    "title for a task that will be created from that prompt."
)
# Recognize the same boundaries as splitlines(), stopping at the first one.
LINE_BREAK = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


class Plugin:
    """This plugin has no shared mutable state between requests."""

    def matches(self, body: dict) -> tuple[bool, dict | None]:
        # Criterion 1: input must contain at least two items.
        items = body.get("input")
        if not isinstance(items, list) or len(items) < 2:
            return False, None

        # Criterion 2: A developer message must follow only developer-role items.
        for item in items:
            if not isinstance(item, dict) or item.get("role") != "developer":
                return False, None
            if item.get("type") == "message":
                break
        else:
            return False, None

        # Criterion 3: The last item must be a message with the user role.
        last = items[-1]
        if (
            not isinstance(last, dict)
            or last.get("type") != "message"
            or last.get("role") != "user"
        ):
            return False, None

        # Criterion 4: The last message must contain an input_text part with text.
        content = last.get("content")
        if not isinstance(content, list):
            return False, None

        for part in content:
            if not isinstance(part, dict) or part.get("type") != "input_text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue

            # Criterion 5: The title sentence must occur on that text's first line.
            # The sample prefixes the sentence with a greeting on the same line.
            first_break = LINE_BREAK.search(text)
            line_end = first_break.start() if first_break else len(text)
            if text.find(TITLE_SENTENCE, 0, line_end) >= 0:
                return True, item
        return False, None

    def apply(self, body: dict, developer: dict) -> None:
        last = body["input"][-1]
        developer["content"] = [
            {"type": "input_text", "text": "You're a helpful assistant"}
        ]
        body["input"] = [developer, last]
        if isinstance(body.get("tools"), list):
            body["tools"] = []

        # Preserve reasoning settings other than effort when they form an object.
        if not isinstance(body.get("reasoning"), dict):
            body["reasoning"] = {}
        body["reasoning"]["effort"] = "low"
        body["model"] = "gpt-6-luna"
        print("codex_gui_titlegen applied", file=sys.stderr, flush=True)
