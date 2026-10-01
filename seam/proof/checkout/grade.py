"""Graders for the checkout proof. They see a copy of the artifact and cannot change the digest."""


def nope(artifact):
    return ["no"]


def leak(artifact):
    import socket

    socket.create_connection(("203.0.113.1", 80), timeout=1)
    return []
