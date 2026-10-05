import sys
import ctypes
import asyncio
import json
import threading
from pathlib import Path

from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QTextEdit
from PyQt5.QtCore import Qt, QObject, pyqtSignal, QPoint
from PyQt5.QtGui import QTextOption, QCursor
from keybinds import Hook
import websockets


# ---------- Windows API ----------
WDA_EXCLUDEFROMCAPTURE = 0x00000011

GWL_EXSTYLE       = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED     = 0x00080000

user32 = ctypes.windll.user32

SetWindowDisplayAffinity = user32.SetWindowDisplayAffinity
SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
SetWindowDisplayAffinity.restype  = ctypes.c_bool

GetWindowLongW = user32.GetWindowLongW
GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
GetWindowLongW.restype  = ctypes.c_long

SetWindowLongW = user32.SetWindowLongW
SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
SetWindowLongW.restype  = ctypes.c_long


CONFIG_PATH = Path(__file__).parent / "overlay_config.json"


def protect_window(hwnd):
    if SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        print("[OK] Invisible to screen share.")
    else:
        print("[WARN] Screen-capture protection failed:", ctypes.GetLastError())


def enable_click_through(hwnd):
    ex = GetWindowLongW(hwnd, GWL_EXSTYLE)
    SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TRANSPARENT | WS_EX_LAYERED)
    print("[OK] Click-through ON.")


def disable_click_through(hwnd):
    ex = GetWindowLongW(hwnd, GWL_EXSTYLE)
    SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_TRANSPARENT)
    print("[OK] Click-through OFF (edit mode).")


