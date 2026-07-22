"""
Rich, real (subclassable) PyQt6 stand-ins used only by test_ui_dialog.py to
genuinely import mcp_server.ui and drive MCPStatusDialog so its statements
are actually executed and counted toward coverage.

Modeled on the stub patterns in moleditpy_nics_placer/tests/conftest.py and
moleditpy_pmeff-plugin/tests/qt_stubs.py. Installed/removed around each test
module via install_ui_qt_stubs()/remove_ui_qt_stubs() so these never leak
into other test files that rely on the blanket MagicMock mock from
tests/conftest.py.
"""

from __future__ import annotations

import sys
import types

_MODULE_NAMES = ("PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets")


class _Signal:
    def __init__(self):
        self._fns = []

    def connect(self, fn):
        self._fns.append(fn)

    def emit(self, *args, **kwargs):
        for fn in list(self._fns):
            try:
                fn(*args, **kwargs)
            except TypeError:
                fn()


class _QObjectBase:
    def __init__(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------------------
# QtCore
# ---------------------------------------------------------------------------


class Qt:
    class AlignmentFlag:
        AlignCenter = 1

    class TextInteractionFlag:
        TextSelectableByMouse = 1


# ---------------------------------------------------------------------------
# QtGui
# ---------------------------------------------------------------------------


class QFont(_QObjectBase):
    def __init__(self, *a, **kw):
        self._bold = False

    def setBold(self, v):
        self._bold = bool(v)


# ---------------------------------------------------------------------------
# QtWidgets
# ---------------------------------------------------------------------------


class _LayoutBase(_QObjectBase):
    def __init__(self, parent=None):
        self._parent = parent
        self._items = []

    def setSpacing(self, n):
        pass

    def addWidget(self, w, stretch=0):
        self._items.append(w)

    def addLayout(self, lay):
        self._items.append(lay)

    def addStretch(self, n=0):
        pass


class QVBoxLayout(_LayoutBase):
    pass


class QHBoxLayout(_LayoutBase):
    pass


class QLabel(_QObjectBase):
    def __init__(self, text="", parent=None):
        self._text = text
        self._alignment = None
        self._flags = None
        self._stylesheet = ""
        self._word_wrap = False

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setAlignment(self, flag):
        self._alignment = flag

    def setFont(self, font):
        self._font = font

    def setTextInteractionFlags(self, flags):
        self._flags = flags

    def setStyleSheet(self, css):
        self._stylesheet = css

    def setWordWrap(self, v):
        self._word_wrap = v


class QCheckBox(_QObjectBase):
    def __init__(self, text="", parent=None):
        self._text = text
        self._checked = False
        self.toggled = _Signal()

    def setChecked(self, v):
        self._checked = bool(v)
        self.toggled.emit(self._checked)

    def isChecked(self):
        return self._checked


class QPushButton(_QObjectBase):
    def __init__(self, text="", parent=None):
        self._text = text
        self.clicked = _Signal()

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setToolTip(self, tip):
        self._tooltip = tip


class QSpinBox(_QObjectBase):
    def __init__(self, parent=None):
        self._value = 0
        self._enabled = True
        self.valueChanged = _Signal()

    def setRange(self, lo, hi):
        self._lo, self._hi = lo, hi

    def setValue(self, v):
        self._value = v

    def value(self):
        return self._value

    def setToolTip(self, tip):
        self._tooltip = tip

    def setEnabled(self, v):
        self._enabled = bool(v)

    def isEnabled(self):
        return self._enabled


class QLineEdit(_QObjectBase):
    def __init__(self, parent=None):
        self._text = ""
        self._placeholder = ""
        self.editingFinished = _Signal()

    def setPlaceholderText(self, text):
        self._placeholder = text

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text


class QComboBox(_QObjectBase):
    def __init__(self, parent=None):
        self._items = []
        self._current = -1
        self.currentTextChanged = _Signal()

    def addItems(self, items):
        self._items.extend(items)
        if self._current == -1 and self._items:
            self._current = 0

    def currentText(self):
        if 0 <= self._current < len(self._items):
            return self._items[self._current]
        return ""

    def setCurrentText(self, text):
        if text in self._items:
            self._current = self._items.index(text)
            self.currentTextChanged.emit(text)


class QTextEdit(_QObjectBase):
    def __init__(self, parent=None):
        self._text = ""
        self._readonly = False
        self._max_height = None
        self._stylesheet = ""

    def setReadOnly(self, v):
        self._readonly = v

    def setMaximumHeight(self, h):
        self._max_height = h

    def setStyleSheet(self, css):
        self._stylesheet = css

    def setPlainText(self, text):
        self._text = text

    def toPlainText(self):
        return self._text


class QDialogButtonBox(_QObjectBase):
    class StandardButton:
        Close = 1

    def __init__(self, buttons=0, parent=None):
        self._mask = buttons
        self.rejected = _Signal()


class QDialog(_QObjectBase):
    def __init__(self, parent=None):
        self._parent = parent
        self._title = ""
        self._min_width = None
        self._closed = False

    def setWindowTitle(self, title):
        self._title = title

    def setMinimumWidth(self, w):
        self._min_width = w

    def close(self):
        self._closed = True


class _FakeClipboard:
    def __init__(self):
        self.text_set = None

    def setText(self, text):
        self.text_set = text


class QApplication:
    _clipboard = _FakeClipboard()

    @staticmethod
    def clipboard():
        return QApplication._clipboard


class QFileDialog:
    """Test-controllable stand-in — set _next_directory before calling."""

    _next_directory = ""

    @staticmethod
    def getExistingDirectory(parent, caption, directory):
        return QFileDialog._next_directory


def install_ui_qt_stubs():
    """Install rich, subclassable PyQt6 stand-ins into sys.modules."""
    qt_core = types.ModuleType("PyQt6.QtCore")
    qt_core.Qt = Qt

    qt_gui = types.ModuleType("PyQt6.QtGui")
    qt_gui.QFont = QFont

    qt_widgets = types.ModuleType("PyQt6.QtWidgets")
    qt_widgets.QApplication = QApplication
    qt_widgets.QCheckBox = QCheckBox
    qt_widgets.QComboBox = QComboBox
    qt_widgets.QDialog = QDialog
    qt_widgets.QDialogButtonBox = QDialogButtonBox
    qt_widgets.QFileDialog = QFileDialog
    qt_widgets.QHBoxLayout = QHBoxLayout
    qt_widgets.QLabel = QLabel
    qt_widgets.QLineEdit = QLineEdit
    qt_widgets.QPushButton = QPushButton
    qt_widgets.QSpinBox = QSpinBox
    qt_widgets.QTextEdit = QTextEdit
    qt_widgets.QVBoxLayout = QVBoxLayout

    pyqt6 = types.ModuleType("PyQt6")
    pyqt6.QtCore = qt_core
    pyqt6.QtGui = qt_gui
    pyqt6.QtWidgets = qt_widgets

    sys.modules["PyQt6"] = pyqt6
    sys.modules["PyQt6.QtCore"] = qt_core
    sys.modules["PyQt6.QtGui"] = qt_gui
    sys.modules["PyQt6.QtWidgets"] = qt_widgets


def remove_ui_qt_stubs():
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)
    sys.modules.pop("mcp_server.ui", None)
