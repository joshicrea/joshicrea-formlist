"""Exact-company adopted copy; adoption never grants permission to send."""
from pathlib import Path
import hashlib
import json
import re

HERE = Path(__file__).resolve().parent

class AdoptionError(ValueError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

def records():
    path = HERE / 'message_adoptions.json'
    if not path.exists():  # 公開版には無い(採用記録は開発側だけが持つ)。無い=採用済みの会社は0社
        return {'cases': []}
    return json.loads(path.read_text(encoding='utf-8'))

def case_for(name, url):
    cases = records()['cases']
    matches = [c for c in cases if c['form_url'] == url]
    if not matches:
        if any(name in (c['source_name'], c['display_name']) for c in cases):
            raise AdoptionError('採用済み会社のフォームURLが一致しません')
        return None
    if len(matches) != 1 or name not in (matches[0]['source_name'], matches[0]['display_name']):
        raise AdoptionError('承認した会社名とフォームURLの組合せではありません')
    return matches[0]

def effective_sender(name, url, sender, industry=None):
    """送信元。件名の優先順: 採用記録の上書き > 業種別の件名(industry を渡したとき・patterns) > 送信元設定の件名。
    採用記録のある会社は、承認時の件名(subject)・送信元の照合値を変えないため、業種別の件名を当てない"""
    case = case_for(name, url)
    base = dict(sender)
    if industry and not case:
        import patterns
        subject = patterns.subject_for_industry(industry)
        if subject:
            base['件名'] = subject
    return dict(base, **case['sender_overrides']) if case else base

def resolve_row(row, message_path, sender, pick_message):
    name, url, industry, opener, hp = row
    case = case_for(name, url)
    if not case:
        return row, pick_message(message_path, industry), dict(sender)
    filename = case['template_name']
    if Path(filename).name != filename:
        raise AdoptionError('承認テンプレートのパスが不正です')
    template = (HERE / '文面' / filename).read_text(encoding='utf-8-sig')
    approved_row = (case['display_name'], url, industry, case['opener'], hp)
    return approved_row, template, effective_sender(name, url, sender)

KANA_ONLY = re.compile(r'[ァ-ヶーぁ-ゖ 　]{0,40}')

def sender_view(effective):
    """送信元の照合値を作るときの見え方。採用のあとに足された設定は照合から外す:
    資料URL=本文の置き換えにだけ使う(本文の照合値が変わらなければ、採用した全文に影響しない)。
    フリガナ=氏名の読み仮名。値は照合せず、読み仮名の文字だけであることを changed_parts で別に確かめる(自由な文字列は通さない)。
    氏名・メール・電話・郵便番号・住所・会社名・件名・署名が変われば照合値が変わる。新しい項目を足したときも、この見え方に入れない限り止まる(安全側)"""
    return {k: v for k, v in effective.items() if k not in ('資料URL', 'フリガナ')}

def _sender_matches(effective, recorded):
    """採用記録の作り方(フリガナ・資料URLのキーが空であった/無かった/全項目で照合した)のどれでも、その後に足されただけなら一致とみなす"""
    v = sender_view(effective)
    return recorded in {digest(effective), digest(v), digest(dict(v, フリガナ='')), digest(dict(v, 資料URL='')), digest(dict(v, フリガナ='', 資料URL=''))}

def changed_parts(name, url, body, sender):
    """採用時から変わった要素を、全文・件名・送信元・フリガナのうち実際に違うものだけ返す(採用記録のない会社は[])"""
    case = case_for(name, url)
    if not case:
        return []
    effective = effective_sender(name, url, sender)
    parts = []
    if hashlib.sha256(body.encode('utf-8')).hexdigest() != case['body_sha256']:
        parts.append('全文')
    subject = case['subject']
    subject_changed = effective.get('件名', '').replace('{企業名}', name) != subject
    if subject_changed:
        parts.append('件名')
    # 件名は送信元の照合値にも入っている。件名と別の項目が同時に変わっても両方を示すため、採用時の件名の元の形(会社名の部分を {企業名} に戻したもの)に直して照合する
    templates = {subject.replace(n, '{企業名}') for n in (name, case.get('display_name', name), case.get('source_name', name)) if n} | {subject}
    if subject_changed:
        sender_ok = any(_sender_matches(dict(effective, 件名=t), case['sender_sha256']) for t in templates)
    else:
        sender_ok = _sender_matches(effective, case['sender_sha256'])
    if not sender_ok:
        parts.append('送信元')
    if not KANA_ONLY.fullmatch(str(effective.get('フリガナ', ''))):
        parts.append('フリガナ(読み仮名以外の文字)')
    return parts

def validate_body(name, url, body, sender):
    """採用が承認されていなければ AdoptionError。そうでなければ、採用時から変わった要素の一覧(無ければ [])を返す"""
    case = case_for(name, url)
    if not case:
        return []
    if not case.get('adoption_approved'):
        raise AdoptionError('本文採用が承認されていません')
    return changed_parts(name, url, body, sender)  # 食い違いでは止めない(2026-11-02 運用者の決定: 採用のあとに手で文面を直すのは通常の流れ)。変わった要素の一覧を返し、画面に出す

def difference_text(parts):
    """画面に出す、採用時との食い違いの文(無ければ空)。止めないこと・送る前に全文を見られることを言う"""
    if not parts:
        return ''
    return '採用したときから変わっている点: ' + '・'.join(parts) + '。止めてはいません。送る前に、届く件名と全文を確かめられます'

def validate_send(name, url, body, sender):
    validate_body(name, url, body, sender)
    case = case_for(name, url)
    if case and not case.get('send_approved'):
        raise AdoptionError('この会社は本文採用済みですが、実送信は承認されていません')

def validate_destination(name, original_url, actual_url):
    case = case_for(name, original_url)
    if case and actual_url not in case.get('allowed_destination_urls', [case['form_url']]):
        raise AdoptionError('実際のフォームURLが採用時の対象と一致しません')

def record_digest():
    return digest(records())
