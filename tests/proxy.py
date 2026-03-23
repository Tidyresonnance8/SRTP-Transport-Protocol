import os
import sys
import socket
import threading
from Helpers import *

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import protocol


#  Proxy UDP capturant — cœur de la stratégie interop avec server et client de reference

class UDPCapturingProxy:
    """
    Proxy UDP transparent intercalé entre client_ref et le serveur réel.

    Architecture :
        client_ref --UDP--> proxy (port proxy_port)
                            proxy --UDP--> server (port server_port)
                            proxy <--UDP-- server
        client_ref <--UDP-- proxy

    Le proxy relaie tous les paquets dans les deux sens.
    Il inspecte également tous les paquets DATA envoyés par le serveur
    vers le client, les décode avec protocol.depackage() et reconstitue
    le contenu du fichier dans self.received_data (bytes ordonnés) avec
    uniquement les paquets pour lesquels le client a renvoyé un ACK.

    Usage :
        proxy = UDPCapturingProxy(server_port)
        proxy.start()
        # lancer client_ref vers proxy.proxy_port
        proxy.wait_done(timeout=20)
        proxy.stop()
        data = proxy.get_received_data() # bytes reçus par client_ref
    """

    def __init__(self, server_port: int, host: str = HOST):
        self.server_port = server_port
        self.host = host
        self.proxy_port = free_port()
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._thread = None
        self._chunks: dict[int, bytes] = {}  # seqnum -> payload (confirmés par ACK)
        self._pending: dict[int, bytes] = {} # seqnum -> payload (vus mais pas encore ACKés)
        self._fin_received = False
        self._lock = threading.Lock()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def wait_done(self, timeout: float = TIMEOUT_TRANSFER) -> bool:
        """Attend que le transfert soit terminé (paquet DATA vide reçu du serveur)."""
        return self._done_event.wait(timeout=timeout)

    def get_received_data(self) -> bytes:
        """
        Reconstruit et retourne les données dans l'ordre des seqnums.

        Le seqnum SRTP est sur 11 bits (0–2047) et se wrap. On reconstitue
        l'ordre en détectant les sauts de wrap : si seqnum[i+1] < seqnum[i]
        on ajoute 2048. On trie ensuite par seqnum absolu.
        """
        with self._lock:
            if not self._chunks:
                return b""
            seqnums = sorted(self._chunks.keys())
            # Déwrapper les seqnums (window max = 63 paquets, wrap à 2048)
            abs_seqnums = []
            offset = 0
            prev = seqnums[0]
            abs_seqnums.append((seqnums[0] + offset, seqnums[0]))
            for s in seqnums[1:]:
                if s < prev - 1024:   # wrap détecté
                    offset += 2048
                abs_seqnums.append((s + offset, s))
                prev = s
            abs_seqnums.sort()
            return b"".join(self._chunks[orig] for _, orig in abs_seqnums)

    # Boucle interne 

    def _run(self):
        """
        Boucle principale du proxy à socket unique.

        Un serveur UDP répond toujours vers l'adresse SOURCE du paquet
        qu'il reçoit. En pratique, le serveur SRTP répond donc toujours
        vers proxy_port. Il n'y a donc qu'un seul socket nécessaire :
        on distingue client et serveur par l'adresse source de chaque
        paquet entrant.
        """
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.bind((self.host, self.proxy_port))
        sock.settimeout(0.05)
        server_addr = (self.host, self.server_port)
        client_addr = None  # adresse du client_ref (apprise au 1er paquet)

        try:
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    if self._fin_received:
                        self._done_event.set()
                    continue

                if client_addr is None or addr == client_addr:
                    # Paquet venant du client -> relayer vers le serveur et inspecter les ACKs
                    client_addr = addr
                    sock.sendto(data, server_addr)
                    self._inspect_client_packet(data)
                else:
                    # Paquet venant du serveur -> relayer vers le client et mettre en attente
                    if client_addr is not None:
                        sock.sendto(data, client_addr)
                    self._inspect_server_packet(data)

                if self._fin_received:
                    self._done_event.set()

        finally:
            sock.close()

    def _inspect_server_packet(self, raw: bytes):
        """Décode le paquet DATA serveur->client et le place en attente de confirmation."""
        pkt = protocol.depackage(raw)
        if pkt is None:
            return
        pack_type, window, seqnum, timestamp, payload = pkt
        if pack_type != protocol.PTYPE_DATA:
            return
        if payload == b"":
            # Paquet de fin de transfert
            with self._lock:
                self._fin_received = True
        else:
            with self._lock:
                # Mémoriser le payload, en attente d'un ACK/SACK du client
                if seqnum not in self._pending and seqnum not in self._chunks:
                    self._pending[seqnum] = payload

    def _inspect_client_packet(self, raw: bytes):
        """
        Décode le paquet ACK/SACK client->serveur et confirme les paquets acquittés.

        ACK(n): le client a reçu tous les seqnums jusqu'à n-1 (inclus).
                On confirme depuis _pending tout seqnum s tel que la
                distance circulaire de s à n est positive (s < n mod 2048).
        SACK(n):même chose pour la base cumulative n, plus les seqnums
                listés explicitement dans le payload SACK.
        """
        pkt = protocol.depackage(raw)
        if pkt is None:
            return
        pack_type, window, ack_seqnum, timestamp, payload = pkt
        if pack_type not in (protocol.PTYPE_ACK, protocol.PTYPE_SACK):
            return

        with self._lock:
            # Confirmer tous les seqnums strictement inférieurs à ack_seqnum
            # (distance circulaire sur 2048)
            to_confirm = [
                s for s in list(self._pending)
                if ((ack_seqnum - s) % 2048) > 0 and ((ack_seqnum - s) % 2048) < 1024
            ]
            for s in to_confirm:
                self._chunks[s] = self._pending.pop(s)

            # Pour SACK : confirmer aussi les seqnums acquittés sélectivement
            if pack_type == protocol.PTYPE_SACK and payload:
                for s in protocol.decode_sack(payload):
                    if s in self._pending:
                        self._chunks[s] = self._pending.pop(s)
