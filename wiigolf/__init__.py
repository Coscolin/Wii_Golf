"""wiigolf - lectura de Wii Balance Board(s) para análisis del swing de golf."""

from .balance_board import BalanceBoard, NINTENDO_VID, BALANCE_BOARD_PID

__all__ = ["BalanceBoard", "NINTENDO_VID", "BALANCE_BOARD_PID"]
__version__ = "0.2.0"
