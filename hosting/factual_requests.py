"""Narrow factual intents; property facts remain outside generic code."""
import re
from pathlib import Path


def address_request(message):
    q = message.casefold()
    return bool(re.search(r"\b(?:address|postcode|post code|postal code)\b", q)
                and re.search(r"\b(?:could|can|what|where|send|give|confirm|need|please)\b", q)
                and not re.search(r"\b(?:restaurant|pub|club|station|airport|shop|supermarket|email|billing)\b", q))


def address_outcome(message, context):
    if not address_request(message):
        return None
    address = context.get('verified_property_address')
    if not address:
        return {'action': 'review', 'answer': '', 'reason': 'Exact address requires a verified confirmed reservation and property address'}
    name = context.get('guest_first_name') or 'there'
    return {'action': 'draft', 'answer': f"Hi {name},\n\nThe property address is {address}.",
            'reason': 'Confirmed reservation and property address verified in Hospitable',
            'sources': [{'source': 'Hospitable verified property address', 'text': address}]}


def station_taxi_outcome(prop, message):
    q = message.casefold()
    if not re.search(r'\b(?:taxi|taxis|uber|cab|cabs)\b', q):
        return None
    # Do not turn a mixed host decision into an informational fare reply.
    if re.search(r'\b(?:refund|compensation|discount|cancel\w*|extend\w*|extension|damage|injur\w*|unsafe|emergency|leak|fire|complaint|broken|fault|heating|boiler|wifi|password|code|unlock|locked|luggage|early|late|check[ -]?in|check[ -]?out|arrange|order|pay|payment|reimburse\w*)\b', q):
        return None
    if re.search(r'\b(?:book|reserve)\s+(?:me|us|a|the|my|our)\b', q):
        return None
    money = list(re.finditer(r'\b(?:price|cost|charge|fee|rate)\b', q))
    if len(money) > 1:
        return None
    if money and not re.search(r'\b(?:price|cost|charge|fee|rate)\s+(?:of|for)\s+(?:(?:a|the|an)\s+)?(?:taxi|uber|cab)\b|\b(?:taxi|uber|cab)\s+(?:price|cost|fare|charge|fee|rate)\b', q):
        return None
    if re.search(r'\b(?:stay|nights?|room|accommodation|rental|deposit|cleaning|booking|reservation)\s+(?:price|cost|charge|fee|rate)\b', q):
        return None
    try:
        reference = (Path(prop['runtime_dir']) / 'docs/station_taxi.md').read_text(encoding='utf-8')
    except (KeyError, OSError):
        return None
    # Each station is an explicit heading followed by owner-approved facts.
    sections = re.split(r'^## ', reference, flags=re.M)[1:]
    selected = []
    general = ''
    for section in sections:
        heading, _, body = section.partition('\n')
        if heading.strip() == 'General':
            general = body.strip()
        elif re.search(r'\b' + re.escape(heading.strip().casefold()) + r'\b', q):
            selected.append(body.strip())
    if not selected or not general or any(not item for item in selected):
        return None
    answer = '\n\n'.join(selected + [general])
    if '[' in answer or ']' in answer:
        return None  # Unfilled example guidance is not a fact.
    return {'action': 'draft', 'answer': answer, 'reason': 'Owner-approved informational station taxi guidance',
            'sources': [{'source': 'station_taxi.md', 'text': answer}]}
