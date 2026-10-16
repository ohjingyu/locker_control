"""RS485 사물함 보드 임시 제어 프로그램 (tkinter + pyserial)"""
import queue
import threading
import time
import tkinter as tk
from functools import reduce
from tkinter import messagebox, ttk

import serial
from serial.tools import list_ports

BAUDS = ["9600", "19200", "38400", "57600", "115200"]


# ---------- 프로토콜 ----------
def bcc(data):
    return reduce(lambda a, b: a ^ b, data, 0)


def frame(*body):
    """본문 바이트 뒤에 체크코드(XOR)를 붙인다."""
    body = bytes(body)
    return body + bytes([bcc(body)])


def mask3(channels):
    """채널 번호 목록 -> 8채널씩 3바이트 (CH1-8, CH9-16, CH17-24)"""
    m = [0, 0, 0]
    for c in channels:
        m[(c - 1) // 8] |= 1 << ((c - 1) % 8)
    return m


def open_one(board, ch):
    return frame(0x8A, board, ch, 0x11)


def open_all(board):
    return frame(0x8A, board, 0x00, 0x11)


def query_one(board, ch):
    return frame(0x80, board, ch, 0x33)


def query_all(board):
    return frame(0x80, board, 0x00, 0x33)


def open_multi(board, channels):
    return frame(0x90, board, *mask3(channels))


def closed_channels(s1, s2, s3):
    """조회 응답(State1=17-24, State2=9-16, State3=1-8) -> 닫힌 채널 목록"""
    return [i * 8 + b + 1 for i, s in enumerate((s3, s2, s1)) for b in range(8) if s >> b & 1]


def describe(d):
    """수신 데이터를 사람이 읽을 문장으로 바꾼다."""
    if len(d) not in (5, 7) or bcc(d[:-1]) != d[-1]:
        return "알 수 없는 데이터 또는 체크코드 오류"
    if len(d) == 7 and d[0] == 0x80 and d[5] == 0x33:
        return f"전체 상태 - 닫힌 칸: {closed_channels(d[2], d[3], d[4]) or '없음'} (나머지는 열림)"
    if d[0] == 0x8A:
        return f"{d[2]}번 칸 열기 응답: " + ("열림 성공" if d[3] == 0x00 else "열림 실패") + " (닫힘감지형 기준)"
    if d[0] == 0x80:
        return f"{d[2]}번 칸 상태: " + ("열림" if d[3] == 0x00 else "닫힘") + " (닫힘감지형 기준)"
    return "알 수 없는 응답"


# 프로토콜 자가 검사: 사진 속 예시와 같은지 확인
assert open_all(1) == bytes.fromhex("8A 01 00 11 9A")
assert open_one(1, 1) == bytes.fromhex("8A 01 01 11 9B")
assert query_one(1, 1) == bytes.fromhex("80 01 01 33 B3")
assert query_all(1) == bytes.fromhex("80 01 00 33 B2")
assert open_multi(1, [2, 10, 18]) == bytes.fromhex("90 01 02 02 02 93")
assert closed_channels(1, 1, 1) == [1, 9, 17]


# ---------- 화면 ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("사물함 보드 임시 제어 프로그램")
        self.geometry("900x720")
        self.ser = None
        self.q = queue.Queue()
        self.btns = {}
        self.build()
        self.refresh_ports()
        self.after(50, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def build(self):
        top = ttk.LabelFrame(self, text="1. 연결")
        top.pack(fill="x", padx=8, pady=4)
        self.port = ttk.Combobox(top, width=10, state="readonly")
        self.port.pack(side="left", padx=4, pady=4)
        ttk.Button(top, text="새로고침", command=self.refresh_ports).pack(side="left")
        ttk.Label(top, text="속도").pack(side="left", padx=(10, 2))
        self.baud = ttk.Combobox(top, width=8, values=BAUDS, state="readonly")
        self.baud.set("9600")
        self.baud.pack(side="left")
        self.conn_btn = ttk.Button(top, text="연결", command=self.toggle)
        self.conn_btn.pack(side="left", padx=10)
        self.status = ttk.Label(top, text="연결 안 됨", foreground="red")
        self.status.pack(side="left")

        ctl = ttk.LabelFrame(self, text="2. 제어")
        ctl.pack(fill="x", padx=8, pady=4)
        ttk.Label(ctl, text="보드 번호").grid(row=0, column=0, padx=4, pady=4)
        self.board = tk.Spinbox(ctl, from_=1, to=99, width=5)
        self.board.grid(row=0, column=1)
        ttk.Label(ctl, text="칸 번호").grid(row=0, column=2, padx=(12, 4))
        self.ch = tk.Spinbox(ctl, from_=1, to=24, width=5)
        self.ch.grid(row=0, column=3)
        for i, (t, f) in enumerate([
            ("이 칸 열기", lambda: self.send(open_one(self.b(), self.c()))),
            ("전체 열기", lambda: self.send(open_all(self.b()))),
            ("이 칸 상태조회", lambda: self.send(query_one(self.b(), self.c()))),
            ("전체 상태조회", lambda: self.send(query_all(self.b()))),
        ]):
            ttk.Button(ctl, text=t, command=f).grid(row=0, column=4 + i, padx=4)
        ttk.Label(ctl, text="여러 칸 열기 (예: 2,10,18)").grid(row=1, column=0, columnspan=3, pady=4)
        self.multi = ttk.Entry(ctl, width=20)
        self.multi.grid(row=1, column=3, columnspan=2)
        ttk.Button(ctl, text="여러 칸 열기", command=self.do_multi).grid(row=1, column=5)

        grid = ttk.LabelFrame(self, text="3. 칸 버튼 (누르면 열림 / 전체 상태조회 후 색: 초록=닫힘, 주황=열림)")
        grid.pack(fill="x", padx=8, pady=4)
        for n in range(1, 25):
            b = tk.Button(grid, text=str(n), width=6, command=lambda n=n: self.send(open_one(self.b(), n)))
            b.grid(row=(n - 1) // 8, column=(n - 1) % 8, padx=3, pady=3)
            self.btns[n] = b
        self.default_bg = self.btns[1].cget("bg")

        raw = ttk.LabelFrame(self, text="4. 직접 보내기 (16진수, 체크코드 자동 추가)")
        raw.pack(fill="x", padx=8, pady=4)
        self.raw = ttk.Entry(raw, width=30)
        self.raw.insert(0, "8A 01 01 11")
        self.raw.pack(side="left", padx=4, pady=4)
        ttk.Button(raw, text="보내기", command=self.do_raw).pack(side="left")

        logf = ttk.LabelFrame(self, text="5. 송수신 로그 (TX=보냄 파랑 / RX=받음 초록)")
        logf.pack(fill="both", expand=True, padx=8, pady=4)
        self.log = tk.Text(logf, height=12, state="disabled", font=("Consolas", 10))
        self.log.tag_config("tx", foreground="#0050c8")
        self.log.tag_config("rx", foreground="#0a8a2a")
        self.log.tag_config("info", foreground="#666666")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        ttk.Button(self, text="로그 지우기", command=lambda: self.log_clear()).pack(pady=2)

    # --- 입력값 ---
    def b(self):
        return int(self.board.get())

    def c(self):
        return int(self.ch.get())

    # --- 로그 ---
    def add(self, tag, text):
        self.log.config(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def log_clear(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # --- 시리얼 ---
    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.port["values"] = ports
        if ports and not self.port.get():
            self.port.set(ports[0])

    def toggle(self):
        if self.ser:
            return self.disconnect()
        try:
            self.ser = serial.Serial(self.port.get(), int(self.baud.get()), timeout=0.05)
        except (serial.SerialException, ValueError) as e:
            self.ser = None
            return messagebox.showerror("연결 실패", f"포트를 열 수 없습니다.\n{e}")
        threading.Thread(target=self.reader, args=(self.ser,), daemon=True).start()
        self.conn_btn.config(text="연결 끊기")
        self.status.config(text=f"{self.port.get()} 연결됨", foreground="green")
        self.add("info", f"연결: {self.port.get()} {self.baud.get()}bps, 8N1")

    def disconnect(self):
        s, self.ser = self.ser, None
        if s:
            s.close()
        self.conn_btn.config(text="연결")
        self.status.config(text="연결 안 됨", foreground="red")
        self.add("info", "연결 끊음")

    def reader(self, s):
        """데이터가 들어온 뒤 잠깐 조용해지면 한 프레임으로 보고 화면에 전달."""
        buf = b""
        try:
            while s.is_open:
                d = s.read(256)
                if d:
                    buf += d
                elif buf:
                    self.q.put(buf)
                    buf = b""
        except (serial.SerialException, OSError):
            self.q.put(None)

    def poll(self):
        while not self.q.empty():
            d = self.q.get()
            if d is None:
                self.add("info", "포트 오류: 연결이 끊어졌습니다")
                self.disconnect()
                continue
            self.add("rx", f"RX {d.hex(' ').upper()}  → {describe(d)}")
            if len(d) == 7 and d[0] == 0x80 and d[5] == 0x33 and bcc(d[:-1]) == d[-1]:
                closed = closed_channels(d[2], d[3], d[4])
                for n, btn in self.btns.items():
                    btn.config(bg="#8fd68f" if n in closed else "#ffb366")
        self.after(50, self.poll)

    def send(self, data):
        if not self.ser:
            return messagebox.showwarning("알림", "먼저 포트를 연결해 주세요.")
        try:
            self.ser.write(data)
        except serial.SerialException as e:
            return messagebox.showerror("전송 실패", str(e))
        self.add("tx", f"TX {data.hex(' ').upper()}")

    def do_multi(self):
        try:
            chs = [int(x) for x in self.multi.get().replace(" ", "").split(",") if x]
            assert chs and all(1 <= c <= 24 for c in chs)
        except (ValueError, AssertionError):
            return messagebox.showwarning("알림", "1~24 숫자를 쉼표로 적어 주세요. 예: 2,10,18")
        self.send(open_multi(self.b(), chs))

    def do_raw(self):
        try:
            body = bytes.fromhex(self.raw.get())
            assert body
        except (ValueError, AssertionError):
            return messagebox.showwarning("알림", "16진수로 적어 주세요. 예: 8A 01 01 11")
        self.send(frame(*body))

    def on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
