"""Unit tests for 2048 console game core engine."""

import pytest

try:
    from examples.game_2048.core import Direction, Game2048, slide_and_merge_line
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from examples.game_2048.core import Direction, Game2048, slide_and_merge_line


class TestSlideAndMergeLine:
    def test_empty_line(self):
        line = [0, 0, 0, 0]
        result, gain = slide_and_merge_line(line)
        assert result == [0, 0, 0, 0]
        assert gain == 0

    def test_no_merges(self):
        line = [2, 4, 8, 16]
        result, gain = slide_and_merge_line(line)
        assert result == [2, 4, 8, 16]
        assert gain == 0

    def test_simple_merge(self):
        line = [2, 2, 0, 0]
        result, gain = slide_and_merge_line(line)
        assert result == [4, 0, 0, 0]
        assert gain == 4

    def test_slide_with_zeros(self):
        line = [0, 2, 0, 2]
        result, gain = slide_and_merge_line(line)
        assert result == [4, 0, 0, 0]
        assert gain == 4

    def test_double_merge(self):
        line = [2, 2, 2, 2]
        result, gain = slide_and_merge_line(line)
        assert result == [4, 4, 0, 0]
        assert gain == 8

    def test_triple_tiles(self):
        line = [2, 2, 2, 0]
        result, gain = slide_and_merge_line(line)
        assert result == [4, 2, 0, 0]
        assert gain == 4

    def test_prevent_cascade_merge(self):
        line = [4, 2, 2, 0]
        result, gain = slide_and_merge_line(line)
        assert result == [4, 4, 0, 0]
        assert gain == 4


class TestGame2048Initialization:
    def test_default_init(self):
        game = Game2048(auto_spawn=False)
        assert game.size == 4
        assert game.score == 0
        assert game.won is False
        assert game.game_over is False
        assert len(game.get_empty_cells()) == 16

    def test_reset_with_spawn(self):
        game = Game2048(seed=42)
        game.reset(spawn_initial=True)
        assert len(game.get_empty_cells()) == 14

    def test_spawn_tile(self):
        game = Game2048(auto_spawn=False)
        spawned = game.spawn_tile(value=2)
        assert spawned is not None
        r, c, val = spawned
        assert game.board[r][c] == 2
        assert val == 2
        assert len(game.get_empty_cells()) == 15

    def test_spawn_tile_full_board(self):
        game = Game2048(auto_spawn=False)
        game.set_board([[2] * 4 for _ in range(4)])
        assert game.spawn_tile() is None


class TestGame2048Moves:
    def test_move_left(self):
        game = Game2048(auto_spawn=False)
        game.set_board([
            [2, 0, 2, 0],
            [4, 4, 0, 0],
            [0, 0, 0, 0],
            [2, 2, 2, 2],
        ])
        changed = game.move_left()
        assert changed is True
        assert game.board == [
            [4, 0, 0, 0],
            [8, 0, 0, 0],
            [0, 0, 0, 0],
            [4, 4, 0, 0],
        ]
        assert game.score == 4 + 8 + 8

    def test_move_right(self):
        game = Game2048(auto_spawn=False)
        game.set_board([
            [2, 0, 2, 0],
            [0, 0, 4, 4],
            [0, 0, 0, 0],
            [2, 2, 2, 0],
        ])
        changed = game.move_right()
        assert changed is True
        assert game.board == [
            [0, 0, 0, 4],
            [0, 0, 0, 8],
            [0, 0, 0, 0],
            [0, 0, 2, 4],
        ]
        assert game.score == 4 + 8 + 4

    def test_move_up(self):
        game = Game2048(auto_spawn=False)
        game.set_board([
            [2, 4, 0, 2],
            [0, 4, 0, 2],
            [2, 0, 0, 2],
            [0, 0, 0, 2],
        ])
        changed = game.move_up()
        assert changed is True
        assert game.board == [
            [4, 8, 0, 4],
            [0, 0, 0, 4],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ]
        assert game.score == 4 + 8 + 8

    def test_move_down(self):
        game = Game2048(auto_spawn=False)
        game.set_board([
            [2, 4, 0, 2],
            [0, 4, 0, 2],
            [2, 0, 0, 2],
            [0, 0, 0, 2],
        ])
        changed = game.move_down()
        assert changed is True
        assert game.board == [
            [0, 0, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 4],
            [4, 8, 0, 4],
        ]
        assert game.score == 4 + 8 + 8

    def test_move_no_change(self):
        game = Game2048(auto_spawn=False)
        game.set_board([
            [2, 4, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ])
        changed = game.move_left()
        assert changed is False
        assert game.score == 0

    def test_direction_aliases(self):
        game = Game2048(auto_spawn=False)
        game.set_board([[0, 2, 0, 0]] + [[0] * 4 for _ in range(3)])
        assert game.move("a") is True
        assert game.board[0][0] == 2

    def test_invalid_direction(self):
        game = Game2048(auto_spawn=False)
        with pytest.raises(ValueError, match="Invalid direction"):
            game.move("INVALID")


class TestGame2048WinAndLoss:
    def test_win_detection(self):
        game = Game2048(target=2048, auto_spawn=False)
        game.set_board([
            [1024, 1024, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ])
        assert game.is_won() is False
        game.move_left()
        assert game.board[0][0] == 2048
        assert game.is_won() is True
        assert game.won is True

    def test_can_move_with_empty_cells(self):
        game = Game2048(auto_spawn=False)
        game.set_board([
            [2, 4, 8, 16],
            [32, 64, 128, 256],
            [512, 1024, 2, 4],
            [8, 16, 32, 0],
        ])
        assert game.can_move() is True
        assert game.is_game_over() is False

    def test_can_move_full_board_with_adjacent_match(self):
        game = Game2048(auto_spawn=False)
        game.set_board([
            [2, 4, 8, 16],
            [32, 64, 128, 256],
            [512, 1024, 2, 4],
            [8, 16, 32, 32],
        ])
        assert game.can_move() is True
        assert game.is_game_over() is False

    def test_game_over(self):
        game = Game2048(auto_spawn=False)
        game.set_board([
            [2, 4, 2, 4],
            [4, 2, 4, 2],
            [2, 4, 2, 4],
            [4, 2, 4, 2],
        ])
        assert game.can_move() is False
        assert game.is_game_over() is True
        assert game.game_over is True

    def test_set_board_clears_win_and_loss_flags(self):
        game = Game2048(target=2048, auto_spawn=False)
        game.set_board([
            [2048, 0, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ])
        assert game.won is True
        # Reset with new playable board
        game.set_board([
            [2, 4, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ])
        assert game.won is False
        assert game.game_over is False


class TestGame2048Render:
    def test_render_output(self):
        game = Game2048(auto_spawn=False)
        game.set_board([[2, 4, 0, 0]] + [[0] * 4 for _ in range(3)], score=100)
        output = game.render()
        assert "Score: 100" in output
        assert "2" in output
        assert "4" in output
