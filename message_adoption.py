"""Exact-company adopted copy; adoption never grants permission to send."""
from pathlib import Path
import hashlib
import json
import re
import time
import unicodedata
import uuid

import settings_dir

HERE = Path(__file__).resolve().parent

class AdoptionError(ValueError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

def _retry(fn, tries=1000, wait=0.005):
    """置き換え・読み込みの最中に別の処理が同じファイルを開いていると(Windows)、PermissionError になる。短い間隔で再試行する"""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)

def norm_contact(value):
    """連絡先(フォームURL・メールアドレス・mailto:)の照合用の形。大文字小文字・全角・http/https・www・末尾の「/」・mailto: の違いを同じものとして扱う"""
    v = unicodedata.normalize('NFKC', str(value or '')).strip().lower()
    if v.startswith('mailto:'):
        v = v[len('mailto:'):].strip()
    if '@' in v and not v.startswith('http'):
        return v.split('?')[0].strip()
    v = re.sub(r'^https?://', '', v).split('#')[0]
    v = re.sub(r'^www\.', '', v)
    return v.rstrip('/')

def norm_name(value):
    """会社名の照合用の形(前後の空白・全角空白を除く)。CSVの値と画面から来た値を、同じ形で比べる"""
    return unicodedata.normalize('NFKC', str(value or '')).replace('\u3000', ' ').strip()

def _names_of(case):
    return {norm_name(case.get('source_name')), norm_name(case.get('display_name'))}

def _contacts_of(case):
    return {norm_contact(case.get('form_url'))} | {norm_contact(x) for x in case.get('contacts', [])}

def records():
    path = settings_dir.root() / 'message_adoptions.json'
    if not path.exists():  # 公開版には無い(採用記録は開発側だけが持つ)。無い=採用済みの会社は0社
        return {'cases': []}
    return _load(path)

_CACHE = {}

def _load(path):
    """記録の読み込み。ファイルが変わっていなければ(更新時刻と大きさが同じ)読み直さない。読み込みでファイルを開きっぱなしにして、書き込み側の置き換えを妨げないため"""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _CACHE.get('key') == key:
            return json.loads(_CACHE['text'])
    except OSError:
        key = None
    try:
        text = _retry(lambda: path.read_text(encoding='utf-8'))
        data = json.loads(text)
        if key:
            _CACHE.update(key=key, text=text)
        return data
    except ValueError as e:  # JSONDecodeError。ファイルは書き換えず、壊れていることを1行で言う(該当会社は送らない側に倒れる)
        raise AdoptionError('文面の指定の記録(message_adoptions.json)が壊れていて読めません。直るまで、指定のある会社は送りません: ' + str(e)[:60])

def case_for(name, url):
    allcases = records()['cases']
    for c in allcases:  # 文面の指定は会社名とフォームURLの組で見る(同名・別URL、同じURLの別名は、その会社の指定ではない)
        if c.get('assigned_only') and norm_contact(url) in _contacts_of(c) and norm_name(name) in _names_of(c):
            return c  # フォームURL・メールアドレス・mailto のどれで来ても同じ会社の指定として見つかる
    cases = [c for c in allcases if not c.get('assigned_only')]
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
    if case and case.get('assigned_only'):  # 文面の指定だけの記録: 送信元は変えない。件名の業種は呼び出し側(autosend.subject_industry が指定と業種ごとの文面のオン・オフを見て決める)の値のまま
        case = None
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
    if case.get('assigned_only'):  # 指定した業種の文面(見本と同じ選び方)。書き出し・会社名は行のまま
        return row, pick_message(message_path, case.get('template_industry') or ''), effective_sender(name, url, sender)
    filename = case['template_name']
    if Path(filename).name != filename:
        raise AdoptionError('承認テンプレートのパスが不正です')
    template = (settings_dir.root() / '文面' / filename).read_text(encoding='utf-8-sig')
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
    if not case or case.get('assigned_only'):
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
    if not case or case.get('assigned_only'):
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
    if case and not case.get('assigned_only') and not case.get('send_approved'):
        raise AdoptionError('この会社は本文採用済みですが、実送信は承認されていません')

def validate_destination(name, original_url, actual_url):
    case = case_for(name, original_url)
    if case and not case.get('assigned_only') and actual_url not in case.get('allowed_destination_urls', [case['form_url']]):
        raise AdoptionError('実際のフォームURLが採用時の対象と一致しません')

def record_digest():
    return digest(records())


def assigned_industry(name, url):
    """会社ごとに指定した文面の業種('' は標準)。指定が無ければ None"""
    case = case_for(name, url)
    return (case.get('template_industry') or '') if case and case.get('assigned_only') else None

def assign(name, url, industry, contacts=()):
    """会社ごとの文面の指定(採用記録の assigned_only の1件)。industry=None で指定を外す。
    文面の選択だけで、送信の許可ではない(send_approved は False のまま)。採用済みの会社は書き換えない。他の記録は一字も変えない"""
    import filelock
    path = settings_dir.root() / 'message_adoptions.json'
    with filelock.locked(path, timeout=60.0):  # 同時の指定で、後の書き込みが先を消さない・一時ファイルが重ならない
        _assign_locked(path, name, url, industry, contacts)

def _assign_locked(path, name, url, industry, contacts=()):
    data = _load(path) if path.exists() else {'schema': 1, 'cases': []}
    cases = data.setdefault('cases', [])
    keys = {norm_contact(url)} | {norm_contact(x) for x in contacts if x}
    mine = [c for c in cases if c.get('assigned_only') and keys & _contacts_of(c) and norm_name(name) in _names_of(c)]
    if any(not c.get('assigned_only') and norm_contact(c.get('form_url')) in keys for c in cases):
        raise AdoptionError('この会社は採用済みの個別の文面があるため、指定では変えません')
    cases[:] = [c for c in cases if c not in mine]
    if industry is not None:
        cases.append({'source_name': name, 'display_name': name, 'form_url': url, 'contacts': sorted(keys), 'allowed_destination_urls': [url], 'assigned_only': True,
                      'template_industry': industry, 'adoption_approved': True, 'send_approved': False})
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        _retry(lambda: tmp.replace(path))
    finally:
        if tmp.exists():  # 置き換えに失敗しても一時ファイルを残さない
            try:
                tmp.unlink()
            except OSError:
                pass
