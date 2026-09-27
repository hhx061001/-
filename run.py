# -*- coding: utf-8 -*-
"""PySide6 + QWebEngineView 窗口壳。"""

import json
import os
import threading
import sys
import ctypes
from datetime import datetime

# Qt WebEngine can leave unpainted bands over scrolling dialogs on some Windows
# graphics drivers. Use its documented software-rendering fallback by default.
if sys.platform == "win32":
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QObject, QRect, Qt, QUrl, Slot
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from werkzeug.datastructures import FileStorage

from app import (
    app,
    export_excel,
    find_free_port,
    get_db,
    import_file_object,
    init_db,
)


class WindowGlyphButton(QPushButton):
    """手绘的最小化/最大化/关闭按钮，避免依赖系统符号字体。"""

    def __init__(self, kind, parent=None):
        super().__init__("", parent)
        self.kind = kind
        self.is_maximized = False
        self.setFixedSize(42, 38)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(QFont("Microsoft YaHei", 9))

    def set_maximized(self, maximized):
        self.is_maximized = maximized
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self.kind == "close" and self.underMouse():
            color = QColor("#ffffff")
        elif self.kind == "close":
            color = QColor("#61666b")
        elif self.underMouse():
            color = QColor("#0f1115")
        else:
            color = QColor("#61666b")

        pen = QPen(color, 1.25)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        c = QRect(0, 0, self.width(), self.height()).center()

        if self.kind == "min":
            painter.drawLine(c.x() - 5, c.y(), c.x() + 5, c.y())
        elif self.kind == "max":
            if self.is_maximized:
                painter.drawRect(QRect(c.x() - 4, c.y() - 3, 8, 8))
                painter.drawRect(QRect(c.x() - 2, c.y() - 6, 8, 8))
            else:
                painter.drawRect(QRect(c.x() - 5, c.y() - 5, 10, 10))
        elif self.kind == "close":
            painter.drawLine(c.x() - 4, c.y() - 4, c.x() + 4, c.y() + 4)
            painter.drawLine(c.x() + 4, c.y() - 4, c.x() - 4, c.y() + 4)

        painter.end()


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("titleBar")
        self.window = window
        self.drag_pos = None
        self.setFixedHeight(52)
        self.setFont(QFont("Microsoft YaHei", 9))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(8)

        self.btn_nav = QToolButton()
        self.btn_nav.setObjectName("navBtn")
        self.btn_nav.setText("☰")
        self.btn_nav.setToolTip("展开或收起侧边栏")
        self.btn_nav.setFixedSize(36, 36)
        self.btn_nav.setCursor(Qt.PointingHandCursor)
        self.btn_nav.setFont(QFont("Microsoft YaHei", 9))

        self.btn_import = QToolButton()
        self.btn_import.setObjectName("barBtn")
        self.btn_import.setText("导入账单")
        self.btn_import.setPopupMode(QToolButton.InstantPopup)
        self.btn_import.setFont(QFont("Microsoft YaHei", 9))
        self.import_menu = QMenu(self.btn_import)
        self.btn_import.setMenu(self.import_menu)

        self.btn_export = QToolButton()
        self.btn_export.setObjectName("barBtn")
        self.btn_export.setText("导出报表")
        self.btn_export.setPopupMode(QToolButton.InstantPopup)
        self.btn_export.setFont(QFont("Microsoft YaHei", 9))
        self.export_menu = QMenu(self.btn_export)
        self.btn_export.setMenu(self.export_menu)

        self.app_mark = QLabel("账")
        self.app_mark.setObjectName("appMark")
        self.app_mark.setFixedSize(27, 27)
        self.app_mark.setAlignment(Qt.AlignCenter)
        self.app_mark.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.app_title = QLabel("账单分析")
        self.app_title.setObjectName("appTitle")
        self.app_title.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout.addWidget(self.btn_nav)
        layout.addWidget(self.app_mark)
        layout.addWidget(self.app_title)
        layout.addSpacing(18)
        layout.addWidget(self.btn_import)
        layout.addWidget(self.btn_export)
        layout.addStretch()

        self.btn_min = WindowGlyphButton("min")
        self.btn_max = WindowGlyphButton("max")
        self.btn_close = WindowGlyphButton("close")
        for btn in (self.btn_min, self.btn_max, self.btn_close):
            btn.setObjectName("winBtn")
        self.btn_close.setObjectName("closeBtn")

        self.btn_min.clicked.connect(window.showMinimized)
        self.btn_max.clicked.connect(self.toggle_maximize)
        self.btn_close.clicked.connect(window.close)

        layout.addWidget(self.btn_min)
        layout.addWidget(self.btn_max)
        layout.addWidget(self.btn_close)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.LeftButton and self.drag_pos:
            if self.window.isMaximized():
                return
            delta = event.globalPosition().toPoint() - self.drag_pos
            self.window.move(self.window.pos() + delta)
            self.drag_pos = event.globalPosition().toPoint()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.drag_pos = None
            self.toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def toggle_maximize(self):
        if self.window.isMaximized():
            self.window.showNormal()
        else:
            self.window.showMaximized()
        self.btn_max.set_maximized(self.window.isMaximized())
        apply_corners = getattr(self.window, "apply_window_corners", None)
        if apply_corners:
            apply_corners(not self.window.isMaximized())


