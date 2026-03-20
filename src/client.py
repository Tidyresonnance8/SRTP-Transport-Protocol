import socket
import sys
import protocol 
from urllib.parse import urlparse
import random

def create_socket_and_send_message(server_addr: str, port: int, path: str) -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.connect((server_addr, port))
        # requête HTTP 0.9
        request = ("GET " + path).encode('ascii')
        packet = protocol.empackage(protocol.PTYPE_DATA,63,0,12345,request)
        sock.send(packet)
        buffer = {}
        seqnum_attendu = 0
        with open("llm.model", "wb") as file:
            while True:
                data, peer_addr = sock.recvfrom(1040)
                # sabotage du packet
                if random.random() < 0.2: #je donne 20% de chance de perdre le paquet
                    print("le client a 'perdu' ce paquet !")
                    continue
                # fin de sabotage
                packet = protocol.depackage(data)
                if packet is not None:
                    pack_type, window, seqnum, timestamp, payload = packet
                    if pack_type == protocol.PTYPE_DATA:
                        if seqnum == seqnum_attendu:
                            if payload == b"":
                                packet_new = protocol.empackage(
                                    protocol.PTYPE_ACK,
                                        window,
                                        seqnum_attendu,
                                        timestamp,
                                        b""
                                )
                                sock.send(packet_new)
                                break
                            
                            file.write(payload)
                            seqnum_attendu = (seqnum_attendu+1)%2048
                            # je vide le buffer si les paquets suivant sont déjà là
                            while seqnum_attendu in buffer:
                                file.write(buffer[seqnum_attendu])
                                del buffer[seqnum_attendu] # On retire du buffer
                                seqnum_attendu = (seqnum_attendu+1)%2048
                        else:
                            # c'est un paquet du futur on le sauvegarde
                            buffer[seqnum] = payload
                        # Envoi de l'acquittement ACK ou SACK
                        if len(buffer) == 0:
                            # Ici tout est en ordre on envoit l'ACK
                            packet_new = protocol.empackage(
                                protocol.PTYPE_ACK,
                                window,
                                seqnum_attendu,
                                timestamp,
                                b""
                            )
                            sock.send(packet_new)
                        else:
                            # on a des paquets en attente, on envoie un SACK
                            list_key = list(buffer.keys())
                            payload_s = protocol.encode_sack(list_key)
                            packet_new = protocol.empackage(
                                protocol.PTYPE_SACK,
                                window,
                                seqnum_attendu,
                                timestamp,
                                payload_s
                            )
                            sock.send(packet_new)
        print("Fichier llm.model téléchargé et sauvegardé avec succès !")
        

if __name__ == "__main__":
    url = sys.argv[1]
    parsed = urlparse(url)
    server_addr = parsed.hostname
    port = parsed.port
    path = parsed.path

    create_socket_and_send_message(server_addr,port,path)