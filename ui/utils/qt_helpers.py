"""Qt thread-safety helpers for ScanHound controllers."""

from PySide6.QtCore import QMetaObject, Qt, Q_ARG


def invoke_on_main(controller, slot_name, *args):
    """Invoke a named @Slot on *controller* via Qt.QueuedConnection.

    Use this to safely marshal calls from a background thread to the
    Qt main thread.  The target method must be decorated with @Slot.
    """
    QMetaObject.invokeMethod(controller, slot_name, Qt.QueuedConnection)


def invoke_on_main_str(controller, slot_name, *str_args):
    """Like invoke_on_main but passes str arguments via Q_ARG."""
    q_args = [Q_ARG(str, a) for a in str_args]
    QMetaObject.invokeMethod(controller, slot_name, Qt.QueuedConnection, *q_args)
