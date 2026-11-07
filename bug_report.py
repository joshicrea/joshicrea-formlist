"""不具合の提出の入口(台帳サーバーの report.php への提出と、本人の削除依頼)。実行環境: Windows / Mac / Linux(Python 3.9+)
動き: 許可キー・導入ID・製品の版は、利用許可の照合(license_check)と同じものが自動で付く。利用者が鍵や版を入力することは無い。
サービス非依存: 台帳サーバー側は許可キーの持ち主のサービスで受ける。ほかのツール(スレッズ等)は、Config を自分の値で作ればこのファイルをそのまま使える。
返す形: {"ok": True, "receipt": 受付番号, "mail": "sent"|"failed"|"held", "message": 画面に出す文} か {"error": 画面に出す文}。許可キーの値は返さない・記録しない。
連絡先メールは台帳サーバーの提出にだけ入り、この導入先の記録には残さない。"""
import json
import re
from dataclasses import dataclass
from typing import Callable

import requests


@dataclass
class Config:
    service_id: str
    version: str            # 製品全体の版(台帳の min_version と比べる値)
    url: str                # report.php の完全な URL
    read_key: Callable      # () -> 許可キー(無ければ空文字)
    install_id: Callable    # () -> 導入ID(16桁の16進)
    timeout: int = 12


FIELD_JA = {"summary": "件名(60字まで・必須)", "detail": "内容(800字まで)", "contact": "連絡先メールアドレス(完了の連絡に使います)"}
MAIL_JA = {"sent": "受付番号つきのメールをお送りしました。",
           "failed": "受付メールは送れませんでした。受付番号を控えてください。",
           "held": "受付メールは送りませんでした。受付番号を控えてください。"}


def _post(cfg, body):
    key = cfg.read_key()
    if not key:
        return None, {"error": "許可キーが設定されていません。設定の「計測キー」に、担当者から届いた鍵を入れてから送ります。"}
    body = dict(body, service_id=cfg.service_id, version=cfg.version)
    try:
        r = requests.post(cfg.url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), timeout=cfg.timeout,
                          headers={"X-License-Key": key, "X-Install-Id": cfg.install_id(), "Content-Type": "application/json; charset=utf-8"})
        code = r.status_code
        d = r.json()
        if not isinstance(d, dict):
            raise ValueError
    except (requests.RequestException, ValueError):
        return None, {"error": "不具合の送り先に通信できませんでした。入力した内容はこの画面に残っています。通信を確かめて、もう一度押してください。"}
    return code, d


def submit(cfg, summary, detail, contact):
    summary, detail, contact = (summary or "").strip(), (detail or "").strip(), (contact or "").strip()
    if not summary:
        return {"error": "件名を入れてください(どこで何が起きたかを一言で)。"}
    code, d = _post(cfg, {"action": "submit", "summary": summary, "detail": detail, "contact": contact})
    if code is None:
        return d
    if code == 200 and d.get("ok"):
        mail = d.get("mail") if d.get("mail") in MAIL_JA else "failed"
        return {"ok": True, "receipt": d.get("receipt"), "mail": mail, "duplicate": bool(d.get("duplicate")),
                "message": "受け付けました。受付番号は " + str(d.get("receipt")) + " です。" + MAIL_JA[mail]}
    if code == 400 and d.get("reason") == "invalid_param":
        return {"error": FIELD_JA.get(d.get("field"), "入力") + "を確かめてください。"}
    return {"error": d.get("message") or "不具合を受け付けられませんでした。時間を置いてもう一度押してください。"}


def delete(cfg, receipt):
    receipt = (receipt or "").strip().upper()
    if not re.fullmatch(r"BR-\d{8}-[A-Z0-9]{4,8}", receipt):
        return {"error": "受付番号の形が違います(例 BR-20261107-AB12)。"}
    code, d = _post(cfg, {"action": "delete", "receipt": receipt})
    if code is None:
        return d
    if code == 200 and d.get("deleted"):
        return {"ok": True, "message": "削除しました。ご連絡内容と連絡先は、受け付けた側から消えています。"}
    return {"error": d.get("message") or "削除できませんでした。受付番号を確かめてください。"}
