# Python 3.12 / Pydantic 2.14.1; approved source bytes, existing stored models, no providers.
from hashlib import sha256  # Verify the deployed source independently of stored records.
from importlib.resources import files  # Runtime assets never depend on the external source checkout.
import json
from pathlib import Path
from shopunow.records import ProfileRecord


def asset_bytes(name):
    return files('shopunow').joinpath('assets', name).read_bytes()


def _records(raw, created_at):
    source = json.loads(raw)
    if (source['schema_version'] != 'v1' or source['dataset_status'] != 'owner_approved'
            or source['synthetic'] is not True):
        raise ValueError('Unapproved profile source.')
    result = {}
    for row in source['profiles']:
        if (row['approval_status'] != 'owner_approved' or row['current_status'] != 'current'
                or row['synthetic'] is not True or row['user_id'] in result):
            raise ValueError('Invalid approved profile binding.')
        record = ProfileRecord.model_validate_json(json.dumps({
            'record_type':'profile','user_id':row['user_id'],'role':row['role'],
            'home_department':row['home_department'],'created_at':created_at,
            'complete':True,'version':1}))
        result[record.user_id] = record
    if not result:
        raise ValueError('Empty approved roster.')
    return result


def prepare(source, destination, *, created_at):
    raw = Path(source).read_bytes()
    _records(raw, created_at)  # Validate before writing any generated artifact.
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    provenance = {'recipe_version':1,'source_filename':'profiles.json','source_schema_version':'v1',
        'source_sha256':sha256(raw).hexdigest(),'output_sha256':sha256(raw).hexdigest(),
        'profile_created_at':created_at}
    (target/'profiles.json').write_bytes(raw)  # Byte-identical source, not another editable format.
    (target/'provenance.json').write_text(json.dumps(provenance,sort_keys=True,indent=2)+'\n')


def load_profiles(directory=None):
    read = asset_bytes if directory is None else lambda name:(Path(directory)/name).read_bytes()
    raw, provenance = read('profiles.json'), json.loads(read('provenance.json'))
    if (provenance['recipe_version'] != 1 or provenance['source_schema_version'] != 'v1'
            or sha256(raw).hexdigest() != provenance['source_sha256']
            or sha256(raw).hexdigest() != provenance['output_sha256']):
        raise ValueError('Profile provenance mismatch.')
    return _records(raw, provenance['profile_created_at'])


# Flow: explicit local preparation -> pinned source/output digests -> packaged verified roster.
