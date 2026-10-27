"""A tiny SOCKS5 server (no auth, CONNECT, names resolved by the proxy) that counts its clients."""

from __future__ import annotations

import socket
import struct
import threading


class SocksServer:
    def __init__(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.connections: list[tuple[str, int]] = []
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        return f"socks5h://127.0.0.1:{self.port}"

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            _, count = client.recv(2)
            client.recv(count)
            client.sendall(b"\x05\x00")  # no authentication
            _, command, _, kind = client.recv(4)
            if kind == 3:
                host = client.recv(client.recv(1)[0]).decode()
            elif kind == 1:
                host = socket.inet_ntoa(client.recv(4))
            else:
                return
            port = struct.unpack(">H", client.recv(2))[0]
            self.connections.append((host, port))
            upstream = socket.create_connection((host, port))
            client.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        except OSError:
            client.close()
            return
        for a, b in ((client, upstream), (upstream, client)):
            threading.Thread(target=_pipe, args=(a, b), daemon=True).start()

    def close(self) -> None:
        self.listener.close()


def _pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
