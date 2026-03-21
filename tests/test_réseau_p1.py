import pytest
import protocol

def test_basique():
    payload = b"hello"
    packet = protocol.empackage(protocol.PTYPE_DATA, 10, 5, 1234, payload)
    decoded = protocol.depackage(packet)
    assert decoded is not None
    pack_type, window, seqnum, timestamp, data = decoded
    assert pack_type == protocol.PTYPE_DATA
    assert window == 10
    assert seqnum == 5
    assert timestamp == 1234
    assert data == payload


def test_payload_vide():
    packet = protocol.empackage(protocol.PTYPE_DATA, 5, 1, 42, b"")
    decoded = protocol.depackage(packet)
    assert decoded is not None
    assert decoded[4] == b""

def test_header_corrompu():
    payload = b"data"
    packet = bytearray(protocol.empackage(protocol.PTYPE_DATA, 1, 1, 1, payload))
    packet[0] ^= 0xFF #corruption
    decoded = protocol.depackage(bytes(packet))
    assert decoded is None

def test_payload_corrompu():
    payload = b"data"
    packet = bytearray(protocol.empackage(protocol.PTYPE_DATA, 1, 1, 1, payload))
    packet[15] ^= 0xFF
    decoded = protocol.depackage(bytes(packet))
    assert decoded is None

def test_payload_trop_large():
    payload = b"a" * 1025
    packet = protocol.empackage(protocol.PTYPE_DATA, 1, 1, 1, payload)
    assert packet is None

def test_payload_maximum():
    payload = b"a" * 1024
    packet = protocol.empackage(protocol.PTYPE_DATA, 1, 1, 1, payload)

    decoded = protocol.depackage(packet)
    assert decoded is not None
    assert decoded[4] == payload
