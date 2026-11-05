#!/bin/bash
# AIまるなげ営業 導入（Mac）。ダブルクリックで、導入と画面の起動まで進める。標準の curl と bash だけで動く
# 公開リポジトリの install.sh を取ってきて実行する。もう一度実行すると更新になる（送信元・文面・送らない会社・できたリストは残る）
# install.sh は、Claude が見つからないと、登録の段で終了コード1で止まる。本体はそこまでに置いてあるので、ここで画面を開く
echo ""
echo "AIまるなげ営業の導入を始めます。はじめては10分から20分かかります。この窓を閉じずに待ちます。"
echo "英語の文が出ても、意味は分からなくて大丈夫です。日本語で「インストールが終わりました」と出て、ブラウザに画面が開くまで待ちます。"
echo ""
f=$(mktemp "${TMPDIR:-/tmp}/formlist_install.XXXXXX") && curl -fsSL https://raw.githubusercontent.com/joshicrea/joshicrea-formlist/master/install.sh -o "$f" && bash "$f"
rc=$?
APP="$HOME/.claude/formlist/app"
if [ -f "$APP/install.sh" ]; then
  bash "$APP/install.sh" --open
  orc=$?
  [ "$rc" = 0 ] && rc=$orc
fi
echo ""
if [ "$rc" = 0 ]; then
  echo "終わりました。画面が開かないときは、ブラウザの上の欄に http://127.0.0.1:8780/ を入れます。"
else
  echo "注意の表示がありました。ブラウザに「AIまるなげ営業」の画面が開いていれば、そのまま使えます。"
  echo "画面が開かず、上に「できませんでした」などの日本語の表示があるときは、その表示をコピーして、このツールをお渡しした担当者に送ってください。"
fi
echo ""
echo "Return キーを押すと、この窓を閉じられます。"
read -r _
exit "$rc"
