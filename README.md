# makebook-gui

USIエンジンで連続対局を行い、定跡を生成するブラウザーアプリです。対局の開始・待機・再開と、盤面・ツリー・棋譜・定跡DBのWeb公開ができます。

学習処理は、たややんさんの [HiraganaSuisho / makebook2.py](https://github.com/tayayan/HiraganaSuisho/blob/main/makebook2.py) を基にしています。[比較と変更点](docs/ALGORITHM.md)も参照してください。

## 始める

Python 3.12と、別途用意したUSIエンジン・評価関数が必要です。Windowsで動作を確認しています。

Windowsでは `Start.cmd` をダブルクリックします。初回に依存パッケージを準備します。コマンドで起動する場合：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

開いたブラウザーでエンジンの実行ファイル、開始局面、探索条件を指定して「開始・再開」を押します。デスクトップGUIは不要です。設定と結果は `data/` に保存され、同じ設定で再開すると続きから動きます。ブラウザーを閉じても対局は継続します。

手数上限は総手数です。Hashはエンジン1つ当たりの値、使用スレッド数の目安は「スレッド数×同時対局数」です。設定を変えて開始すると別の保存先になり、それまでの結果も残ります。

## Web公開

- 操作画面：`http://127.0.0.1:8765/`
- 公開用の観戦画面：`http://127.0.0.1:8766/`（読み取り専用）

観戦ポート `8766` をリバースプロキシやTunnelへ接続します。Cloudflareの [Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) で一時公開する例：

```powershell
cloudflared tunnel --url http://127.0.0.1:8766
```

表示されたURLを共有できます。Quick Tunnelは一時公開用で、固定URLの運用には通常のTunnel等を設定します。操作ポート `8765` は公開しません。

公開例：[定跡生成配信 202610](https://478shogi.com/makebook/202610/)

## 構成

```text
app.py        起動・ブラウザー操作・公開API
core/         USI通信・連続対局・学習・保存
web/          操作画面・観戦画面
data/         設定・棋譜・定跡・復旧情報（自動生成、Git対象外）
tests/        本家ロジック比較・模擬エンジン検証
```

別の保存先やポートは `python app.py --data data-other --port 8775 --public-port 8776` で指定できます。

```powershell
.venv\Scripts\python -B -m unittest discover -s tests
```

MIT License。エンジン・評価関数・第三者の定跡・認証情報は同梱しません。
