import socket
import sys
import protocol 

def create_socket_and_send_message(server_addr: str, port: int) -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.connect((server_addr, port))
        message = b"Mon premier vrai paquet SRTP !"
        packet = protocol.empackage(protocol.PTYPE_DATA,1,0,12345,message)
        sock.send(packet)
        data, peer_addr = sock.recvfrom(1040)
        print(f"voilà le message a envoyé: '{data}")
        

if __name__ == "__main__":
    server_addr = sys.argv[1]
    port = int(sys.argv[2])

    create_socket_and_send_message(server_addr,port)