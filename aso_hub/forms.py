"""Reading a submitted form, with the standard library."""
from urllib.parse import parse_qs

from starlette.requests import Request

LIMIT = 100_000  # a key file is a few thousand characters


class TooLarge(Exception):
    pass


class Form(dict):
    """Field name -> every value sent for it."""

    def text(self, name: str, default: str = "") -> str:
        return self[name][0].strip() if self.get(name) else default

    def secret(self, name: str) -> str:
        """A password exactly as typed."""
        return self[name][0] if self.get(name) else ""

    def all(self, name: str) -> list[str]:
        return [v.strip() for v in self.get(name, []) if v.strip()]

    def on(self, name: str) -> bool:
        return bool(self.get(name))


async def read(request: Request) -> Form:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > LIMIT:
            raise TooLarge
    return Form(parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=500))
