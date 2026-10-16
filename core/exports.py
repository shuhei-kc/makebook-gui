"""Read-only exports, independent of game control and the live SFEN archive."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

import cshogi

from .learning import key_of
from .sfen_archive import game_line
from .store import atomic_write, ManagerLock

FORMAT_VERSION = 2


def terminal(game):
    reason = game.get('reason')
    if reason in ('resign', 'mate'):
        return 'resign'
    if reason in ('repetition', 'loop_abort'):
        return 'rep_draw'
    if reason == 'perpetual_check':
        # Preserve the recorded checking-side loss; KC2 cannot express it.
        return {'black_win': 'rep_white_loss', 'white_win': 'rep_black_loss'}[game['rule_result']]
    if reason in ('max_total_plies', 'max_plies'):
        return 'max_moves'
    if reason == 'declaration':
        return 'win'
    raise ValueError('Unknown completed ending: ' + str(reason))


def replay(game):
    origin = game.get('origin', 'startpos')
    board = cshogi.Board() if origin == 'startpos' else cshogi.Board(origin.removeprefix('sfen '))
    for move in game.get('moves', []):
        code = board.move_from_usi(move)
        if not board.is_legal(code):
            raise ValueError('Illegal export move: ' + move)
        board.push(code)


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


def project(games, candidates, campaign, seq):
    lines, kc2, index, skipped = [], [], [], []
    ids = set()
    for game in games:
        if not game.get('completed'):
            continue
        if game['campaign_id'] != campaign or game['game_id'] in ids:
            raise ValueError('Wrong campaign or duplicate completed game')
        ids.add(game['game_id'])
        replay(game)
        ending = terminal(game)
        line = game_line(game) + ' moves' if not game.get('moves') else game_line(game)
        line += ' ' + ending
        lines.append(line)
        entry = {name: game.get(name) for name in ('game_id', 'focus', 'prefix_length', 'reason',
                 'rule_result', 'research_result', 'settings_revision', 'settings')}
        entry.update(line=len(lines), terminal=ending)
        index.append(entry)
        # KC2 accepts only these two endings, and computes resign parity from
        # a black-to-move origin. Never relabel a move limit or a declaration.
        origin = game.get('origin', 'startpos')
        black_origin = origin == 'startpos' or origin.split()[2] == 'b'
        if ending in ('resign', 'rep_draw') and black_origin and game.get('moves'):
            kc2.append(line)
        else:
            skipped.append({'game_id': game['game_id'], 'terminal': ending,
                            'reason': 'KC2 unsupported ending or origin'})
    full = ''.join(line + '\n' for line in lines)
    compatible = ''.join(line + '\n' for line in kc2)
    book, positions = book_text(candidates)
    report = {'schema_version': 1, 'export_format_version': FORMAT_VERSION, 'campaign_id': campaign, 'committed_seq': seq,
              'completed': len(lines), 'playing': 0, 'kc2_games': len(kc2), 'kc2_skipped': skipped,
              'book_positions': positions, 'updated_unix': time.time(), 'games': index,
              'sha256': {name: sha256(value.encode()).hexdigest() for name, value in
                         (('completed.sfen', full), ('kc2.sfen', compatible), ('book.db', book))}}
    return full, compatible, book, report


def publish(config, output):
    database = Path(config['runtime_state_dir']) / 'state.sqlite3'
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        seq = connection.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        previous = output / 'index.json'
        prior = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else {}
        if (all((output / name).exists() for name in ('completed.sfen', 'kc2.sfen', 'book.db'))
                and prior.get('committed_seq') == seq and prior.get('export_format_version') == FORMAT_VERSION):
            return None
        games = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM games WHERE json_extract(payload,'$.completed')=1 ORDER BY rowid")]
        candidates = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM candidates')]
    finally:
        connection.close()
    full, compatible, book, report = project(games, candidates, config['campaign_id'], seq)
    for name, content in (('completed.sfen', full), ('kc2.sfen', compatible), ('book.db', book)):
        atomic_write(output / name, content)
    atomic_write(output / 'index.json', json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return {name: value for name, value in report.items() if name not in ('games', 'sha256')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    output = args.output.resolve()
    if output == Path(config['runtime_state_dir']).resolve():
        raise ValueError('Use a separate export directory')
    with ManagerLock(output):  # Export directory only; never the manager lock.
        while True:
            try:
                report = publish(config, output)
                if report:
                    print(json.dumps(report, ensure_ascii=False), flush=True)
            except (OSError, sqlite3.OperationalError) as error:
                if not args.watch:
                    raise
                print(json.dumps({'error': str(error)}), flush=True)
            if not args.watch:
                return
            time.sleep(2)


if __name__ == '__main__':
    main()
