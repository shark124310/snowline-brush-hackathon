"""Receive numbered ESP32 Wi-Fi test packets and optionally configure the board."""

import argparse
import getpass
import ipaddress
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class TestReceiver(HTTPServer):
    def __init__(self, address):
        super().__init__(address, TestPacketHandler)
        self.boot_id = None
        self.last_sequence = 0
        self.consecutive = 0
        self.timeout = 0.25

    def record(self, packet, peer):
        sequence = packet["seq"]
        if self.boot_id != packet["boot_id"]:
            self.boot_id = packet["boot_id"]
            self.last_sequence = 0
            self.consecutive = 0
        if sequence > self.last_sequence:
            if sequence != self.last_sequence + 1:
                self.consecutive = 0
            self.last_sequence = sequence
            self.consecutive += 1
        print(f"RX {peer} seq={sequence} rssi={packet['rssi_dbm']} dBm "
              f"consecutive={self.consecutive}", flush=True)


class TestPacketHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def do_POST(self):
        if self.path != "/api/wifi-test":
            self.send_error(404)
            return
        try:
            packet = read_test_packet(self.headers, self.rfile)
        except (KeyError, TypeError, ValueError):
            self.send_error(400, "Invalid Wi-Fi test packet")
            return

        ack = str(packet["seq"]).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(ack)))
        self.end_headers()
        self.wfile.write(ack)
        self.server.record(packet, self.client_address[0])

    def log_message(self, *_):
        pass


def read_test_packet(headers, stream):
    length = int(headers.get("Content-Length", "0"))
    if not 1 <= length <= 512:
        raise ValueError("invalid length")
    packet = json.loads(stream.read(length))
    if (packet["source"] != "wifi-test" or type(packet["seq"]) is not int
            or not 1 <= packet["seq"] <= 0xFFFFFFFF
            or type(packet["boot_id"]) is not int
            or type(packet["rssi_dbm"]) is not int):
        raise ValueError("invalid test packet")
    return packet


def configure_board(port, url):
    import serial

    ssid = os.environ.get("SKI_WIFI_TEST_SSID") or input("2.4 GHz Wi-Fi SSID: ")
    password = os.environ.get("SKI_WIFI_TEST_PASSWORD")
    if password is None:
        password = getpass.getpass("Wi-Fi password (hidden): ")
    if (not 1 <= len(ssid.encode()) <= 32 or len(password.encode()) > 64
            or any(char in ssid + password for char in "\t\r\n")):
        raise ValueError("Invalid SSID or password format")

    board = serial.Serial(baudrate=115200, timeout=0, write_timeout=2)
    board.port = port
    board.dtr = False
    board.rts = False
    board.open()
    try:
        time.sleep(0.5)
        board.write(f"WIFI\t{ssid}\t{password}\t{url}\n".encode())
    except BaseException:
        board.close()
        raise
    return board


def run_test(args):
    with TestReceiver((str(args.host), args.port)) as receiver:
        host, port = receiver.server_address
        url = f"http://{host}:{port}/api/wifi-test"
        print(f"Listening on {url}", flush=True)
        board = configure_board(args.serial_port, url) if args.serial_port else None
        serial_buffer = b""
        last_ack = 0
        passed = False
        deadline = time.monotonic() + args.timeout
        try:
            while time.monotonic() < deadline:
                receiver.handle_request()
                if board:
                    serial_buffer += board.read_all()
                    lines = serial_buffer.split(b"\n")
                    serial_buffer = lines.pop()
                    for line in lines:
                        print("ESP", line.decode(errors="replace").rstrip(), flush=True)
                        if line.startswith(b"ACK seq="):
                            sequence_text = line[8:].split(b" ", 1)[0]
                            if sequence_text.isdigit():
                                last_ack = int(sequence_text)
                if (receiver.consecutive >= args.packets
                        and (board is None or last_ack == receiver.last_sequence)):
                    passed = True
                    break
        finally:
            if board:
                board.close()

        print(f"Received {receiver.consecutive}/{args.packets} consecutive packets",
              flush=True)
        if board:
            print(f"Last ESP ACK seq={last_ack}, last received seq={receiver.last_sequence}",
                  flush=True)
        return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, type=ipaddress.IPv4Address,
                        help="IPv4 address of the PC interface connected to the same Wi-Fi")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--serial-port", help="ESP32 UART port, for example COM8")
    parser.add_argument("--packets", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if args.host.is_unspecified or not 0 <= args.port <= 65535:
        parser.error("Specify an interface IPv4 address and a valid TCP port")
    if args.packets < 1 or args.timeout <= 0:
        parser.error("Packet count and timeout must be positive")
    try:
        return run_test(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Wi-Fi test failed: {error}\n")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
