"""Exact-company adopted copy; adoption never grants permission to send."""
from pathlib import Path
import hashlib
import json

HERE = Path(__file__).resolve().parent

class AdoptionError(ValueError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

def records():
    return json.loads((HERE / 'message_adoptions.json').read_text(encoding='utf-8'))

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

def effective_sender(name, url, sender):
    case = case_for(name, url)
    return dict(sender, **case['sender_overrides']) if case else dict(sender)

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

def validate_body(name, url, body, sender):
    case = case_for(name, url)
    if not case:
        return
    effective = effective_sender(name, url, sender)
    if not case.get('adoption_approved'):
        raise AdoptionError('本文採用が承認されていません')
    actual = hashlib.sha256(body.encode('utf-8')).hexdigest()
    subject = effective.get('件名', '').replace('{企業名}', name)
    if actual != case['body_sha256'] or subject != case['subject'] or digest(effective) != case['sender_sha256']:
        raise AdoptionError('採用済み全文・件名・送信元のどれかが変わりました')

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
