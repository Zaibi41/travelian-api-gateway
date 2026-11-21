"""X-Robots-Tag: noindex on every gateway response, so API URLs stay out of search results (gateway#81)."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = b"x-robots-tag"
VALUE = b"noindex"


class NoIndexMiddleware:
    """Pure ASGI (no response buffering), so streaming responses and CORS headers are left as they are."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != HEADER]
                headers.append((HEADER, VALUE))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)
