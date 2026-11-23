# Pydantic 2.14.1 / Python 3.12; approved immutable source facts and independent provenance.
from hashlib import sha256  # Bind the packaged source and each returned excerpt to reviewed bytes.
from importlib.resources import files  # Runtime never opens the developer's canonical source path.
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from shopunow.records import Department


class KnowledgeRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    source_id: str = Field(pattern=r'^SU-(HR|IT|FA|BP|SD|CW)-[0-9]{3}$')
    version: str = Field(min_length=1)
    source_question: str = Field(min_length=1)
    answer_facts: tuple[str, ...] = Field(min_length=1)
    eligible_audience: tuple[Literal['employee', 'customer'], ...] = Field(min_length=1)
    subject: Department | Literal['company_wide']
    classification: Literal['general', 'non_general']
    access_scope: Literal['employee_general', 'customer_general', 'company_wide',
                          'employee_personal_handoff', 'customer_personal_handoff',
                          'home_department_internal']
    approval_status: Literal['owner_approved']
    current_status: Literal['current']
    synthetic: bool  # Strict bool prevents JSON 1 from impersonating an approved synthetic flag.

    @model_validator(mode='after')
    def consistent_labels(self):
        if self.synthetic is not True:
            raise ValueError('Knowledge must be explicitly synthetic.')
        audience = set(self.eligible_audience)
        if len(audience) != len(self.eligible_audience) or not all(x.strip() for x in self.answer_facts):
            raise ValueError('Invalid Knowledge audience or facts.')
        if self.subject in ('hr', 'it_support', 'facilities_admin') and 'customer' in audience:
            raise ValueError('Employee-only subject has a customer audience.')
        if self.subject == 'company_wide':
            valid = self.access_scope == 'company_wide' and self.classification == 'general'
        elif self.classification == 'general':
            valid = ((self.access_scope == 'employee_general' and audience == {'employee'}) or
                     (self.access_scope == 'customer_general' and audience == {'customer'}))
        else:
            valid = ((self.access_scope in ('employee_personal_handoff', 'home_department_internal')
                      and audience == {'employee'}) or
                     (self.access_scope == 'customer_personal_handoff' and audience == {'customer'}))
        if not valid:
            raise ValueError('Inconsistent approved source labels.')
        return self


class KnowledgeSource(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    schema_version: Literal['v1']
    dataset_status: Literal['owner_approved']
    synthetic: bool
    records: tuple[KnowledgeRecord, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def require_synthetic(self):
        if self.synthetic is not True:
            raise ValueError('Knowledge source must be explicitly synthetic.')
        return self


def knowledge_payload(record):
    # Keep whole approved FAQ facts together; there is no second editable FAQ format.
    text = record.source_question + '\n' + '\n'.join(record.answer_facts)
    metadata = record.model_dump(mode='json', exclude={'source_question', 'answer_facts'})
    return text, metadata


def _catalog(raw):
    source = KnowledgeSource.model_validate_json(raw)
    catalog = {row.source_id: row for row in source.records}
    if len(catalog) != len(source.records):
        raise ValueError('Duplicate approved source ID.')
    return catalog


def _index(raw, catalog):
    return {'recipe_version': 1, 'source_sha256': sha256(raw).hexdigest(), 'entries': {
        key: {'version': row.version, 'digest': sha256(knowledge_payload(row)[0].encode()).hexdigest(),
              'metadata': knowledge_payload(row)[1]} for key, row in catalog.items()}}


def prepare_knowledge(source, destination):
    raw = Path(source).read_bytes()
    catalog = _catalog(raw)  # Reject all invalid records before writing either artifact.
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    (target/'knowledge.json').write_bytes(raw)
    (target/'knowledge_index.json').write_text(json.dumps(_index(raw, catalog), sort_keys=True,
                                                        indent=2)+'\n')


def load_knowledge(directory=None):
    root = files('shopunow').joinpath('assets') if directory is None else Path(directory)
    raw = root.joinpath('knowledge.json').read_bytes()
    catalog = _catalog(raw)
    if json.loads(root.joinpath('knowledge_index.json').read_text()) != _index(raw, catalog):
        raise ValueError('Approved-source index is missing, stale or inconsistent.')
    return catalog


def source_disposition(record, *, role, department):
    if role not in record.eligible_audience:
        return 'restricted'
    if record.access_scope == 'home_department_internal' and department != record.subject:
        return 'restricted'
    return 'answerable' if record.classification == 'general' else 'handoff'


def knowledge_filter(*, role, destination):
    subject = 'company_wide' if destination == 'central_support' else destination
    if role not in ('employee', 'customer') or subject not in (
            'company_wide', 'hr', 'it_support', 'facilities_admin', 'billing_payments', 'shipping_delivery'):
        raise ValueError('Invalid code-owned retrieval scope.')
    if role == 'customer' and subject in ('hr', 'it_support', 'facilities_admin'):
        raise ValueError('Restricted retrieval scope.')
    return {'$and': [{'approval_status': 'owner_approved'}, {'current_status': 'current'},
        {'eligible_audience': {'$contains': role}}, {'subject': subject}, {'classification': 'general'}]}


def verify_knowledge(record_id, text, metadata, *, catalog, role, department, destination):
    expected = catalog.get(record_id)
    if expected is None:
        return None
    document, labels = knowledge_payload(expected)
    subject = 'company_wide' if destination == 'central_support' else destination
    if (type(metadata) is not dict or json.dumps(metadata,sort_keys=True) != json.dumps(labels,sort_keys=True)
            or text != document
            or expected.subject != subject or
            source_disposition(expected, role=role, department=department) != 'answerable'):
        return None
    return expected  # Equality is against the independently packaged source, never the hit itself.


# Flow: explicit approved-source preparation -> independent deployed index -> scoped returned-hit check.


def index_knowledge(*, catalog, storage, models, account_check, timeout):
    """Explicit operator action AFTER external authorization/account checks; never called at startup.

    Caller retains responsibility for selecting the approved account, existing configured
    collection, spend authorization and one supervised writer. No collection creation,
    upsert, retry, migration, environment loading or automatic reconciliation is performed.
    """
    if account_check() is not True:
        raise ValueError('Knowledge indexing account gate has not passed.')
    created=existing=0
    for record in catalog.values():
        budget=min(8.0,timeout())
        raw=storage.exact_knowledge(record.source_id,timeout=budget)
        timeout()
        document,metadata=knowledge_payload(record)
        intended={'ids':[record.source_id],'documents':[document],'metadatas':[metadata]}
        if all(raw.get(key)==value for key,value in intended.items()):
            existing+=1
            continue
        if not all(raw.get(key)==[] for key in intended):
            raise ValueError('Existing Knowledge source conflicts or is unverified.')
        vector=models.embed([document],timeout=timeout())[0]
        timeout()
        storage.write_knowledge(record,vector,timeout=min(8.0,timeout()))
        raw=storage.exact_knowledge(record.source_id,timeout=min(8.0,timeout()))
        timeout()
        if not all(raw.get(key)==value for key,value in intended.items()):
            raise ValueError('Intended Knowledge save is unverified; no retry permitted.')
        created+=1
    return {'created':created,'existing':existing}
