"""USI fault injection subprocess used only by tests."""

import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
position = "position startpos"
loop_cycle = ("5i5h", "5a5b", "5h5i", "5b5a")
loop_index = 0


def current_board():
    import cshogi

    words = position.split()
    board = (
        cshogi.Board(sfen=" ".join(words[2:6]))
        if words[1] == "sfen"
        else cshogi.Board()
    )
    if "moves" in words:
        for move in words[words.index("moves") + 1 :]:
            board.push_usi(move)
    return board


for raw in sys.stdin:
    line = raw.strip()
    if line == "usi":
        if mode == "silent_init":
            continue
        print("id name makebook-test")
        if mode != "minimal":
            print("option name Threads type spin default 1 min 1 max 16")
            print("option name USI_Hash type spin default 16 min 1 max 65536")
            print("option name ResignValue type spin default 300 min 0 max 99999")
            print("option name BookFile type string default no_book")
            print(
                "option name EnteringKingRule type combo default CSARule27 var CSARule27"
            )
            print("option name FV_SCALE type spin default 24 min 1 max 100")
            print("option name MultiPV type spin default 1 min 1 max 100")
            print("option name DrawValueBlack type spin default 0 min -99999 max 99999")
            print("option name DrawValueWhite type spin default 0 min -99999 max 99999")
        print("usiok", flush=True)
    elif line == "isready":
        if mode == "eof":
            break
        if mode != "silent_ready":
            print("readyok", flush=True)
    elif line.startswith("position "):
        position = line
    elif line.startswith("go "):
        if mode == "eof_search":
            break
        if mode == "slow":
            time.sleep(0.3)
        if mode in ("loop", "checking_loop", "white_checking_loop", "broken_checking_loop"):
            cycle = (
                ("5a4a", "5c4c", "4a5a", "4c5c")
                if mode == "checking_loop"
                else ("5i6i", "5g6g", "6i5i", "6g5g")
                if mode == "white_checking_loop"
                else ("5a4a", "5c5d", "4a3a", "5d5c", "3a4a", "5c4c", "4a5a", "4c5c")
                if mode == "broken_checking_loop"
                else loop_cycle
            )
            print("bestmove " + cycle[loop_index % len(cycle)], flush=True)
            loop_index += 1
        elif mode in ("game", "game_slow", "game_delay", "black_win"):
            import cshogi

            board = current_board()
            if mode in ("game_slow", "game_delay"):
                time.sleep(float(sys.argv[2]) if mode == "game_delay" else 0.15)
            move = (
                "resign"
                if (
                    board.turn == cshogi.WHITE
                    if mode == "black_win"
                    else board.move_number > 3
                )
                else cshogi.move_to_usi(next(iter(board.legal_moves)))
            )
            print("bestmove " + move, flush=True)
        elif mode != "silent_search":
            print("info depth 3 nodes 10 score cp 25 pv 7g7f", flush=True)
            print("bestmove 7g7f", flush=True)
    elif line == "stop":
        if mode in ("game", "game_slow", "game_delay"):
            import cshogi

            board = current_board()
            if mode in ("game_slow", "game_delay"):
                time.sleep(float(sys.argv[2]) if mode == "game_delay" else 0.15)
            move = (
                "resign"
                if board.move_number > 3
                else cshogi.move_to_usi(next(iter(board.legal_moves)))
            )
            print("bestmove " + move, flush=True)
        elif mode != "silent_search":
            print("bestmove 7g7f", flush=True)
    elif line == "quit":
        break