# ---------- Overlay Window ----------
class OverlayWindow(QWidget):

    MIN_W, MIN_H       = 260, 100
    MAX_W, MAX_H       = 1400, 900
    MARGIN             = 20
    PADDING            = 20
    SCROLL_STEP        = 40
    MOVE_STEP          = 20
    RESIZE_STEP        = 30
    RESIZE_GRIP        = 24   # bottom-right corner grab size

    def __init__(self):
        super().__init__()

        self._edit_mode   = False
        self._drag_anchor = None   # QPoint when dragging
        self._drag_geom   = None   # geometry at drag start
        self._drag_op     = None   # "move" | "resize"

        self._build_flags()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not self._edit_mode)
        self.setCursor(Qt.ArrowCursor)

        # -------- text widget --------
        self.text = QTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setFrameShape(QTextEdit.NoFrame)
        self.text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.text.setTextInteractionFlags(Qt.NoTextInteraction)
        self.text.setCursor(Qt.ArrowCursor)
        self.text.viewport().setCursor(Qt.ArrowCursor)
        self.text.setFocusPolicy(Qt.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.text)
        self.setLayout(layout)

        self._apply_stylesheet()

        # -------- geometry (from config or centered) --------
        if not self._load_config():
            self._center_on_screen()

        self.hwnd = int(self.winId())
        protect_window(self.hwnd)
        enable_click_through(self.hwnd)

    # -------- window flags --------
    def _build_flags(self):
        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        if self._edit_mode:
            flags |= Qt.WindowDoesNotAcceptFocus  # keep focus off but allow mouse
        else:
            flags |= Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)

    # -------- styling (border shows edit mode) --------
    def _apply_stylesheet(self):
        border = "3px solid #ff3366" if self._edit_mode else "none"
        self.text.setStyleSheet(f"""
            QTextEdit {{
                color: #00ff88;
                background-color: rgba(0, 0, 0, 200);
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 18px;
                padding: {self.PADDING}px;
                border-radius: 10px;
                border: {border};
            }}
        """)

    # -------- geometry helpers --------
    def _screen(self):
        return QApplication.primaryScreen().geometry()

    def _center_on_screen(self):
        s = self._screen()
        w, h = 700, 150
        self.setGeometry((s.width() - w) // 2, (s.height() - h) // 2, w, h)

    def _resize_to_fit(self):
        """Grow/shrink height to fit the text, clamped to MAX_H and screen."""
        if self._edit_mode:
            return  # don't fight the user while they're resizing
        s = self._screen()
        w = self.width()
        viewport_w = max(1, w - 2 * self.PADDING)

        doc = self.text.document()
        doc.setTextWidth(viewport_w)
        text_h = doc.size().height()

        needed = int(text_h) + 2 * self.PADDING + 8
        h = max(self.MIN_H, min(needed, self.MAX_H))
        h = min(h, s.height() - self.MARGIN * 2)

        # keep top-left fixed while auto-resizing height
        self.setGeometry(self.x(), self.y(), w, h)

    # -------- public actions (called via signals) --------
    def update_text(self, new_text):
        self.text.setPlainText(new_text)
        self.text.verticalScrollBar().setValue(0)
        self._resize_to_fit()

    def scroll_up(self):
        b = self.text.verticalScrollBar()
        b.setValue(b.value() - self.SCROLL_STEP)

    def scroll_down(self):
        b = self.text.verticalScrollBar()
        b.setValue(b.value() + self.SCROLL_STEP)

    def move_by(self, dx, dy):
        g = self.geometry()
        s = self._screen()
        nx = max(0, min(g.x() + dx, s.width()  - g.width()))
        ny = max(0, min(g.y() + dy, s.height() - g.height()))
        self.setGeometry(nx, ny, g.width(), g.height())
        self._save_config()

    def resize_by(self, dw, dh):
        g = self.geometry()
        w = max(self.MIN_W, min(g.width()  + dw, self.MAX_W))
        h = max(self.MIN_H, min(g.height() + dh, self.MAX_H))
        self.setGeometry(g.x(), g.y(), w, h)
        self._save_config()

    def center(self):
        self._center_on_screen()
        self._save_config()

    # -------- edit mode --------
    def toggle_edit_mode(self):
        self._edit_mode = not self._edit_mode

        # Need hide/show because Qt can't change input flags at runtime
        self.hide()
        self._build_flags()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not self._edit_mode)
        self._apply_stylesheet()
        self.show()

        # Re-apply Win32 style on the (possibly new) HWND
        self.hwnd = int(self.winId())
        protect_window(self.hwnd)
        if self._edit_mode:
            disable_click_through(self.hwnd)
        else:
            enable_click_through(self.hwnd)
            self._save_config()

        print(f"[EDIT] Edit mode: {'ON (drag me!)' if self._edit_mode else 'OFF'}")

    # -------- mouse drag / resize (edit mode only) --------
    def mousePressEvent(self, e):
        if not self._edit_mode or e.button() != Qt.LeftButton:
            return
        pos = e.pos()
        near_br = (pos.x() > self.width()  - self.RESIZE_GRIP and
                   pos.y() > self.height() - self.RESIZE_GRIP)
        self._drag_op     = "resize" if near_br else "move"
        self._drag_anchor = e.globalPos()
        self._drag_geom   = self.geometry()
        self.setCursor(Qt.SizeFDiagCursor if near_br else Qt.SizeAllCursor)

    def mouseMoveEvent(self, e):
        if not self._edit_mode:
            return
        # cursor feedback when hovering bottom-right corner
        if self._drag_anchor is None:
            pos = e.pos()
            near_br = (pos.x() > self.width()  - self.RESIZE_GRIP and
                       pos.y() > self.height() - self.RESIZE_GRIP)
            self.setCursor(Qt.SizeFDiagCursor if near_br else Qt.ArrowCursor)
            return

        delta = e.globalPos() - self._drag_anchor
        g = self._drag_geom
        if self._drag_op == "move":
            self.setGeometry(g.x() + delta.x(), g.y() + delta.y(),
                             g.width(), g.height())
        else:
            w = max(self.MIN_W, min(g.width()  + delta.x(), self.MAX_W))
            h = max(self.MIN_H, min(g.height() + delta.y(), self.MAX_H))
            self.setGeometry(g.x(), g.y(), w, h)

    def mouseReleaseEvent(self, e):
        self._drag_anchor = None
        self._drag_geom   = None
        self._drag_op     = None
        self.setCursor(Qt.ArrowCursor)
        self._save_config()

    # -------- persistence --------
    def _save_config(self):
        try:
            data = {
                "x": self.x(), "y": self.y(),
                "w": self.width(), "h": self.height(),
            }
            CONFIG_PATH.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print("[CFG] save failed:", e)

    def _load_config(self):
        try:
            data = json.loads(CONFIG_PATH.read_text())
            self.setGeometry(data["x"], data["y"], data["w"], data["h"])
            print(f"[CFG] loaded: {data}")
            return True
        except Exception:
            return False


