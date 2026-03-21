import socket
import sys
import argparse
import protocol
from urllib.parse import urlparse
import random
import time


def create_socket_and_send_message(server_addr: str, port: int, path: str, save_path: str) -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.connect((server_addr, port))

        # Requête HTTP 0.9
        request = ("GET " + path).encode('ascii')
        ts_envoi = int(time.time() * 1000) % (2**32)
        packet = protocol.empackage(protocol.PTYPE_DATA, 63, 0, ts_envoi, request)
        sock.send(packet)

        buffer = {}
        seqnum_attendu = 0
        sock.settimeout(5.0)  # timeout global pour éviter boucle infinie

        # BUG FIX #4 : utiliser save_path au lieu de "llm.model" en dur
        with open(save_path, "wb") as file:
            while True:
                try:
                    data, peer_addr = sock.recvfrom(1040)
                except socket.timeout:
                    # Retransmettre la requête initiale si rien reçu
                    ts_envoi = int(time.time() * 1000) % (2**32)
                    packet = protocol.empackage(protocol.PTYPE_DATA, 63, 0, ts_envoi, request)
                    sock.send(packet)
                    continue

                packet = protocol.depackage(data)
                if packet is None:
                    continue

                pack_type, window, seqnum, timestamp, payload = packet

                if pack_type == protocol.PTYPE_DATA:
                    # BUG FIX #5 : détecter fin de transfert même si seqnum != seqnum_attendu
                    # La spec dit : paquet vide = fin de connexion
                    if payload == b"":
                        # Envoyer un ACK final et terminer
                        ts_ack = int(time.time() * 1000) % (2**32)
                        pkt_ack = protocol.empackage(
                            protocol.PTYPE_ACK, 63, seqnum_attendu, ts_ack, b""
                        )
                        sock.send(pkt_ack)
                        break

                    if seqnum == seqnum_attendu:
                        file.write(payload)
                        seqnum_attendu = (seqnum_attendu + 1) % 2048
                        # Vider le buffer des paquets suivants déjà reçus
                        while seqnum_attendu in buffer:
                            file.write(buffer[seqnum_attendu])
                            del buffer[seqnum_attendu]
                            seqnum_attendu = (seqnum_attendu + 1) % 2048
                    else:
                        # Paquet hors-séquence : on le met en buffer
                        buffer[seqnum] = payload

                    # Envoyer ACK ou SACK
                    ts_ack = int(time.time() * 1000) % (2**32)
                    if len(buffer) == 0:
                        pkt_ack = protocol.empackage(
                            protocol.PTYPE_ACK, 63, seqnum_attendu, ts_ack, b""
                        )
                        sock.send(pkt_ack)
                    else:
                        list_key = list(buffer.keys())
                        payload_s = protocol.encode_sack(list_key)
                        pkt_sack = protocol.empackage(
                            protocol.PTYPE_SACK, 63, seqnum_attendu, ts_ack, payload_s
                        )
                        sock.send(pkt_sack)

        print(f"Fichier sauvegardé dans {save_path}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--save', default='llm.model', help='Chemin de sauvegarde')
    args = parser.parse_args()

    parsed = urlparse(args.url)
    server_addr = parsed.hostname
    port = parsed.port
    path = parsed.path

    create_socket_and_send_message(server_addr, port, path, args.save)