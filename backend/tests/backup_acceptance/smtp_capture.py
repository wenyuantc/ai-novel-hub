"""隔离 Docker 网络内的 TLS SMTP 捕获器，不进行任何外部投递。"""

import asyncio
import base64
import binascii
import json
import os
import ssl
from pathlib import Path

ROOT = Path("/acceptance-data")


async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    authenticated = False

    async def send(line: bytes) -> None:
        writer.write(line + b"\r\n")
        await writer.drain()

    try:
        await send(b"220 smtp.acceptance.test ESMTP acceptance-only")
        while raw := await reader.readline():
            line = raw.strip()
            verb = line.split(b" ", 1)[0].upper()
            if verb in (b"EHLO", b"HELO"):
                await send(b"250-smtp.acceptance.test")
                await send(b"250-SIZE 1048576")
                await send(b"250 AUTH PLAIN")
            elif verb == b"AUTH":
                try:
                    mechanism, value = line.split(b" ", 2)[1:]
                    expected = (
                        b"\0" + os.environ["SMTP_USERNAME"].encode() + b"\0" + os.environ["SMTP_PASSWORD"].encode()
                    )
                    authenticated = mechanism.upper() == b"PLAIN" and base64.b64decode(value, validate=True) == expected
                except (ValueError, binascii.Error):
                    authenticated = False
                await send(b"235 Authentication succeeded" if authenticated else b"535 Authentication failed")
            elif verb in (b"MAIL", b"RCPT") and not authenticated:
                await send(b"530 Authentication required")
            elif verb in (b"MAIL", b"RCPT", b"RSET", b"NOOP"):
                await send(b"250 OK")
            elif verb == b"DATA":
                await send(b"354 End data with <CR><LF>.<CR><LF>")
                chunks = []
                while True:
                    part = await reader.readline()
                    if part in (b".\r\n", b".\n", b""):
                        break
                    chunks.append(part[1:] if part.startswith(b"..") else part)
                    if sum(map(len, chunks)) > 1024 * 1024:
                        return
                descriptor = os.open(ROOT / "smtp_messages.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "a") as stream:
                    stream.write(json.dumps({"message": b"".join(chunks).decode(errors="replace")}) + "\n")
                mode = ROOT / "smtp_mode.txt"
                if mode.exists() and mode.read_text().strip() == "accept_without_ack":
                    # 已捕获正文但未发250，模拟对方已受理而确认丢失，必须显示unknown且不盲重发。
                    await asyncio.sleep(35)
                    return
                await send(b"250 Accepted by isolated test capture")
            elif verb == b"QUIT":
                await send(b"221 Bye")
                return
            else:
                await send(b"502 Unsupported test command")
    finally:
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT / "tls/smtp_cert.pem", ROOT / "tls/smtp_key.pem")
    server = await asyncio.start_server(accept, "0.0.0.0", 465, ssl=context)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
