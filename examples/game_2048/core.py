"""2048 Game Core Engine."""

from __future__ import annotations

import random
from enum import Enum
from typing import List, Optional, Tuple


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"


def slide_and_merge_line(line: List[int]) -> Tuple[List[int], int]:
    """Slide and merge a single 1D row/column line towards the left.

    Args:
        line: List of integers representing a line on the board.

    Returns:
        A tuple of (merged_line, score_gained).
    """
    non_zeros = [x for x in line if x != 0]
    merged: List[int] = []
    score_gain = 0
    skip = False

    for i in range(len(non_zeros)):
        if skip:
            skip = False
            continue
        if i + 1 < len(non_zeros) and non_zeros[i] == non_zeros[i + 1]:
            combined = non_zeros[i] * 2
            merged.append(combined)
            score_gain += combined
            skip = True
        else:
            merged.append(non_zeros[i])

    while len(merged) < len(line):
        merged.append(0)

    return merged, score_gain


class Game2048:
    """Core 2048 Game Engine."""

    def __init__(
        self,
        size: int = 4,
        target: int = 2048,
        seed: Optional[int] = None,
        auto_spawn: bool = True,
    ) -> None:
        self.size = size
        self.target = target
        self.rng = random.Random(seed)
        self.auto_spawn = auto_spawn
        self.board: List[List[int]] = [[0] * size for _ in range(size)]
        self.score = 0
        self.won = False
        self.game_over = False
        if self.auto_spawn:
            self.spawn_tile()
            self.spawn_tile()

    def reset(self, spawn_initial: bool = True) -> None:
        """Reset the game board and game state."""
        self.board = [[0] * self.size for _ in range(self.size)]
        self.score = 0
        self.won = False
        self.game_over = False
        if spawn_initial:
            self.spawn_tile()
            self.spawn_tile()

    def get_empty_cells(self) -> List[Tuple[int, int]]:
        """Return list of (row, col) coordinates for empty cells."""
        return [
            (r, c)
            for r in range(self.size)
            for c in range(self.size)
            if self.board[r][c] == 0
        ]

    def spawn_tile(self, value: Optional[int] = None) -> Optional[Tuple[int, int, int]]:
        """Spawn a tile in a random empty cell.

        90% chance of 2, 10% chance of 4 (unless value is specified).
        Returns (row, col, value) if spawned, or None if board is full.
        """
        empty_cells = self.get_empty_cells()
        if not empty_cells:
            return None
        r, c = self.rng.choice(empty_cells)
        if value is None:
            val = 2 if self.rng.random() < 0.9 else 4
        else:
            val = value
        self.board[r][c] = val
        self._check_status()
        return (r, c, val)

    def set_board(self, grid: List[List[int]], score: Optional[int] = None) -> None:
        """Set board state directly (for testing or loading games)."""
        self.size = len(grid)
        self.board = [list(row) for row in grid]
        if score is not None:
            self.score = score
        self.won = self.is_won()
        self.game_over = self.is_game_over()

    def can_move(self) -> bool:
        """Check if any legal move is possible."""
        if self.get_empty_cells():
            return True
        for r in range(self.size):
            for c in range(self.size):
                val = self.board[r][c]
                if r + 1 < self.size and self.board[r + 1][c] == val:
                    return True
                if c + 1 < self.size and self.board[r][c + 1] == val:
                    return True
        return False

    def is_won(self) -> bool:
        """Check if target tile value has been reached."""
        return any(val >= self.target for row in self.board for val in row)

    def is_game_over(self) -> bool:
        """Check if game is over (no valid moves remaining)."""
        return not self.can_move()

    def _check_status(self) -> None:
        """Update won and game_over state flags."""
        if self.is_won():
            self.won = True
        self.game_over = self.is_game_over()

    def move(self, direction: Direction | str) -> bool:
        """Execute a move in the given direction.

        Supports Direction enum and string inputs ('UP', 'DOWN', 'LEFT', 'RIGHT',
        or 'w', 's', 'a', 'd').
        Returns True if board state changed, False otherwise.
        """
        if isinstance(direction, str):
            dir_str = direction.strip().upper()
            dir_map = {
                "W": Direction.UP,
                "UP": Direction.UP,
                "S": Direction.DOWN,
                "DOWN": Direction.DOWN,
                "A": Direction.LEFT,
                "LEFT": Direction.LEFT,
                "D": Direction.RIGHT,
                "RIGHT": Direction.RIGHT,
            }
            if dir_str not in dir_map:
                raise ValueError(f"Invalid direction: {direction}")
            d = dir_map[dir_str]
        else:
            d = direction

        original_board = [list(row) for row in self.board]
        total_score_gain = 0

        if d == Direction.LEFT:
            new_board = []
            for row in self.board:
                merged_row, gain = slide_and_merge_line(row)
                new_board.append(merged_row)
                total_score_gain += gain
            self.board = new_board

        elif d == Direction.RIGHT:
            new_board = []
            for row in self.board:
                rev = list(reversed(row))
                merged_rev, gain = slide_and_merge_line(rev)
                new_board.append(list(reversed(merged_rev)))
                total_score_gain += gain
            self.board = new_board

        elif d == Direction.UP:
            new_board = [[0] * self.size for _ in range(self.size)]
            for c in range(self.size):
                col = [self.board[r][c] for r in range(self.size)]
                merged_col, gain = slide_and_merge_line(col)
                total_score_gain += gain
                for r in range(self.size):
                    new_board[r][c] = merged_col[r]
            self.board = new_board

        elif d == Direction.DOWN:
            new_board = [[0] * self.size for _ in range(self.size)]
            for c in range(self.size):
                col = [self.board[r][c] for r in reversed(range(self.size))]
                merged_col, gain = slide_and_merge_line(col)
                total_score_gain += gain
                for r in range(self.size):
                    new_board[self.size - 1 - r][c] = merged_col[r]
            self.board = new_board

        changed = self.board != original_board

        if changed:
            self.score += total_score_gain
            if self.auto_spawn:
                self.spawn_tile()
            self._check_status()

        return changed

    def move_up(self) -> bool:
        return self.move(Direction.UP)

    def move_down(self) -> bool:
        return self.move(Direction.DOWN)

    def move_left(self) -> bool:
        return self.move(Direction.LEFT)

    def move_right(self) -> bool:
        return self.move(Direction.RIGHT)

    def render(self) -> str:
        """Render board to formatted console string."""
        lines = [f"Score: {self.score} | Target: {self.target}"]
        if self.won:
            lines.append("★ You Won! ★")
        if self.game_over:
            lines.append("✖ Game Over ✖")

        border = "+" + "+".join(["------"] * self.size) + "+"
        lines.append(border)
        for row in self.board:
            row_str = "|" + "|".join(f"{x:^6}" if x != 0 else "      " for x in row) + "|"
            lines.append(row_str)
            lines.append(border)
        return "\n".join(lines)