class Bridge(QObject):
    def __init__(self, window, title_bar, web=None):
        super().__init__()
        self.window = window
        self.title_bar = title_bar
        self.web = web

    @Slot()
    def toggle_sidebar(self):
        if self.web:
            self.web.page().runJavaScript(
                "document.querySelector('.sidebar').classList.toggle('sidebar-collapsed');"
            )

    @Slot(str)
    def import_files(self, branch):
        files, _ = QFileDialog.getOpenFileNames(
            None,
            "选择账单文件",
            "",
            "账单文件 (*.csv *.xlsx *.xls)",
        )
        if not files:
            return json.dumps({"ok": False, "cancelled": True}, ensure_ascii=False)
        summaries = []
        for path in files:
            with open(path, "rb") as f:
                fs = FileStorage(stream=f, filename=os.path.basename(path))
                result = import_file_object(fs, branch)
                summaries.append(
                    {
                        "filename": result.get("filename"),
                        "inserted": result.get("inserted", 0),
                        "skipped": result.get("skipped", 0),
                        "error": result.get("error"),
                    }
                )
        if self.web:
            if any(item.get("error") for item in summaries):
                self.web.page().runJavaScript(
                    "switchPage('import'); showFlash('部分文件未导入，请在导入页面重新选择并查看提示', true);"
                )
            else:
                self.web.page().runJavaScript(
                    "showFlash('导入完成'); loadMonths(); loadDetail(); loadSummary(); loadLedgers();"
                )
        return json.dumps({"ok": True, "results": summaries}, ensure_ascii=False)

    @Slot(str, str)
    def export_ledger(self, month, branch):
        try:
            data = export_excel(month, None, branch)
            label = "总账本" if branch == "生活" else branch
            filename = f"账单分析_{label}_{month or '全部'}.xlsx"
            path, _ = QFileDialog.getSaveFileName(
                None,
                "保存 Excel",
                filename,
                "Excel 文件 (*.xlsx)",
            )
            if not path:
                return json.dumps({"ok": False, "cancelled": True}, ensure_ascii=False)
            with open(path, "wb") as f:
                f.write(data.getvalue())
            if self.web:
                self.web.page().runJavaScript(
                    f"showFlash('已保存到：{path}');"
                )
            return json.dumps({"ok": True, "path": path}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot()
    def minimize_window(self):
        self.window.showMinimized()

    @Slot()
    def toggle_maximize(self):
        self.title_bar.toggle_maximize()

    @Slot()
    def close_window(self):
        self.window.close()

    @Slot(str, list, result=str)
    def save_export(self, month, columns):
        try:
            data = export_excel(month, columns)
            filename = f"账单分析_{month or '全部'}.xlsx"
            path, _ = QFileDialog.getSaveFileName(
                None,
                "保存 Excel",
                filename,
                "Excel 文件 (*.xlsx)",
            )
            if not path:
                return json.dumps({"ok": False, "cancelled": True}, ensure_ascii=False)
            with open(path, "wb") as f:
                f.write(data.getvalue())
            return json.dumps({"ok": True, "path": path}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def start_flask(port):
    app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


def set_window_corners(window, rounded=True):
    """Windows 11 原生圆角，失败时保持直角，不影响程序启动。"""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(window.winId())
        value = 2 if rounded else 1  # DWMWCP_ROUND / DWMWCP_DONOTROUND
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(ctypes.c_int(value)),
            ctypes.sizeof(ctypes.c_int),
        )
    except Exception:
        pass


def main():
    init_db()
    port = find_free_port()
    threading.Thread(target=start_flask, args=(port,), daemon=True).start()

    qt_app = QApplication(sys.argv)
    qt_app.setFont(QFont("Microsoft YaHei", 9))
    qt_app.setStyleSheet(
        """
        QWidget#titleBar {
            background: #f8faff;
            border-bottom: 1px solid #dce5f4;
        }
        QLabel#appMark {
            background: #365de3;
            color: white;
            border-radius: 7px;
            font-size: 15px;
            font-weight: 700;
        }
        QLabel#appTitle {
            color: #1d2c48;
            font-size: 13px;
            font-weight: 700;
            padding-right: 3px;
        }
        QPushButton#winBtn {
            border: none;
            background: transparent;
            border-radius: 8px;
            color: #61666b;
            font-size: 16px;
            font-family: "Microsoft YaHei";
        }
        QPushButton#winBtn:hover {
            background: #f1f3f5;
            color: #0f1115;
        }
        QPushButton#closeBtn {
            border: none;
            background: transparent;
            border-radius: 8px;
            color: #61666b;
            font-size: 16px;
            font-family: "Microsoft YaHei";
        }
        QPushButton#closeBtn:hover {
            background: #e5484d;
            color: #ffffff;
        }
        QToolButton#navBtn, QToolButton#barBtn {
            border: none;
            background: transparent;
            border-radius: 9px;
            color: #344461;
            font-size: 13px;
            height: 34px;
            font-family: "Microsoft YaHei";
        }
        QToolButton#barBtn {
            border: 1px solid #dce5f4;
            background: #ffffff;
            padding: 0 13px;
        }
        QToolButton#barBtn::menu-indicator {
            image: none;
            width: 0;
        }
        QToolButton#navBtn:hover, QToolButton#barBtn:hover {
            background: #eaf0ff;
            border-color: #bfcdf5;
            color: #274dd0;
        }
        QMenu {
            background: #ffffff;
            color: #0f1115;
            border: 1px solid rgba(0, 0, 0, 0.10);
            border-radius: 8px;
            padding: 4px;
            font-family: "Microsoft YaHei";
            font-size: 13px;
        }
        QMenu::item {
            padding: 7px 24px 7px 12px;
            border-radius: 6px;
            background: transparent;
        }
        QMenu::item:selected {
            background: #edf3fe;
            color: #3964fe;
        }
        """
    )
    window = QMainWindow()
    window.setWindowFlags(Qt.FramelessWindowHint)
    window.resize(1280, 860)
    window.apply_window_corners = lambda rounded=True: set_window_corners(window, rounded)

    title_bar = TitleBar(window)
    web = QWebEngineView()
    web.setUrl(QUrl(f"http://127.0.0.1:{port}"))

    bridge = Bridge(window, title_bar, web)
    channel = QWebChannel()
    channel.registerObject("bridge", bridge)
    web.page().setWebChannel(channel)

    shim = """
    window.pywebview = {
      api: {
        minimize_window: function() { window.bridge.minimize_window(); },
        toggle_maximize: function() { window.bridge.toggle_maximize(); },
        close_window: function() { window.bridge.close_window(); },
        save_export: function(month, columns) {
          return new Promise(function(resolve) {
            window.bridge.save_export(month, columns || [], function(result) {
              resolve(JSON.parse(result));
            });
          });
        }
      }
    };
    """
    web.loadFinished.connect(lambda ok: web.page().runJavaScript(shim))

    title_bar.btn_nav.clicked.connect(bridge.toggle_sidebar)

    def ledger_names():
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT name FROM ledgers WHERE deleted=0 ORDER BY id ASC"
            ).fetchall()
            return [r["name"] for r in rows]
        finally:
            conn.close()

    def refresh_menus():
        names = ledger_names()
        title_bar.import_menu.clear()
        title_bar.export_menu.clear()

        total_import = title_bar.import_menu.addAction("总账本")
        total_import.triggered.connect(lambda: bridge.import_files("生活"))
        for name in names:
            action = title_bar.import_menu.addAction(name)
            action.triggered.connect(
                lambda checked=False, n=name: bridge.import_files(n)
            )

        total_export = title_bar.export_menu.addMenu("总账本")
        total_month = total_export.addAction("导出本月")
        total_month.triggered.connect(
            lambda: bridge.export_ledger(datetime.now().strftime("%Y-%m"), "生活")
        )
        total_all = total_export.addAction("导出全部")
        total_all.triggered.connect(lambda: bridge.export_ledger("", "生活"))
        for name in names:
            sub = title_bar.export_menu.addMenu(name)
            month_action = sub.addAction("导出本月")
            month_action.triggered.connect(
                lambda checked=False, n=name: bridge.export_ledger(
                    datetime.now().strftime("%Y-%m"), n
                )
            )
            all_action = sub.addAction("导出全部")
            all_action.triggered.connect(
                lambda checked=False, n=name: bridge.export_ledger("", n)
            )

    refresh_menus()

    main_widget = QWidget()
    main_widget.setStyleSheet("background: #f9fafb;")
    main_layout = QVBoxLayout(main_widget)
    main_layout.setContentsMargins(0, 0, 0, 0)
    main_layout.setSpacing(0)
    main_layout.addWidget(title_bar)
    main_layout.addWidget(web)
    window.setCentralWidget(main_widget)
    window.show()
    window.apply_window_corners(True)
    sys.exit(qt_app.exec())


if __name__ == "__main__":
    main()
