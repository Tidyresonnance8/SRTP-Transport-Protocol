import socket
import sys
import protocol

def recv_and_handle_message(bind_addr: str, bind_port: int) -> int:
    
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.bind((bind_addr, bind_port))
        while True:
            data, peer_addr = sock.recvfrom(1040) 
            packet = protocol.depackage(data)
            if packet is not None:
                pack_type, window, seqnum, timestamp, payload = packet
                print("le décodage réseau a fonctionné")
                print(f"Type:{pack_type},Window: {window},Timestamp: {timestamp}, Seqnum: {seqnum}, Payload: {payload}")
                sock.sendto(data, peer_addr)
            else:
                print("Paquet invalide ou corrompu reçu")
                continue
            #print(f"message reçu du client : {data}")
            #sock.sendto(data,peer_addr)

if __name__ == "__main__":
    bind_addr = sys.argv[1]
    bind_port = int(sys.argv[2])

    recv_and_handle_message(bind_addr,bind_port)

