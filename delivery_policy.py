"""Local retry policy. Unknown server business codes are never guessed."""
import random


class DeliveryError(RuntimeError):
    def __init__(self, kind, code):
        self.kind = kind
        self.code = str(code)
        super().__init__(self.code)


def transport_error(code):
    code = str(code)
    if code == "PAUSED_1H":
        return DeliveryError("paused", code)
    if code in ("http_401", "http_403"):
        return DeliveryError("auth", code)
    if code.startswith("http_"):
        try:
            status = int(code[5:])
        except ValueError:
            status = 0
        if 400 <= status < 500 and status not in (408, 429):
            return DeliveryError("permanent", code)
    return DeliveryError("transient", code)


def retry_delay(attempts):
    return min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7)) * random.uniform(0.8, 1.2)


def classify(error):
    if isinstance(error, DeliveryError):
        return error.kind, error.code
    if isinstance(error, (ValueError, FileNotFoundError)):
        return "permanent", type(error).__name__
    if isinstance(error, OSError):
        return "local_io", type(error).__name__
    return "transient", type(error).__name__
