#!/usr/bin/env python3
"""Minimal Source RCON client for the CS:GO Legacy listen server (UDP, no deps).

Handshake (connectionless style, works against a listen server on 27015):
  -> "challenge rcon\\n"
  <- "challenge rcon <num>\\n"
  -> "rcon <num> <password> <command>\\n"
  <- command output, possibly split over several UDP packets

Usage:
  python rcon.py 127.0.0.1:27015 <password> <command...>
"""
import socket
import sys


def rcon(addr, password, command, timeout=3.0):
    host, port = addr.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)

    s.sendto(b"challenge rcon\n", (host, int(port)))
    try:
        challenge, _ = s.recvfrom(4096)
    except socket.timeout:
        raise SystemExit("no reply to challenge: server not listening, wrong port, or firewall")
    text = challenge.decode("utf-8", "replace").strip()
    parts = text.split()
    if len(parts) < 3 or parts[0] != "challenge" or parts[1] != "rcon":
        raise SystemExit("unexpected challenge reply: %r" % text)
    num = parts[2]

    payload = ("rcon %s %s %s\n" % (num, password, command)).encode("utf-8")
    s.sendto(payload, (host, int(port)))

    chunks = []
    while True:
        try:
            data, _ = s.recvfrom(65500)
        except socket.timeout:
            break
        body = data.decode("utf-8", "replace")
        if body.startswith("challenge rcon"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        if "Invalid password" in body or "rcon failed" in body.lower():
            raise SystemExit("rcon authentication rejected: wrong password, or rcon_password not set")
        chunks.append(body)
    s.close()
    return "".join(chunks).strip()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    target, pwd = sys.argv[1], sys.argv[2]
    cmd = " ".join(sys.argv[3:])
    print(rcon(target, pwd, cmd))
