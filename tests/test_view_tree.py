import unittest

import cshogi

from core.view_tree import ViewTree, move_label


class ViewTreeTests(unittest.TestCase):
    def test_start_board_and_japanese_move_labels(self):
        prefix = ["7g7f", "3c3d"]
        tree = ViewTree(
            {"start_position": "position startpos moves " + " ".join(prefix)}
        )
        tree.add(prefix + ["2g2f"], "black_win", "first")
        data = tree.export()
        self.assertEqual(data["base"], 2)
        self.assertEqual(data["nodes"][1]["label"], "▲２六歩")
        board = cshogi.Board()
        for move in prefix + ["2g2f"]:
            board.push_usi(move)
        self.assertEqual(data["nodes"][1]["sfen"], board.sfen())
        self.assertEqual(data["nodes"][0]["b"], 1)

    def test_merge_repetition_and_counts_are_per_game(self):
        tree = ViewTree({"start_position": "position startpos"})
        cycle = ["5i5h", "5a5b", "5h5i", "5b5a"]
        tree.add(cycle * 3, "unresolved", "repeat")
        tree.add(cycle + ["7g7f"], "white_win", "second")
        data = tree.export()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["nodes"][0]["count"], 2)
        self.assertEqual(data["nodes"][4]["main"], 0)
        self.assertEqual(data["nodes"][4]["count"], 2)
        self.assertEqual(data["nodes"][5]["parent"], 0)
        self.assertEqual(data["nodes"][5]["label"], "▲７六歩")
        self.assertEqual(data["nodes"][5]["w"], 1)
        self.assertTrue(all(n["parent"] < n["id"] for n in data["nodes"]))

    def test_promotion_and_drop_labels(self):
        board = cshogi.Board("4k4/9/4P4/9/9/9/9/9/4K4 b B 1")
        self.assertEqual(move_label(board, "5c5b+"), "▲５二歩成")
        self.assertEqual(move_label(board, "B*4c"), "▲４三角打")


if __name__ == "__main__":
    unittest.main()
