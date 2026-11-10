# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""驗證 token 後，將兩個 TCP endpoint 轉送到本機 Unix listener。"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets


async def forward_streams(reader_a: asyncio.StreamReader, writer_a: asyncio.StreamWriter,
                          reader_b: asyncio.StreamReader, writer_b: asyncio.StreamWriter) -> None:
    """雙向轉送並獨立傳遞半關閉；取消時回收兩個方向的工作。"""
    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.write_eof()
        await writer.drain()

    tasks = [asyncio.create_task(pump(reader_a, writer_b)),
             asyncio.create_task(pump(reader_b, writer_a))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class StreamConnections:
    """共同管理 listener 與尚在連線／轉送的工作。"""

    def __init__(self) -> None:
        self._servers: list[asyncio.Server] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._closing = False

    async def close(self) -> None:
        """停止接受連線，關閉所有驗證中與轉送中的連線。"""
        self._closing = True
        for server in self._servers:
            server.close()
        # wait_closed 也可能等待既有連線；必須先收束連線，避免互相等待。
        for writer in tuple(self._writers):
            writer.transport.abort()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()


class TcpBridge(StreamConnections):
    """驗證 token 後轉送到對應 Unix endpoint。"""

    def __init__(self, core_path: str, lineage_path: str) -> None:
        super().__init__()
        self.token = secrets.token_hex(32)
        self.core_port: int | None = None
        self.lineage_port: int | None = None
        self._paths = (core_path, lineage_path)

    async def start(self) -> None:
        """建立兩個由系統選 port 的 IPv4 listener。"""
        try:
            for path in self._paths:
                server = await asyncio.start_server(
                    lambda reader, writer, endpoint=path: self._handle(reader, writer, endpoint),
                    host="0.0.0.0", port=0, limit=1024,
                )
                self._servers.append(server)
            self.core_port, self.lineage_port = (
                int(server.sockets[0].getsockname()[1]) for server in self._servers)
        except BaseException:
            await self.close()
            raise

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      endpoint: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        local_writer: asyncio.StreamWriter | None = None
        try:
            if self._closing:
                return
            header = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5.0)
            if len(header) > 1024:
                return
            request = json.loads(header)
            supplied = request.get("token") if isinstance(request, dict) else None
            if not isinstance(supplied, str) or not hmac.compare_digest(
                    supplied.encode("utf-8"), self.token.encode("ascii")):
                return
            local_reader, local_writer = await asyncio.open_unix_connection(endpoint)
            self._writers.add(local_writer)
            # readuntil 只消費驗證行；同次收到的其餘 bytes 仍由第一個 pump 讀取。
            await forward_streams(reader, writer, local_reader, local_writer)
        except (OSError, ValueError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            for connection in (writer, local_writer):
                if connection is not None:
                    connection.close()
                    try:
                        await connection.wait_closed()
                    except OSError:
                        pass
                    self._writers.discard(connection)
            if task is not None:
                self._tasks.discard(task)
