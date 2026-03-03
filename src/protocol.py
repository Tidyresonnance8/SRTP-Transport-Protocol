import struct
import zlib
import socket

PTYPE_DATA = 1
PTYPE_ACK = 2
PTYPE_SACK = 3

def empackage(packet_type, window, seqnum,timestamp, payload = b""):
    try:
        longueur_0 = len(payload)
        if longueur_0 > 1024:
            raise ValueError("Payload trop grand")
        seqnum = seqnum & 0x7FF # Pour garantir 11 bits maximum
        header_byte = (packet_type << 6) | window
        longueur = longueur_0 & 0x1FFF # pour garantir 13 bits maximum
        header_byte_2 = (header_byte << 13) | longueur
        header_byte_3 = (header_byte_2 << 11) | seqnum

        header_sans_crc = struct.pack("!II",header_byte_3,timestamp)

        crc1 = zlib.crc32(header_sans_crc)
        entete_finale = header_sans_crc + struct.pack("!I", crc1)

        if len(payload) > 0:
            crc2 = zlib.crc32(payload)
            return entete_finale + payload + struct.pack("!I", crc2)
        else:
            return entete_finale

    except Exception:
        return None
    
def depackage(segment):
    try:
        if len(segment) < 12:
            return None
        header_bytes = segment[:12]
        bloc_32, timestamp, crc1_recu = struct.unpack("!III", header_bytes)
        seqnum = bloc_32 & 0x7FF
        longueur = (bloc_32 >> 11) & 0x1FFF
        window = (bloc_32 >> 24) & 0x3F
        pack_type = (bloc_32 >> 30) 

        crc1_calcule = zlib.crc32(segment[:8]) & 0xFFFFFFFF
        if crc1_calcule != crc1_recu:
            return None
        if longueur > 0:
            payload = segment[12:12+longueur]
            crc2_bytes = segment[12 + longueur : 12 + longueur + 4]
            crc2_recu = struct.unpack("!I",crc2_bytes)[0]
            if crc2_recu != crc2_bytes:
               return None
        return (pack_type,window,seqnum,timestamp,payload)
    except Exception:
        return None


if __name__ == "__main__":
    print("--- DÉBUT DES TESTS DU PROTOCOLE ---")
    
    # Paramètres de test
    p_type = PTYPE_DATA
    win = 32
    seq = 123
    ts = 456789
    message = b"Bonjour SRTP!" # 13 octets

    try:
        # ÉTAPE 1 : Empaquetage
        print(f"\n[Test 1] Création d'un paquet DATA (Seq: {seq})...")
        paquet_brut = empackage(p_type, win, seq, ts, message)
        print(f"-> Paquet créé ! Taille totale : {len(paquet_brut)} octets")

        # ÉTAPE 2 : Dépaquetage (Le "Round-Trip")
        print("[Test 2] Décodage du paquet...")
        resultat = depackage(paquet_brut)

        if resultat:
            res_type, res_win, res_seq, res_ts, res_payload = resultat
            
            # ÉTAPE 3 : Vérification des valeurs
            print("\n--- RÉSULTATS ---")
            print(f"Type :    {res_type} (Attendu: {p_type})")
            print(f"Window :  {res_win} (Attendu: {win})")
            print(f"Seqnum :  {res_seq} (Attendu: {seq})")
            print(f"Payload : {res_payload.decode()} (Attendu: {message.decode()})")
            
            if res_seq == seq and res_payload == message:
                print("\n TEST RÉUSSI : Les données sont identiques !")
            else:
                print("\n ÉCHEC : Les données ont changé pendant le transport simulé.")
        else:
            print("\n ÉCHEC : depackage() a renvoyé None (CRC invalide ?)")

    except Exception as e:
        print(f"\n ERREUR CRITIQUE : {e}")



