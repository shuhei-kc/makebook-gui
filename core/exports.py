"""Standard SFEN endings and YaneuraOu book output for publication."""

import cshogi

from .learning import key_of


def terminal(game):
    reason = game.get('reason')
    if reason in ('resign', 'mate'):
        return 'resign'
    if reason in ('repetition', 'loop_abort', 'checking_loop_abort'):
        # Twofold checking loops carry a book-update loss in the JSON record,
        # but the standard SFEN stream must not claim an official foul.
        return 'rep_draw'
    if reason == 'perpetual_check':
        # Preserve checking-side losses in frozen old fourfold games.
        return {'black_win': 'rep_white_loss', 'white_win': 'rep_black_loss'}[game['rule_result']]
    if reason in ('max_total_plies', 'max_plies'):
        return 'max_moves'
    if reason == 'declaration':
        return 'win'
    raise ValueError('Unknown completed ending: ' + str(reason))


def book_text(candidates):
    positions = {}
    for row in candidates:
        if not row.get('active'):
            continue
        board = cshogi.Board(row['key'] + ' 1')
        if key_of(board) != row['key'] or not board.is_legal(board.move_from_usi(row['move'])):
            raise ValueError('Illegal active book candidate')
        positions.setdefault(row['key'], []).append(row)
    lines = ['#YANEURAOU-DB2016 1.00']
    for key in sorted(positions):
        lines.append('sfen ' + key + ' 0')
        for row in sorted(positions[key], key=lambda r: (-r.get('wins', 0), r['move'])):
            lines.append(f"{row['move']} none 0 32 {row.get('wins', 0)}")
    return '\n'.join(lines) + '\n', len(positions)
