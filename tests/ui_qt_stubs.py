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
        self._enabled = True

    def setEnabled(self, value):
        self._enabled = value

    def setAccessibleName(self, name):
        self._accessible_name = name

    def setFont(self, font):
        self._font = font

    def setToolTip(self, text):
        self._tooltip = text

    def clear(self):
        self.setText("")


# ---------------------------------------------------------------------------
# QtCore
# ---------------------------------------------------------------------------


class Qt:
    class TextFormat:
        PlainText = 0

    class AlignmentFlag:
        AlignCenter = 1

    class TextInteractionFlag:
        TextSelectableByMouse = 1

    class ItemDataRole:
        ToolTipRole = 3


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

    def setTextFormat(self, fmt):
        self._format = fmt


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
        self.returnPressed = _Signal()

    def setClearButtonEnabled(self, enabled):
        self._clear_button = enabled

    def setPlaceholderText(self, text):
        self._placeholder = text

    def setReadOnly(self, v):
        self._readonly = v

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setCursorPosition(self, position):
        self._cursor = position


class QComboBox(_QObjectBase):
    def __init__(self, parent=None):
        self._items = []
        self._data = []
        self._roles = []
        self._current = -1
        self._enabled = True
        self._tooltip = ""
        self.currentTextChanged = _Signal()
        self.currentIndexChanged = _Signal()

    def addItems(self, items):
        for item in items:
            self.addItem(item)

    def addItem(self, text, userData=None):
        self._items.append(text)
        self._data.append(userData)
        self._roles.append({})
        if self._current == -1:
            self._current = 0

    def count(self):
        return len(self._items)

    def setItemData(self, index, value, role=None):
        self._roles[index][role] = value

    def itemData(self, index, role=None):
        if role is None:
            return self._data[index]
        return self._roles[index].get(role)

    def findData(self, value):
        return self._data.index(value) if value in self._data else -1

    def setCurrentIndex(self, index):
        if 0 <= index < len(self._items) and index != self._current:
            self._current = index
            self.currentIndexChanged.emit(index)
            self.currentTextChanged.emit(self._items[index])

    def currentIndex(self):
        return self._current

    def currentData(self):
        if 0 <= self._current < len(self._data):
            return self._data[self._current]
        return None

    def currentText(self):
        if 0 <= self._current < len(self._items):
            return self._items[self._current]
        return ""

    def setCurrentText(self, text):
        if text in self._items:
            self._current = self._items.index(text)
            self.currentTextChanged.emit(text)

    def setEnabled(self, value):
        self._enabled = bool(value)

    def isEnabled(self):
        return self._enabled

    def setToolTip(self, text):
        self._tooltip = text


class QTextEdit(_QObjectBase):
    def __init__(self, parent=None):
        self._text = ""
        self._readonly = False
        self._max_height = None
        self._stylesheet = ""

    def setReadOnly(self, v):
        self._readonly = v

    def setMinimumHeight(self, h):
        self._min_height = h

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

    def resize(self, width, height):
        self._size = (width, height)

    def findChildren(self, cls):
        return []

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


class QTimer(_QObjectBase):
    def __init__(self, parent=None):
        self.timeout = _Signal()

    def setInterval(self, interval):
        self._interval = interval

    def start(self):
        self._active = True

    def stop(self):
        self._active = False


class QFontDatabase:
    class SystemFont:
        FixedFont = 1

    @staticmethod
    def systemFont(kind):
        return QFont()


class QWidget(_QObjectBase):
    pass


class QTabWidget(QWidget):
    def __init__(self, parent=None):
        self._tabs = []

    def addTab(self, widget, title):
        self._tabs.append((widget, title))


class QHeaderView(_QObjectBase):
    class ResizeMode:
        Stretch = 1
        ResizeToContents = 2

    def setSectionResizeMode(self, *args):
        pass

    def hide(self):
        pass


class QAbstractItemView:
    class SelectionBehavior:
        SelectRows = 1

    class SelectionMode:
        ExtendedSelection = 1

    class EditTrigger:
        NoEditTriggers = 1


class QTableWidgetItem(_QObjectBase):
    def __init__(self, text):
        self._text = text
        self._selected = False

    def text(self):
        return self._text

    def setSelected(self, selected):
        self._selected = selected


class QTableWidget(_QObjectBase):
    def __init__(self, rows, columns):
        self._rows = rows
        self._items = {}
        self.itemSelectionChanged = _Signal()

    def setHorizontalHeaderLabels(self, labels):
        self._labels = labels

    def horizontalHeader(self):
        return QHeaderView()

    verticalHeader = horizontalHeader

    def setSelectionBehavior(self, behavior):
        pass

    def setSelectionMode(self, mode):
        pass

    def setEditTriggers(self, triggers):
        pass

    def setAlternatingRowColors(self, enabled):
        pass

    def setWordWrap(self, enabled):
        pass

    def setRowCount(self, rows):
        self._rows = rows
        self._items = {
            key: value for key, value in self._items.items() if key[0] < rows
        }

    def rowCount(self):
        return self._rows

    def setItem(self, row, column, item):
        self._items[row, column] = item

    def item(self, row, column):
        return self._items.get((row, column))

    def selectionModel(self):
        return self

    def selectedRows(self):
        return [
            types.SimpleNamespace(row=lambda r=row: r)
            for row in range(self._rows)
            if self.item(row, 0) and self.item(row, 0)._selected
        ]

    def clearSelection(self):
        for item in self._items.values():
            item.setSelected(False)

    def blockSignals(self, enabled):
        self._blocked = enabled


class QMessageBox:
    class StandardButton:
        Yes = 1
        No = 2

    @staticmethod
    def question(*args):
        return QMessageBox.StandardButton.No


def install_ui_qt_stubs():
    """Install rich, subclassable PyQt6 stand-ins into sys.modules."""
    qt_core = types.ModuleType("PyQt6.QtCore")
    qt_core.Qt = Qt
    qt_core.QTimer = QTimer

    qt_gui = types.ModuleType("PyQt6.QtGui")
    qt_gui.QFont = QFont
    qt_gui.QFontDatabase = QFontDatabase

    qt_widgets = types.ModuleType("PyQt6.QtWidgets")
    qt_widgets.QAbstractItemView = QAbstractItemView
    qt_widgets.QWidget = QWidget
    qt_widgets.QTableWidget = QTableWidget
    qt_widgets.QTableWidgetItem = QTableWidgetItem
    qt_widgets.QTabWidget = QTabWidget
    qt_widgets.QHeaderView = QHeaderView
    qt_widgets.QMessageBox = QMessageBox
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
