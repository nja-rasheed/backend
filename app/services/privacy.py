import re


def redact_sensitive_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(
        r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
        "[Aadhaar Redacted]",
        text,
    )
    return re.sub(r"\b\d{9,18}\b", "[Account Masked]", text)
