#!/bin/bash
# 画面を開く（動いていればそれを開き、止まっていれば起動する）。Mac 標準の python3 は 3.9 で使えないため、install.sh が 3.10 以上を探して起動する
cd "$(dirname "$0")" || exit 1
exec bash ./install.sh --open