# ---------- Controller ----------
class OverlayController(QObject):

    sig_text        = pyqtSignal(str)
    sig_toggle      = pyqtSignal()
    sig_scroll_up   = pyqtSignal()
    sig_scroll_down = pyqtSignal()

    sig_move        = pyqtSignal(int, int)
    sig_resize      = pyqtSignal(int, int)
    sig_center      = pyqtSignal()
    sig_edit        = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.window = OverlayWindow()
        self.hook   = Hook()

        self.sig_text       .connect(self.window.update_text)
        self.sig_toggle     .connect(self._on_toggle)
        self.sig_scroll_up  .connect(self.window.scroll_up)
        self.sig_scroll_down.connect(self.window.scroll_down)
        self.sig_move       .connect(self.window.move_by)
        self.sig_resize     .connect(self.window.resize_by)
        self.sig_center     .connect(self.window.center)
        self.sig_edit       .connect(self.window.toggle_edit_mode)

    def _on_toggle(self):
        if self.window.isVisible():
            self.window.hide()
            print("[HIDDEN]")
        else:
            self.window.show()
            print("[SHOWN]")

    # ---- hotkey callbacks (emit signals → safe cross-thread) ----
    def toggle_overlay(self): self.sig_toggle.emit()

    def scroll_up(self):      self.sig_scroll_up.emit()
    def scroll_down(self):    self.sig_scroll_down.emit()

    def move_left(self):      self.sig_move.emit(-self.window.MOVE_STEP, 0)
    def move_right(self):     self.sig_move.emit( self.window.MOVE_STEP, 0)
    def move_up(self):        self.sig_move.emit(0, -self.window.MOVE_STEP)
    def move_down(self):      self.sig_move.emit(0,  self.window.MOVE_STEP)

    def grow_wider(self):     self.sig_resize.emit( self.window.RESIZE_STEP, 0)
    def shrink_wider(self):   self.sig_resize.emit(-self.window.RESIZE_STEP, 0)
    def grow_taller(self):    self.sig_resize.emit(0,  self.window.RESIZE_STEP)
    def shrink_taller(self):  self.sig_resize.emit(0, -self.window.RESIZE_STEP)

    def center(self):         self.sig_center.emit()
    def toggle_edit(self):    self.sig_edit.emit()

    # ---- websocket ----
    async def websocket_listener(self, uri):
        while True:
            try:
                async with websockets.connect(uri) as ws:
                    print(f"[WS] Connected to {uri}")
                    async for msg in ws:
                        data = json.loads(msg)
                        if data.get("action") == "clear":
                            self.sig_text.emit("")
                        else:
                            text = data.get("text", "")
                            self.sig_text.emit(text)
                            print(f"[MSG] {text[:60]}{'...' if len(text) > 60 else ''}")
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                print(f"[WS] lost: {e}. Reconnect in 3s.")
                await asyncio.sleep(3)
            except Exception as e:
                print(f"[WS] err: {e}. Retry in 5s.")
                await asyncio.sleep(5)

    def start_websocket(self, uri):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.websocket_listener(uri))

    # ---- run ----
    def run(self, server_uri):
        # visibility / scroll
        self.hook.bind("ctrl+shift+space", self.toggle_overlay)
        self.hook.bind("ctrl+shift+up",    self.scroll_up)
        self.hook.bind("ctrl+shift+down",  self.scroll_down)

        # move (Ctrl+Alt+Arrows)
        self.hook.bind("ctrl+alt+left",  self.move_left)
        self.hook.bind("ctrl+alt+right", self.move_right)
        self.hook.bind("ctrl+alt+up",    self.move_up)
        self.hook.bind("ctrl+alt+down",  self.move_down)

        # resize (Ctrl+Alt+Shift+Arrows)
        self.hook.bind("ctrl+alt+shift+left",  self.shrink_wider)
        self.hook.bind("ctrl+alt+shift+right", self.grow_wider)
        self.hook.bind("ctrl+alt+shift+up",    self.shrink_taller)
        self.hook.bind("ctrl+alt+shift+down",  self.grow_taller)

        # utility
        self.hook.bind("ctrl+alt+c", self.center)
        self.hook.bind("ctrl+alt+e", self.toggle_edit)

        threading.Thread(
            target=self.start_websocket,
            args=(server_uri,),
            daemon=True,
        ).start()

        self.window.show()
        print("""
[RUNNING]
  Ctrl+Shift+Space        toggle overlay
  Ctrl+Shift+Up / Down    scroll text
  Ctrl+Alt+Arrows         move overlay
  Ctrl+Alt+Shift+Arrows   resize overlay
  Ctrl+Alt+E              toggle EDIT mode (mouse drag + corner resize)
  Ctrl+Alt+C              re-center
""")


if __name__ == "__main__":
    SERVER_URI = "wss://pr.shoaib.com.np"
    app = QApplication(sys.argv)
    ctrl = OverlayController()
    ctrl.run(SERVER_URI)
    sys.exit(app.exec_())