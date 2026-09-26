"""Explicit, inactive backfill of verified owner-channel statements into ADR-001 memory."""
from __future__ import annotations

import glob
import hashlib
import json
import re
from typing import Callable

from .memory_runtime import MemoryRecord, MemoryScope, MemoryStorage

_CHANNEL = re.compile(r'<channel\s+([^>]*\bsource="plugin:telegram:telegram"[^>]*)>(.*?)</channel>', re.S)
_ATTRIBUTE = re.compile(r'([a-z_]+)="([^"]*)"')


def owner_key(bot: str, chat_id: str, message_id: str) -> str:
    """Canonical encoding of (bot, verified channel, chat, message)."""
    return json.dumps([bot, 'plugin:telegram:telegram', str(chat_id), str(message_id)],
                      ensure_ascii=False, separators=(',', ':'))


def _texts(entry: dict):
    if entry.get('isCompactSummary') is True:
        return
    if entry.get('type') == 'user':
        origin = entry.get('origin')
        if not (isinstance(origin, dict) and origin.get('kind') == 'channel'
                and origin.get('server') == 'plugin:telegram:telegram'):
            return
        message = entry.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, str):
            yield content
        elif isinstance(content, dict) and content.get('type') == 'text':
            yield content.get('text', '')
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    yield block.get('text', '')
    elif entry.get('type') == 'attachment':
        attachment = entry.get('attachment')
        origin = attachment.get('origin') if isinstance(attachment, dict) else None
        if (isinstance(attachment, dict) and attachment.get('type') == 'queued_command'
                and isinstance(origin, dict) and origin.get('kind') == 'channel'
                and origin.get('server') == 'plugin:telegram:telegram'
                and isinstance(attachment.get('prompt'), str)):
            yield attachment['prompt']


def _verify_telegram(message_id, *, text_sha256, transcripts_glob, access_path):
    """Apply owner_channel.verify_telegram's sender, chat and digest checks."""
    result = {'status': 'NOT_FOUND', 'message_id': str(message_id), 'user_id': None,
              'chat_id': None, 'ts': None, 'text_sha256': None}
    try:
        with open(access_path, encoding='utf-8') as stream:
            allowed = json.load(stream)['allowFrom']
        if not isinstance(allowed, list):
            raise ValueError('allowFrom must be a list')
        allowed = {str(sender) for sender in allowed}
        for path in sorted(glob.glob(transcripts_glob)):
            with open(path, encoding='utf-8') as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    for content in _texts(entry):
                        if not isinstance(content, str):
                            continue
                        for match in _CHANNEL.finditer(content):
                            attrs = dict(_ATTRIBUTE.findall(match.group(1)))
                            if attrs.get('message_id') != str(message_id):
                                continue
                            sender, chat = attrs.get('user_id'), attrs.get('chat_id')
                            digest = hashlib.sha256(match.group(2).strip().encode()).hexdigest()
                            result.update(user_id=sender, chat_id=chat, ts=attrs.get('ts'),
                                          text_sha256=digest)
                            if sender not in allowed or chat != sender:
                                result['status'] = 'SENDER_NOT_ALLOWED'
                            elif digest != text_sha256.removeprefix('sha256:'):
                                result['status'] = 'TEXT_MISMATCH'
                            else:
                                result['status'] = 'VERIFIED'
                            return result
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        result['status'] = 'UNAVAILABLE'
    return result


def ingest_owner_transcripts(
    storage: MemoryStorage, *, bot: str, transcripts_glob: str, access_path: str,
    supersedes: dict[str, str] | None = None, verifier: Callable | None = None,
) -> dict:
    """Backfill one explicit bot. Return counts and coverage gaps; never infer authority."""
    if not bot or not re.fullmatch(r'[a-z][a-z0-9_-]*', bot):
        raise ValueError('explicit bot/project name required')
    paths = sorted(glob.glob(transcripts_glob))
    report: dict = {'bot': bot, 'inserted': 0, 'existing': 0, 'gaps': []}
    if not paths:
        report['gaps'].append({'reason': 'MISSING_TRANSCRIPT', 'source': transcripts_glob})
        return report
    candidates: dict[str, list[dict]] = {}
    for path in paths:
        with open(path, encoding='utf-8') as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                for content in _texts(entry):
                    if not isinstance(content, str):
                        continue
                    for match in _CHANNEL.finditer(content):
                        attrs = dict(_ATTRIBUTE.findall(match.group(1)))
                        message_id = attrs.get('message_id')
                        if not message_id:
                            report['gaps'].append({'reason': 'MISSING_MESSAGE_ID', 'source': path,
                                                   'line': line_number})
                            continue
                        text = match.group(2).strip()
                        candidates.setdefault(message_id, []).append({
                            'attrs': attrs, 'text': text, 'timestamp': attrs.get('ts') or entry.get('timestamp'),
                            'source_ref': f'{path}:{line_number}',
                        })
    verify = verifier or _verify_telegram
    scope = MemoryScope(project=bot)
    for message_id, appearances in sorted(candidates.items(),
                                          key=lambda item: (item[0] in (supersedes or {}), item[0])):
        signatures = {(row['attrs'].get('user_id'), row['attrs'].get('chat_id'), row['text'])
                      for row in appearances}
        if len(signatures) != 1:
            report['gaps'].append({'reason': 'AMBIGUOUS_MESSAGE', 'message_id': message_id})
            continue
        row = appearances[0]
        digest = hashlib.sha256(row['text'].encode('utf-8')).hexdigest()
        try:
            proof = verify(message_id, text_sha256=digest, transcripts_glob=transcripts_glob,
                           access_path=access_path)
        except (OSError, ValueError, TypeError) as exc:
            report['gaps'].append({'reason': 'VERIFICATION_UNAVAILABLE', 'message_id': message_id,
                                   'detail': str(exc)})
            continue
        if (proof.get('status') != 'VERIFIED' or proof.get('chat_id') != row['attrs'].get('chat_id')
                or proof.get('user_id') != row['attrs'].get('user_id')
                or proof.get('text_sha256') != digest):
            report['gaps'].append({'reason': 'VERIFICATION_FAILED', 'message_id': message_id,
                                   'status': proof.get('status')})
            continue
        key = owner_key(bot, proof['chat_id'], message_id)
        prior_id = (supersedes or {}).get(message_id)
        prior = owner_key(bot, proof['chat_id'], prior_id) if prior_id else None
        value = {'kind': 'owner_statement', 'bot': bot, 'channel': 'plugin:telegram:telegram',
                 'chat_id': proof['chat_id'], 'message_id': str(message_id), 'text': row['text'],
                 'text_sha256': digest, 'timestamp': row['timestamp'],
                 'source_ref': row['source_ref'], 'verification': proof}
        if prior:
            value['supersedes'] = prior
        try:
            existing = storage.audit(scope, key)
            if existing:
                original = next(record for record in existing if record.key == key)
                if (original.value.get('text_sha256') == digest
                        and original.value.get('text') == row['text']
                        and original.value.get('supersedes') == prior
                        and original.value.get('verification', {}).get('status') == 'VERIFIED'):
                    report['existing'] += 1
                    continue
            storage.put(MemoryRecord('authoritative', key, value, scope))
        except ValueError as exc:
            report['gaps'].append({'reason': 'WRITE_REJECTED', 'message_id': message_id,
                                   'detail': str(exc)})
            continue
        report['existing' if existing else 'inserted'] += 1
    return report
