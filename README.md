# hdd-undelete

HDD から削除してしまったファイルを復元するツール。Python 標準ライブラリだけで動く単一スクリプト (`undelete.py`)。

- **NTFS 復元** — MFT に残っている削除済みレコードから、ファイル名・フォルダ構成・更新日時つきで復元
- **署名スキャン (カービング)** — ディスクを直接読んでファイルの中身から探す。フォーマット後や exFAT / FAT、上で見つからない場合用
- 復元元ドライブは読み取り専用で開く (書き込みは一切しない)

## 最初に (重要)

1. **消してしまったドライブにはもう何も書き込まない。** 新しいデータが書かれると消えたファイルが上書きされ、どんなツールでも戻せなくなる。このツールもそのドライブには置かない・ダウンロードしない。
2. **復元先は必ず別のドライブ** (別の HDD や USB メモリ) にする。同じドライブを指定するとエラーで止まる。
3. C ドライブ (Windows が動いているドライブ) は動作中も書き込みが続くので、できるだけ早く実行する。

## 使い方 (Windows)

必要なもの: Windows 10/11、[Python 3.8 以上](https://www.python.org/downloads/) (インストール時に "Add python.exe to PATH" にチェック)

### かんたん

`run.bat` をダブルクリック → 管理者権限の確認で「はい」→ 質問に答えるだけ。

```
復元元 (ドライブ文字 例: D ...): D
モード [1]: 1
復元先フォルダ (復元元とは別のドライブ): E:\recovered
```

### コマンド

管理者として開いたターミナルで実行する。

```bat
python undelete.py drives                          :: ドライブ一覧
python undelete.py scan D:                         :: 削除済みファイルの一覧
python undelete.py scan D: --name *.jpg --csv list.csv
python undelete.py recover D: -o E:\recovered      :: すべて復元
python undelete.py recover D: -o E:\recovered --name *.psd --name *.clip
python undelete.py recover D: -o E:\recovered --id 1234 --id 5678
python undelete.py recover D: -o E:\recovered --good-only
python undelete.py carve D: -o E:\carved           :: 署名スキャン
python undelete.py carve D: -o E:\carved --types jpg,png
```

ドライブ文字の代わりにディスクイメージ (`dd` などで取った `.img` / `.raw`) も指定できる。Linux / macOS でもイメージや `/dev/sdX1` に対して動く。

## 「状態」の見方

| 状態 | 意味 |
|---|---|
| 良好 | ファイルがあった場所は今も空き。中身が残っている可能性が高い |
| 一部上書き | 一部が別のファイルに再利用されている。開けないか、途中から壊れている可能性が高い |
| 上書き済み | 全体が別のファイルに使われている。復元しても中身は別物 |
| 圧縮 / 暗号化 | NTFS 圧縮・EFS 暗号化ファイル。非対応 (スキップ) |

「良好」でも、一度上書きされてからその上書きしたファイルも消された場合などは中身が壊れていることがある。復元後に必ず開いて確認すること。

## 署名スキャンで復元できる種類

jpg / png / gif / pdf / zip (docx・xlsx・pptx は自動判別) / mp4 (mov・heic・avif・m4a) / wav / avi / webp

- ファイル名とフォルダは戻らない (`jpg_000001_at....jpg` のような連番になる)
- 断片化 (ディスク上で飛び飛びに保存) されていたファイルは正しく取り出せない
- NTFS ドライブでは空き領域だけを調べる。全域を調べるなら `--all-space`
- ディスク全体を読むので時間がかかる (HDD で 1TB あたり数時間が目安)

## 復元できないケース

- **SSD** — 削除と同時に TRIM でデータが消去されるため、ほぼ復元できない (USB 接続の HDD・SD カード・USB メモリは可能性あり)
- 削除後に大量の書き込みがあった / 完全フォーマット (クイックでない) をした
- BitLocker で暗号化されたドライブ — ロック解除してマウントした状態のドライブ文字を指定すれば可
- 物理的に壊れた HDD (異音がする・認識しない) — 通電を続けると悪化する。専門業者へ
- exFAT / FAT32 の名前つき復元 (署名スキャンのみ対応)

## 免責

無保証。復元できる・できないは消した後のドライブの状態しだい。大事なデータの場合は、作業前にドライブ全体のイメージを取ってから試すのが安全。

## License

MIT
