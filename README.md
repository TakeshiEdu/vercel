# バスナビおおいた Vercel 版

このフォルダは、Vercel にそのまま Import / Deploy できる単独ホスト用プロジェクトです。

フロントエンドは `public/index.html` に配置し、API は `api/index.py` の Flask アプリとして動かします。フロントと API が同じ Vercel ドメインになるため、ブラウザの CORS ブロックを避けられます。

## 構成

```text
vercel/
  api/
    index.py          # Flask API / 外部バスサイトへの中継
  public/
    index.html        # 現行UI
  requirements.txt    # Python依存
  vercel.json         # /api と静的ページのルーティング
```

## デプロイ手順

1. この `vercel/` フォルダだけを GitHub リポジトリとして push します。
2. Vercel にログインします。
3. `Add New...` -> `Project` を選びます。
4. push した GitHub リポジトリを Import します。
5. Framework Preset は `Other` のままで大丈夫です。
6. Root Directory はリポジトリ直下がこの `vercel/` の中身になるようにします。
7. Build Command は空欄で大丈夫です。
8. Output Directory も空欄で大丈夫です。
9. Deploy を押します。

デプロイ後、以下のようなURLで動きます。

```text
https://your-project.vercel.app/
https://your-project.vercel.app/api/health
```

## 既存リポジトリに含める場合

既存リポジトリのサブフォルダとして `vercel/` を push する場合は、Vercel の Project Settings で `Root Directory` を `vercel` に設定してください。

## CORS回避の仕組み

ブラウザは直接外部バスサイトを叩きません。

```text
public/index.html
  -> /api/stops/approach
  -> Vercel Functions
  -> 外部バスサイト
```

フロントの `API_BASE` は次のままです。

```js
const API_BASE = `${window.location.origin}/api`;
```

同一ドメインの `/api` を叩くので、ブラウザ側の CORS 問題を避けられます。

## 無料運用向けの負荷対策

外部バスサイトへのアクセスを増やしすぎないよう、Vercel 版では以下を入れています。

- フロントの自動更新を30秒間隔に調整
- `/api/stops/approach` と `/api/bus/location` は Vercel CDN で短時間キャッシュ
- `/api/stops/directions` は長めにキャッシュ
- Vercel上では自動更新の `refresh=1` を無視して、キャッシュを優先
- 画面が非表示の間は自動更新を停止

主なキャッシュ設定:

```text
/api/stops/approach      20秒キャッシュ + 40秒 stale
/api/bus/location        20秒キャッシュ + 40秒 stale
/api/stops/directions    6時間キャッシュ + 24時間 stale
/api/stops/suggest       1時間キャッシュ + 6時間 stale
/api/routes/search       60秒キャッシュ + 120秒 stale
```

## ローカル確認

Vercel CLI を使う場合:

```bash
npm i -g vercel
vercel dev
```

ローカルで通常の Flask として動かす場合は、リポジトリルート側の `busnavi_server.py` を使う運用の方が簡単です。

## 注意点

- Vercel Hobby は個人・非商用向けです。
- サーバーレス環境なので、メモリキャッシュは永続ではありません。
- 外部バスサイトの応答が遅い場合、Vercel Functions の実行時間制限に当たる可能性があります。
- 公開利用者が増える場合は、更新間隔やキャッシュ時間をさらに長くしてください。

