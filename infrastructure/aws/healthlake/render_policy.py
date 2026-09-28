#!/usr/bin/env python3
"""Render a policy template: `${NAME}` placeholders are replaced from the environment. Missing variable = hard error.

    ACCT=123456789012 DS=... python render_policy.py policies/import-trust.json.tpl > import-trust.json

The output is validated as JSON before it is printed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from string import Template


def render(template_text: str, env: dict[str, str]) -> str:
    text = Template(template_text).substitute(env)  # KeyError on a missing variable; never a silent blank
    json.loads(text)
    return text


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: render_policy.py TEMPLATE")
    try:
        print(render(Path(sys.argv[1]).read_text("utf-8"), dict(os.environ)), end="")
    except KeyError as exc:
        sys.exit(f"render_policy: missing variable {exc} for {sys.argv[1]}")
