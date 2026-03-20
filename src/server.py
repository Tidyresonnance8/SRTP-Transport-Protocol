import socket
import sys
import protocol
import time

def recv_and_handle_message(bind_addr: str, bind_port: int) -> int:
    
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.bind((bind_addr, bind_port))
        while True:
            data, peer_addr = sock.recvfrom(1040) 
            packet = protocol.depackage(data)
            if packet is not None:
                pack_type, window, seqnum, timestamp, payload = packet
                # Bouclier anti-fantomes
                if pack_type != protocol.PTYPE_DATA or payload == b"":
                    continue
                print("le décodage réseau a fonctionné")
                texte = payload.decode('ascii')
                chemin_extrait = texte.split(' ')
                if len(chemin_extrait) < 2:
                    continue
                chemin_local = "." + chemin_extrait[1].strip()
                print("Nouvelle requête acceptée :")
                print(f"Type:{pack_type},Window: {window},Timestamp: {timestamp}, Seqnum: {seqnum}, Payload: {chemin_local}")
                try:
                    with open(chemin_local, "rb") as file:
                        base = 0   # il fait référence au plus vieux paquet non acquitté
                        prochain_seqnum = 0 #le prochain paquet qu'on va lire dans le fichier et envoyer
                        memoire_envoi = {} # ce dict va stocker les paquets envoyés mais pas encore acquitté
                        window_client = 63 
                        fichier_fini = False
                        while True: # lecture du fichier binaire
                            while len(memoire_envoi) < window_client and not fichier_fini:
                                morceau = file.read(1024)
                                packet = protocol.empackage(
                                    protocol.PTYPE_DATA,
                                    window,
                                    prochain_seqnum,
                                    timestamp,
                                    morceau
                                )
                                
                                sock.sendto(packet, peer_addr)
                                memoire_envoi[prochain_seqnum] = packet
                                prochain_seqnum = (prochain_seqnum + 1)%2048
                                if morceau == b"":
                                    fichier_fini = True
                                    break
                            sock.settimeout(2.0) # 2000 milliseconde
                            try:
                                ack_data, _ = sock.recvfrom(1040)
                                new_pack = protocol.depackage(ack_data)
                                if new_pack is not None:
                                    pack_type,  window_recue, seqnum_ack, timestamp, payload = new_pack
                                    if pack_type == protocol.PTYPE_ACK or pack_type == protocol.PTYPE_SACK:
                                        while base != seqnum_ack:
                                            if base in memoire_envoi:
                                                del memoire_envoi[base]
                                            base = (base + 1) % 2048
                                        if pack_type == protocol.PTYPE_SACK:
                                                packet_s = protocol.decode_sack(payload)
                                                for numero in packet_s:
                                                    if numero in memoire_envoi.keys():
                                                        del memoire_envoi[numero]
                            except socket.timeout:
                                    for pkt_sauvegarde in memoire_envoi.values():
                                        sock.sendto(pkt_sauvegarde,peer_addr)
                            # l'ack a bien été reçu avec succès
                            if fichier_fini and len(memoire_envoi) == 0:
                                print("transfert du fichier terminé avec succès !")
                                sock.settimeout(None) # désactivation du chrono
                                break

                except FileNotFoundError:
                    print("fichier demandé introuvable")
                    packet = protocol.empackage(
                        protocol.PTYPE_DATA,
                        window,
                        seqnum,
                        timestamp,
                        b""
                    )
                    sock.sendto(packet, peer_addr)

                    continue
            else:
                print("Paquet invalide ou corrompu reçu")
                continue
            #print(f"message reçu du client : {data}")
            #sock.sendto(data,peer_addr)

def server_multitache(bind_addr: str, bind_port: int) -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.bind((bind_addr, bind_port))
        clients = {}
        sock.settimeout(0.01)

        print(f"Serveur multi-clients à l'écoute sur {bind_addr}:{bind_port}...")

        while True:
            # on écoute tous les clients
            try:
                data, peer_addr = sock.recvfrom(1040)
                packet = protocol.depackage(data)
                # pour un nouveau client
                if packet is not None:
                    pack_type, window, seqnum, timestamp, payload = packet
                    if peer_addr not in clients:
                        # Bouclier anti-fantomes
                        if pack_type != protocol.PTYPE_DATA or payload == b"":
                            continue
                        texte = payload.decode('ascii')
                        chemin_extrait = texte.split(' ')
                        if len(chemin_extrait) < 2:
                            continue
                        chemin_local = "." + chemin_extrait[1].strip()
                        print("Nouvelle requête acceptée :")
                        print(f"Type:{pack_type},Window: {window},Timestamp: {timestamp}, Seqnum: {seqnum}, Payload: {chemin_local}")
                        try:
                            fichier = open(chemin_local,"rb")
                            clients[peer_addr] = {
                                'file': fichier, 
                                'base': 0, 
                                'prochain_seqnum': 0,
                                'memoire_envoi': {}, 
                                'fichier_fini': False,
                                'dernier_contact': time.time()
                            }
                        
                        except FileNotFoundError:
                            print("Fichier demandé introuvable")
                            continue
                    # pour un ancien client
                    else:
                        etat = clients[peer_addr]
                        # si un ancien client nous parle on remet son chronomètre à zéro
                        etat['dernier_contact'] = time.time()
                        if pack_type == protocol.PTYPE_ACK or pack_type == protocol.PTYPE_SACK:
                            while etat['base'] != seqnum: # 'seqnum' est le seqnum_ack reçu
                                if etat['base'] in etat['memoire_envoi']:
                                    del etat['memoire_envoi'][etat['base']]
                                etat['base'] = (etat['base'] + 1) % 2048
                            if pack_type == protocol.PTYPE_SACK:
                                packet_s = protocol.decode_sack(payload)
                                for numero in packet_s:
                                    if numero in etat['memoire_envoi']:
                                        del etat['memoire_envoi'][numero]
                            
            except socket.timeout:
                pass

            # on répond à tous les clients
            clients_a_supprimer = []
            temps_actuel = time.time()
            for addr, etat in clients.items():
                while len(etat['memoire_envoi']) < 63 and not etat['fichier_fini']:
                    morceau = etat['file'].read(1024)
                    packet = protocol.empackage(
                        protocol.PTYPE_DATA,
                        63,
                        etat['prochain_seqnum'],
                        0,
                        morceau
                    )
                    sock.sendto(packet,addr)
                    etat['memoire_envoi'][etat['prochain_seqnum']] = packet
                    etat['prochain_seqnum'] = (etat['prochain_seqnum'] + 1) % 2048
                    if morceau == b"":
                        etat['fichier_fini'] = True
                        break
                # on gère la perte des paquets
                if temps_actuel - etat['dernier_contact'] > 2.0:
                    for pkt_sauvegarde in etat['memoire_envoi'].values():
                        sock.sendto(pkt_sauvegarde,addr)
                        # on va reset le chrono pour ne pas le spammer en boucle
                    etat['dernier_contact'] = temps_actuel
                # on vérifie s'il a terminé
                if etat['fichier_fini'] and len(etat['memoire_envoi']) == 0:
                    print(f"Transfert du fichier terminé avec succès pour {addr} !")
                    etat['file'].close()
                    clients_a_supprimer.append(addr)
            for addr in clients_a_supprimer:
                del clients[addr]



            


if __name__ == "__main__":
    bind_addr = sys.argv[1]
    bind_port = int(sys.argv[2])

    #recv_and_handle_message(bind_addr,bind_port)
    server_multitache(bind_addr,bind_port)

