import socket
import os
import threading
import time
from config import *
import struct
from typing import List, Dict, Optional
import hashlib
from queue import Queue


class PackageInfo:
    def __init__(self, send_time: float, package_size: int) -> None:
        self.send_time: float = send_time  # 数据包发送时间
        self.package_size: int = package_size  # 包大小


class ServerInfo:
    package_sent_count = 0  # 发送的数据包个数
    ack_received_count = 0  # 接收到的 ACK 个数
    rtt_increase_count = 0  # RTT 向上调整的次数
    rtt_decrease_count = 0  # RTT 向下调整的次数
    rtt_increase_counter = 0  # 扰动次数多于 ADJUST_RTT_THRESHOLD 则调整
    rtt_decrease_counter = 0  # 扰动次数多于 ADJUST_RTT_THRESHOLD 则调整
    timeout_count = 0  # 超时次数
    package_resent_count = 0  # 重发的数据包个数


class Server:
    def __init__(self) -> None:
        self.rtt = 0
        self.info = ServerInfo()
        self.status = TCPstatus.CLOSED

        self.init_socket()
        self.status = TCPstatus.LISTEN

        # 确定发送文件是否压缩
        if ENABLE_PRE_ZIP:
            start_time = time.time()
            if not os.path.exists(ZIP_FILE_PATH):
                raise FileNotFoundError(ZIP_FILE_PATH)
            with open(FILE_PATH, "rb") as f_in:
                with ZIP_LIB.open(ZIP_FILE_PATH, "wb") as f_out:
                    f_out.writelines(f_in)
            end_time = time.time()
            print(f"zip time: [{end_time - start_time}]")

        else:
            if not os.path.exists(FILE_PATH):
                raise FileNotFoundError(FILE_PATH)

        # 确定文件路径和大小
        self.file_path = ZIP_FILE_PATH if ENABLE_PRE_ZIP else FILE_PATH
        self.file_size = os.path.getsize(self.file_path)

        # 初始化计时器
        # { {seek_pos} : PackageInfo }
        self.timers: Dict[str, Optional[PackageInfo]] = {}
        for thread_id in range(SERVER_SEND_THREAD_NUMBER):
            sequence_numbers = int(round(self.file_size / SERVER_SEND_THREAD_NUMBER)) // CHUNK_SIZE
            for sequence_number in range(sequence_numbers):
                self.timers[f"{thread_id}_{sequence_number}"] = None

        self.init_thread()

    def run(self):
        self.establish_connection()
        self.receive_ack()
        self.send_data()

        for thread in self.receive_threads:
            thread.join()

        for thread in self.send_threads:
            thread.join()

        self.timer_checker.join()
        self.statistic_thread.join()

    def init_socket(self):
        """
        创建两个 socket

        - control_socket 用于传输 ACK NAK 确认帧
        - data_socket 用于传输数据包
        """
        self.control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        control_address = (SERVER_IP, SERVER_CONTROL_PORT)
        self.control_socket.bind(control_address)
        self.log(f"UDP control socket start, listen {control_address}")

        # 发送端的发送缓冲区设置为最大
        self.data_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.data_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, MAX_UDP_BUFFER_SIZE)
        self.client_address = (CLIENT_IP, CLIENT_DATA_PORT)
        self.log("UDP data socket start, waiting for client...")

    def init_thread(self):
        """
        初始化所有线程, 但并不执行
        """
        self.lock = threading.Lock()
        self.send_threads: list[threading.Thread] = []  # 发送线程
        self.receive_threads: List[threading.Thread] = []  # 接收 ACK 线程
        self.timeout_resend_threads : List[threading.Thread] = [] # 超时重发线程
        self.timer_checker = threading.Thread(target=self.check_timer_arrival, args=())  # 定时器检查线程
        self.timer_checker.daemon = True
        # 统计信息的线程, 每隔一秒更新一次
        self.statistic_thread = threading.Thread(target=self.display_statistic, args=())
        self.statistic_thread.daemon = True

        # 初始化发送线程
        # 计算每一个线程需要发送的文件大小, 如果不能整除的话采用四舍五入的方式
        thread_send_size = int(round(self.file_size / SERVER_SEND_THREAD_NUMBER))
        for thread_id in range(SERVER_SEND_THREAD_NUMBER - 1):
            thread = threading.Thread(target=self.send_package, args=(thread_id, thread_send_size))
            thread.daemon = True
            self.send_threads.append(thread)
        # 将剩余部分都放入最后一个线程发送
        bias = thread_send_size * SERVER_SEND_THREAD_NUMBER - self.file_size
        thread = threading.Thread(
            target=self.send_package,
            args=(
                SERVER_SEND_THREAD_NUMBER - 1,
                thread_send_size - bias,
            ),
        )
        thread.daemon = True
        self.send_threads.append(thread)

        # 初始化接收线程
        for thread_id in range(SERVER_ACK_HANDLE_THREAD_NUMBER):
            thread = threading.Thread(target=self.handle_ack, args=(thread_id,))
            thread.daemon = True
            self.receive_threads.append(thread)

        # (seek_pos, package_size)
        self.data_queue = Queue()
        for thread_id in range(SERVER_TIMEOUT_RESEND_THREAD_NUMBER):
            thread = threading.Thread(target=self.timeout_resend, args=())
            thread.daemon = True
            self.timeout_resend_threads.append(thread)

        self.max_package_count = SERVER_SEND_THREAD_NUMBER * (
            int(round(self.file_size / SERVER_SEND_THREAD_NUMBER)) // CHUNK_SIZE
        )
        self.log(f"max_package_count: {self.max_package_count}")

    def establish_connection(self):
        """
        三次握手建立连接

        初步计算 RTT 时延, 告知 client 传输文件大小
        """
        syn_data, _ = self.control_socket.recvfrom(1024)
        syn_data = SYN_PATTERN.match(syn_data.decode())

        client_send_time = syn_data.group("time")
        self.log(f"receive SYN")
        start_time = self.get_time()
        self.rtt = (start_time - float(client_send_time)) * 2
        self.debug(f"rtt = {self.rtt}")
        self.status = TCPstatus.SYN_RCVD

        syn_ack_data = f"SYN ACK {self.file_size}"
        self.data_socket.sendto(syn_ack_data.encode(), self.client_address)
        self.log("send SYN ACK")

        syn_retry_time = 0
        tcp_syn_timeout = self.rtt * MAX_RTT_MULTIPLIER

        while syn_retry_time < TCP_SYN_RETIRES:
            self.control_socket.settimeout(tcp_syn_timeout)
            try:
                ack_data, _ = self.control_socket.recvfrom(1024)
                self.log("receive ACK")
                self.rtt = self.get_time() - start_time
                self.status = TCPstatus.ESTABLISHED
                break
            except socket.timeout:
                syn_retry_time += 1
                tcp_syn_timeout *= 4
                self.rtt *= 4
                self.log("ACK timeout, double")

        if syn_retry_time >= TCP_SYN_RETIRES:
            self.log("fail to connect\n")
            self.close_socket()
            exit(1)

        self.log("successfully build connection")

    def send_data(self):
        """
        所有发送线程都可以开始发送数据了
        """
        # 启动定时器线程
        self.timer_checker.start()
        # 启动所有超时重发线程
        for thread in self.timeout_resend_threads:
            thread.start()    
    
        # 启动所有发送线程
        for thread in self.send_threads:
            thread.start()

        self.statistic_thread.start()

    def send_package(self, thread_id: int, thread_send_size: int):
        """
        每个线程根据偏移量分块发送

        1                            4                          8
        +-------------------------------------------------------+
        |                                                       |
        |                        seek pos                       |
        |                                                       |
        +-------------------------------------------------------+
        |                                                       |
        |                         data                          |
        |                                                       |
        +-------------------------------------------------------+
        """
        start_offset = thread_id * thread_send_size  # 起始偏移位置
        self.debug(f"start sending thread {thread_id}")

        with open(self.file_path, "rb") as f:
            f.seek(start_offset)

            # 如果小于分块大小, 直接一次性发送过去
            if thread_send_size <= CHUNK_SIZE:
                data = f.read(thread_send_size)
                header = struct.pack("!Q", start_offset)
                full_message = header + data
                send_time = self.get_time()
                with self.lock:
                    self.timers[start_offset] = PackageInfo(
                        send_time=send_time,
                        package_size=thread_send_size,
                    )

                self.data_socket.sendto(full_message, self.client_address)
                self.debug(f"[{thread_id}] send data {len(data)}")

            else:
                # 按照分块大小发送 n 次
                n = thread_send_size // CHUNK_SIZE
                for sequence_number in range(n):
                    # 发送的文件内容数据
                    seek_pos = start_offset + sequence_number * CHUNK_SIZE  # 文件偏移量

                    if sequence_number == n - 1:
                        # 最后一次把 thread_send_size 剩余的部分都发过去
                        package_size = thread_send_size - sequence_number * CHUNK_SIZE
                    else:
                        package_size = CHUNK_SIZE  # 数据块大小

                    data = f.read(package_size)
                    header = struct.pack("!Q", seek_pos)

                    full_message = header + data
                    send_time = self.get_time()  # 发送时间
                    # 添加一个定时器
                    with self.lock:
                        self.timers[seek_pos] = PackageInfo(
                            send_time=send_time,
                            package_size=package_size,
                        )
                    self.data_socket.sendto(full_message, self.client_address)

                    self.debug(f"[{thread_id}] send data {sequence_number}:{len(full_message)}")
                    self.info.package_sent_count += 1

        if self.info.package_sent_count == self.max_package_count:
            self.log(f"all packages send, ack/sent = {self.info.ack_received_count}/{self.info.package_sent_count}")

        # 当发送进程结束之后也加入超时重发线程
        self.timeout_resend()    
    def receive_ack(self):
        """
        创建接收线程, 准备收 ACK 数据包
        """
        self.control_socket.settimeout(None)
        for thread in self.receive_threads:
            thread.start()

    def handle_ack(self, thread_id):
        """
        处理来自 client 的 ack 数据包
        """
        while True:
            ack_data, _ = self.control_socket.recvfrom(1024)
            if len(ack_data) < 8:
                # 多余的 ACK 数据包
                self.log("receive unused ack package")
                continue
            seek_pos = struct.unpack("!Q", ack_data[:8])[0]

            # 拥塞控制
            # 根据数据包的往返 RTT 来判断当前 RTT 是否需要改变
            package_info = self.timers[seek_pos]
            if package_info is None:
                # print("?")
                continue

            # 如果连续 ADJUST_RTT_THRESHOLD 次
            package_rtt = self.get_time() - package_info.send_time
            if package_rtt > self.rtt * MAX_RTT_MULTIPLIER:
                self.info.rtt_increase_counter += 1
                self.info.rtt_decrease_counter -= 1
                if self.info.rtt_increase_counter == ADJUST_RTT_THRESHOLD:
                    self.info.rtt_increase_counter = 0
                    self.rtt *= MAX_RTT_MULTIPLIER // 2
                    self.info.rtt_increase_count += 1
                    # self.log(f'{package_rtt} {self.rtt * MAX_RTT_MULTIPLIER}')
                    self.log("adjust rtt larger")

            elif package_rtt < self.rtt / MAX_RTT_MULTIPLIER:
                self.info.rtt_decrease_counter += 1
                self.info.rtt_increase_counter -= 1
                if self.info.rtt_decrease_counter == ADJUST_RTT_THRESHOLD:
                    self.info.rtt_decrease_counter = 0
                    self.rtt /= MAX_RTT_MULTIPLIER // 2
                    self.info.rtt_decrease_count += 1
                    # self.log(f'{package_rtt} {self.rtt * MAX_RTT_MULTIPLIER}')
                    self.log("adjust rtt smaller")

            self.info.rtt_decrease_counter = max(self.info.rtt_decrease_counter, 0)
            self.info.rtt_increase_counter = max(self.info.rtt_increase_counter, 0)

            # 收到 ACK 之后清除定时器
            with self.lock:
                self.timers[seek_pos] = None

            self.debug(f"[{thread_id}] receive ack {seek_pos}")
            self.info.ack_received_count += 1

    def check_timer_arrival(self):
        """
        检查
        """
        self.debug("init timer checker")
        while True:
            time.sleep(self.rtt)
            current_time = self.get_time()
            timeout_packages = []  # 超时重发的数据包
            with self.lock:
                for seek_pos, package_info in self.timers.items():
                    if package_info is None:
                        continue
                    if current_time - package_info.send_time > self.rtt * MAX_RTT_MULTIPLIER:
                        # 超时重发
                        timeout_packages.append(seek_pos)
                        self.data_queue.put((seek_pos, package_info.package_size))
                        self.debug(f"timeout!")
                        self.info.timeout_count += 1

                # 清除超时重发的数据包, 循环结束后处理, 避免影响迭代器
                for seek_pos in timeout_packages:
                    self.timers[seek_pos] = None

    def timeout_resend(self):
        '''
        超时重发
        '''
        # 全部发送完毕后等待没有 ACK 的数据包再次发送
        while True:
            seek_pos, package_size = self.data_queue.get()
            with open(self.file_path, "rb") as f:
                f.seek(seek_pos)
                data = f.read(package_size)
                header = struct.pack("!Q", seek_pos)
                # 重新添加定时器
                with self.lock:
                    self.timers[seek_pos] = PackageInfo(
                        send_time=self.get_time(),
                        package_size=package_size,
                    )
                full_message = header + data
                # 如果当前缓冲区已满, 延迟一段时间后再次发送
                # if self.info.ack_received_count < (self.info.package_sent_count + self.info.package_resent_count) // 10:
                #     # self.log('sleep for a while')
                #     time.sleep(self.rtt * MAX_RTT_MULTIPLIER * 10)

                self.data_socket.sendto(full_message, self.client_address)
                self.info.package_resent_count += 1
                self.debug("resend package")

    def display_statistic(self):
        while True:
            time.sleep(STATISTIC_INTERVAL)
            self.log("-" * 20)
            self.show_statistical_info()
            self.log("-" * 20)

    def close_socket(self):
        self.control_socket.close()
        self.data_socket.close()

    def debug(self, info: str):
        if LOG_MODE == "DEBUG":
            print(f"server: {info}")

    def log(self, info: str):
        print(f"server: {info}")

    def show_statistical_info(self):
        self.log(
            f"send packages: {self.info.package_sent_count}/{self.max_package_count} [{self.info.package_sent_count / self.max_package_count * 100:.2f}%]"
        )
        self.log(f"resend packages: {self.info.package_resent_count}")
        self.log(f"receive acks: {self.info.ack_received_count}")
        self.log(f"timeout count: {self.info.timeout_count}")
        self.log(
            f"package loss: {round(self.info.timeout_count/(self.info.package_sent_count + self.info.package_resent_count), 3) * 100}%"
        )
        # self.log(f"rtt: {self.rtt}")
        # self.log(f"rtt increase time: {self.info.rtt_increase_count}")
        # self.log(f"rtt decrease time: {self.info.rtt_decrease_count}")
        

        # print(NUMBER, self.info.package_sent_count - self.info.ack_received_count)

    def get_time(self):
        return time.time()

    def calculate_md5(self, block_size=8192):
        self.log("calculating md5...")
        md5_hash = hashlib.md5()
        with open(self.file_path, "rb") as file:
            for chunk in iter(lambda: file.read(block_size), b""):
                md5_hash.update(chunk)
        self.log(f"md5: {md5_hash.hexdigest()}")


def main():
    server = Server()
    try:
        server.run()
    except KeyboardInterrupt as e:
        print(e)
    finally:
        server.show_statistical_info()
        # server.calculate_md5()
        server.close_socket()
    print("over")


if __name__ == "__main__":
    main()
