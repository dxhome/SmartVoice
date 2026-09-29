"""Limits and canonical identifier syntax for the local model catalog."""

import re

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 3 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MODEL_ID_PATTERN = re.compile(r"^(stt|tts)-[a-z0-9]+(?:-[a-z0-9]+)*$")
