"""Controller-only publication of one authorized UTF-8 file to the selected project."""
import hashlib
import os

from core.paths import open_directory
from runtime.safety import digest_json


def open_source(source, identity):
    fd = open_directory(source)
    info = os.fstat(fd)
    selected = hashlib.sha256(f'{source}:{info.st_dev}:{info.st_ino}'.encode()).hexdigest()
    if selected != identity:
        os.close(fd)
        raise RuntimeError('source_identity_changed')
    return fd


def prepare_publication(source, identity, path, before_digest, helper):
    fd = open_source(source, identity)
    try:
        root = f'/proc/self/fd/{fd}/.'
        state = helper.target_state(root, path)
        if digest_json(state) != before_digest:
            raise RuntimeError('source_conflict')
        return {'path': path, 'before_digest': before_digest, 'parents': helper._parents(root, path),
                'mode': state.get('mode', 0o644)}
    finally:
        os.close(fd)


def publish_file(source, identity, grant, data, helper):
    if len(data) > helper.MAX_FILE_BYTES:
        raise ValueError('publication_budget')
    fd = open_source(source, identity)
    try:
        root = f'/proc/self/fd/{fd}/.'
        path = grant['path']
        if helper._parents(root, path) != grant['parents']:
            raise RuntimeError('source_parent_changed')
        helper._atomic_write(root, path, data, grant['before_digest'], grant['mode'])
        after = helper.target_state(root, path)
        if after['sha256'] != hashlib.sha256(data).hexdigest() or after['mode'] != grant['mode']:
            raise RuntimeError('publication_verification_failed')
    finally:
        os.close(fd)
